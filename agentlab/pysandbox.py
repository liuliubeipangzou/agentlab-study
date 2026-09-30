"""在独立子进程中执行模型提供的 Python 代码，并限制其影响范围。

设计目标不是"安全沙箱"，而是**有界且可观测的本地代码执行**：

- 子进程 + 进程组，超时后杀掉整组，避免残留后台进程；
- `-I` 隔离模式：忽略 PYTHONPATH 与环境变量对 sys.path 的注入，不读取用户 site-packages；
- 工作目录固定为一个工作区内的临时目录，结束后删除；
- 内存与 CPU 使用 `resource.setrlimit` 硬限制（RLIMIT_CPU 在 macOS/Linux 上可靠触发）；
- 环境变量只保留最小集合，密钥不会进入子进程；
- 结果通过专用文件描述符回传，业务 stdout 被重定向后一并捕获。

**明确的边界**：子进程仍以当前用户身份运行，拥有完整文件系统与网络访问权限。
本模块不提供操作系统级隔离（如需请自行接入容器或 seccomp）。它是"把代码跑起来"
的能力，不是"把不可信代码关起来"的能力。网络层防护（netguard）作用在工具调用
路径上，子进程内的 socket 调用不受其约束。
"""

import asyncio
import json
import math
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 300.0
DEFAULT_MEMORY_MB = 1024
MAX_MEMORY_MB = 4096
MAX_CODE_CHARS = 100_000
MAX_OUTPUT_CHARS = 20_000

# 子进程只允许看到这些环境变量；密钥与代理配置一律不传递。
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "HOME", "SYSTEMROOT")

# 引导程序：把业务 stdout/stderr 重定向到缓冲区，再用专用 fd 回传结构化结果，
# 这样即使业务代码打印大量内容也不会污染协议通道。
_RUNNER = r'''
import contextlib, io, json, os, resource, sys, traceback
_payload = json.loads(sys.stdin.read())
_fd = int(_payload["result_fd"])
_code = _payload["code"]
_stdout, _stderr = io.StringIO(), io.StringIO()

# 第一道防线（子进程自保）：在 exec 之前由父进程设置的限制在部分平台上会被继承
# 但不可靠，故子进程自行再设一次。RLIMIT_AS 在 Linux 有效、macOS 上多半被忽略，
# 因此它只作为补充；真正的强制来自父进程的 RSS 看门狗。
_memory = int(_payload.get("memory_mb") or 0) * 1024 * 1024
if _memory > 0:
    for _name in ("RLIMIT_AS", "RLIMIT_DATA"):
        if hasattr(resource, _name):
            try:
                resource.setrlimit(getattr(resource, _name), (_memory, _memory))
            except (ValueError, OSError):
                pass
_cpu = int(_payload.get("cpu_seconds") or 0)
if _cpu > 0 and hasattr(resource, "RLIMIT_CPU"):
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (_cpu, _cpu + 1))
    except (ValueError, OSError):
        pass

_ok, _value = True, None
try:
    with contextlib.redirect_stdout(_stdout), contextlib.redirect_stderr(_stderr):
        _ns = {"__name__": "__main__", "__doc__": None}
        exec(compile(_code, "<agent_python>", "exec"), _ns)
        _value = _ns.get("result")
except BaseException:
    _ok = False
    _value = traceback.format_exc(limit=8)
_result = {"ok": _ok, "result": _value, "stdout": _stdout.getvalue(), "stderr": _stderr.getvalue()}
with os.fdopen(_fd, "w", encoding="utf-8") as _stream:
    json.dump(_result, _stream, ensure_ascii=False, default=repr)
'''


def _safe_value(value, depth=0):
    """把返回值转成 JSON 可序列化结构；不可序列化时退化为文本表示。"""
    if depth > 6:
        return "<达到最大嵌套深度>"
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else repr(value)
    if type(value) in (list, tuple, set, frozenset):
        return [_safe_value(item, depth + 1) for item in list(value)[:1000]]
    if type(value) is dict:
        return {str(key)[:200]: _safe_value(item, depth + 1)
                for key, item in list(value.items())[:1000]}
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return "<%d bytes>" % len(value)
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)[:2000]


def _limits(memory_mb, cpu_seconds):
    """在 fork 之后、exec 之前设置的限制集合。

    内存与 CPU 限制已经由子进程在 `_RUNNER` 内自行设置（更可靠且不依赖 fork
    后的 Python 状态），这里只保留文件大小限制，避免子进程写满磁盘。
    """
    def apply():
        import resource
        if hasattr(resource, "RLIMIT_FSIZE"):
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
            except (ValueError, OSError):
                pass

    return apply


def _child_environment():
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 明确移除可能影响子进程行为的变量。
    for name in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
                 "AGENTLAB_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    return env


def _rss_bytes(pid):
    """读取指定进程的常驻内存（RSS）字节数；无法获取时返回 None。

    按平台选择手段：
    - macOS：`libproc.proc_pidinfo`（原生，实时准确；`ps` 在受限环境可能不可用）
    - Linux：`/proc/<pid>/statm`
    - 其他：放弃父进程侧检查，改由子进程自限（见 _RUNNER）
    """
    if sys.platform == "darwin":
        return _rss_darwin(pid)
    return _rss_procfs(pid)


_PROC_PIDTASKINFO = 4


def _rss_darwin(pid):
    try:
        import ctypes
        import ctypes.util
        library = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        if not hasattr(library, "proc_pidinfo"):
            return None

        class _TaskInfo(ctypes.Structure):
            _fields_ = [
                ("pti_virtual_size", ctypes.c_uint64),
                ("pti_resident_size", ctypes.c_uint64),
                ("pti_total_user", ctypes.c_uint64),
                ("pti_total_system", ctypes.c_uint64),
                ("pti_threads_user", ctypes.c_uint64),
                ("pti_threads_system", ctypes.c_uint64),
                ("pti_policy", ctypes.c_int32),
                ("pti_faults", ctypes.c_int32),
                ("pti_pageins", ctypes.c_int32),
                ("pti_cow_faults", ctypes.c_int32),
                ("pti_messages_sent", ctypes.c_int32),
                ("pti_messages_received", ctypes.c_int32),
                ("pti_syscalls_mach", ctypes.c_int32),
                ("pti_syscalls_unix", ctypes.c_int32),
                ("pti_csw", ctypes.c_int32),
                ("pti_threadnum", ctypes.c_int32),
                ("pti_numrunning", ctypes.c_int32),
                ("pti_priority", ctypes.c_int32),
            ]

        info = _TaskInfo()
        written = library.proc_pidinfo(ctypes.c_int(pid), _PROC_PIDTASKINFO, 0,
                                       ctypes.byref(info), ctypes.sizeof(info))
        if written <= 0:
            return None
        return int(info.pti_resident_size)
    except Exception:
        return None


def _rss_procfs(pid):
    try:
        with open("/proc/%d/statm" % pid, "r") as stream:
            fields = stream.read().split()
        if len(fields) >= 2:
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    return None


def _children_peak_bytes():
    """已结束子进程的历史峰值（macOS 为字节，Linux 为 KiB）。

    仅在父进程侧无法实时读取 RSS 时作为兜底：它无法阻止超限，但能在事后
    判断"超限"而不是把结果误报为成功。
    """
    try:
        import resource
        raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except Exception:
        return None
    if raw is None:
        return None
    return raw if sys.platform == "darwin" else raw * 1024


async def _memory_watchdog(process, limit_bytes, interval=0.15):
    """轮询子进程 RSS，超限则终止整个进程组。

    macOS 的 Darwin 内核对 RLIMIT_AS 基本不生效，因此必须有这一层主动检查。

    返回值：
    - True  因内存超限被终止
    - False 进程自行结束（未超限）
    - None  本平台无法读取 RSS，未能实施检查（调用方需如实告知用户）
    """
    inspected = False
    while process.returncode is None:
        await asyncio.sleep(interval)
        if process.returncode is not None:
            break
        rss = _rss_bytes(process.pid)
        if rss is None:
            continue
        inspected = True
        if rss > limit_bytes:
            _kill_group(process)
            return True
    return False if inspected else None


async def run_python(code, timeout=DEFAULT_TIMEOUT, memory_mb=DEFAULT_MEMORY_MB,
                     cwd=None, max_output_chars=MAX_OUTPUT_CHARS):
    """执行一段 Python 代码，返回结构化结果。

    代码可通过定义名为 `result` 的变量来返回数据。返回 dict：
    `ok` / `result` / `stdout` / `stderr` / `duration` / `exit_code` /
    `timed_out` / `truncated`。
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code 不能为空")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError("code 超过 %d 字符上限" % MAX_CODE_CHARS)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError("timeout 必须是不超过 %s 的正数" % MAX_TIMEOUT)
    if type(memory_mb) is not int or not 64 <= memory_mb <= MAX_MEMORY_MB:
        raise ValueError("memory_mb 必须在 64 到 %d 之间" % MAX_MEMORY_MB)
    if type(max_output_chars) is not int or max_output_chars < 128:
        raise ValueError("max_output_chars 必须是 >= 128 的整数")

    workdir = None
    temporary = False
    if cwd is None:
        # 默认给一个工作区内的临时目录，退出后删除，避免污染项目目录。
        workdir = tempfile.mkdtemp(prefix="agentlab-py-")
        temporary = True
    else:
        workdir = str(Path(cwd).expanduser().resolve())
        Path(workdir).mkdir(parents=True, exist_ok=True)

    read_fd, write_fd = os.pipe()
    cpu_seconds = max(1, int(math.ceil(timeout)))
    payload = json.dumps({"code": code, "result_fd": write_fd,
                          "memory_mb": memory_mb, "cpu_seconds": cpu_seconds})
    started = asyncio.get_event_loop().time()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", "-c", _RUNNER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=_child_environment(),
            pass_fds=(write_fd,),
            start_new_session=True,          # 独立进程组，便于整组终止
            preexec_fn=_limits(memory_mb, cpu_seconds),
        )
        os.close(write_fd)
        write_fd = None
        watchdog = asyncio.ensure_future(
            _memory_watchdog(process, memory_mb * 1024 * 1024))
        killed_for_memory = False
        memory_inspected = True
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload.encode("utf-8")), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            # 终止前先尽力排空管道，保住超时前已产生的输出，便于排查。
            _kill_group(process)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                stdout, stderr = b"", b""
        if not watchdog.done():
            watchdog.cancel()
        try:
            outcome = await asyncio.wait_for(watchdog, timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            outcome = False
        if outcome is True:
            killed_for_memory = True
            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
            except Exception:
                pass
        elif outcome is None:
            memory_inspected = False

        duration = round(asyncio.get_event_loop().time() - started, 3)
        with os.fdopen(read_fd, "r", encoding="utf-8", errors="replace") as stream:
            read_fd = None
            raw_result = stream.read(MAX_OUTPUT_CHARS * 8)
        decoded = None
        if raw_result.strip():
            try:
                decoded = json.loads(raw_result)
            except ValueError:
                decoded = None

        # 兜底：若父进程无法实时读取 RSS，用子进程历史峰值事后判断是否超限，
        # 避免把"内存超限"误报为成功。
        if not killed_for_memory and not memory_inspected:
            peak = _children_peak_bytes()
            if peak is not None and peak > memory_mb * 1024 * 1024:
                return {"ok": False, "timed_out": False, "exit_code": process.returncode,
                        "error": "代码内存占用峰值约 %d MiB，超过 %d MiB 上限"
                                 % (peak // (1024 * 1024), memory_mb),
                        "stdout": _clip(stdout.decode("utf-8", "replace"), max_output_chars),
                        "stderr": _clip(stderr.decode("utf-8", "replace"), max_output_chars),
                        "duration": duration, "truncated": False,
                        "limit_exceeded": "memory", "memory_enforced": False}

        if killed_for_memory:
            return {"ok": False, "timed_out": False, "exit_code": process.returncode,
                    "error": "代码内存占用超过 %d MiB 上限，已终止进程组" % memory_mb,
                    "stdout": _clip(stdout.decode("utf-8", "replace"), max_output_chars),
                    "stderr": _clip(stderr.decode("utf-8", "replace"), max_output_chars),
                    "duration": duration, "truncated": False,
                    "limit_exceeded": "memory", "memory_enforced": True}

        if timed_out:
            return {"ok": False, "timed_out": True, "exit_code": process.returncode,
                    "error": "代码执行超过 %.1f 秒，已终止进程组" % timeout,
                    "stdout": _clip(stdout.decode("utf-8", "replace"), max_output_chars),
                    "stderr": _clip(stderr.decode("utf-8", "replace"), max_output_chars),
                    "duration": duration, "truncated": False}

        out = _clip(stdout.decode("utf-8", "replace"), max_output_chars)
        err = _clip(stderr.decode("utf-8", "replace"), max_output_chars)
        if decoded is None:
            if process.returncode != 0:
                hint = "（常见原因：内存超限、CPU 超时，或被信号终止）"
                return {"ok": False, "exit_code": process.returncode,
                        "error": "子进程异常退出，返回码 %s%s" % (process.returncode, hint),
                        "stdout": out, "stderr": err, "duration": duration,
                        "timed_out": False, "truncated": False}
            return {"ok": False, "exit_code": process.returncode,
                    "error": "子进程没有回传结构化结果", "stdout": out, "stderr": err,
                    "duration": duration, "timed_out": False, "truncated": False}

        if not decoded.get("ok"):
            return {"ok": False, "exit_code": process.returncode,
                    "error": _clip(str(decoded.get("result", "")), max_output_chars),
                    "stdout": _clip(decoded.get("stdout", ""), max_output_chars) or out,
                    "stderr": _clip(decoded.get("stderr", ""), max_output_chars) or err,
                    "duration": duration, "timed_out": False, "truncated": False}

        value = _safe_value(decoded.get("result"))
        rendered = json.dumps(value, ensure_ascii=False, default=repr)
        truncated = len(rendered) > max_output_chars
        if truncated:
            value = {"truncated": True, "preview": rendered[:max_output_chars],
                     "original_chars": len(rendered)}
        return {"ok": True, "result": value, "stdout": _clip(decoded.get("stdout", ""), max_output_chars),
                "stderr": _clip(decoded.get("stderr", ""), max_output_chars),
                "exit_code": process.returncode, "duration": duration,
                "timed_out": False, "truncated": truncated}
    finally:
        for descriptor in (write_fd, read_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is not None and process.returncode is None:
            _kill_group(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass
        if temporary and workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _clip(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（输出已截断，原始 %d 字符）" % len(text)


def _kill_group(process):
    """终止整个进程组；失败时退化为终止单个进程。"""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
