"""在独立子进程中执行模型提供的 Python 代码，并限制其影响范围。

设计目标不是"安全沙箱"，而是**有界且可观测的本地代码执行**：

- 子进程 + 进程组，超时后杀掉整组，避免残留后台进程；
- `-I` 隔离模式：忽略 PYTHONPATH 与环境变量对 sys.path 的注入，不读取用户 site-packages；
- 未指定 cwd 时使用临时目录并在结束后删除；`run_python` 工具会传入工作区根目录，写出的文件会保留；
- 内存与 CPU 使用 `resource.setrlimit` 硬限制（RLIMIT_CPU 在 macOS/Linux 上可靠触发）；
- 环境变量只保留最小集合，密钥不会进入子进程；
- 结果通过专用文件描述符回传，stdout/stderr 并发有界捕获。

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

# 引导程序：业务 stdout/stderr 保留独立管道，结果由专用 fd 回传。
_RUNNER = r'''
import json, os, resource, sys, traceback
_payload = json.loads(sys.stdin.read())
_fd = int(_payload["result_fd"])
_code = _payload["code"]
_output_limit = int(_payload["max_output_chars"])

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

if hasattr(resource, "RLIMIT_FSIZE"):
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
_ok, _value = True, None
try:
    _ns = {"__name__": "__main__", "__doc__": None}
    exec(compile(_code, "<agent_python>", "exec"), _ns)
    _value = _ns.get("result")
except BaseException:
    _ok = False
    _value = traceback.format_exc(limit=8)
try:
    _encoded = json.dumps(_value, ensure_ascii=False, default=repr)
except (TypeError, ValueError, RecursionError):
    _encoded = json.dumps(repr(_value), ensure_ascii=False)
_truncated = len(_encoded) > _output_limit
if _truncated:
    _value = {"truncated": True, "preview": _encoded[:_output_limit],
              "original_chars": len(_encoded)}
else:
    _value = json.loads(_encoded)
_result = {"ok": _ok, "result": _value, "truncated": _truncated}
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


async def _capture_stream(stream, limit):
    """持续排空管道，但只保存有界前缀，防止管道写满和父进程内存增长。"""
    chunks, kept, total = [], 0, 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if kept < limit:
            prefix = chunk[:limit - kept]
            chunks.append(prefix)
            kept += len(prefix)
    return b"".join(chunks), total > kept


async def _wait_and_clean_group(process):
    # asyncio Process.wait 可能等待被孙进程继承的 stdout EOF；先观察 leader 的退出。
    while process.returncode is None:
        await asyncio.sleep(0.02)
    _kill_group(process)
    return await process.wait()


async def run_python(code, timeout=DEFAULT_TIMEOUT, memory_mb=DEFAULT_MEMORY_MB,
                     cwd=None, max_output_chars=MAX_OUTPUT_CHARS):
    """执行当前用户批准的代码，返回结果、stdout/stderr 与资源限制诊断。

    三条输出管道并发排空并分别限额；每条退出路径都清理原进程组。子进程主动
    setsid 后脱离进程组不在本工具隔离保证内，需要容器级管理才能可靠限制。
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code 不能为空")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError("code 超过 %d 字符上限" % MAX_CODE_CHARS)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError("timeout 必须是不超过 %s 的正数" % MAX_TIMEOUT)
    if type(memory_mb) is not int or not 64 <= memory_mb <= MAX_MEMORY_MB:
        raise ValueError("memory_mb 必须在 64 到 %d 之间" % MAX_MEMORY_MB)
    if type(max_output_chars) is not int or not 128 <= max_output_chars <= 100000:
        raise ValueError("max_output_chars 必须在 128 到 100000 之间")
    if os.name != "posix":
        raise RuntimeError("当前代码执行器需要 POSIX 进程组和资源限制支持")
    temporary = cwd is None
    workdir = tempfile.mkdtemp(prefix="agentlab-py-") if temporary else str(Path(cwd).expanduser().resolve())
    Path(workdir).mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    payload = json.dumps({"code": code, "result_fd": write_fd,
                          "memory_mb": memory_mb, "cpu_seconds": max(1, int(math.ceil(timeout))),
                          "max_output_chars": max_output_chars})
    loop = asyncio.get_running_loop()
    started = loop.time()
    process, transport, watchdog, completion = None, None, None, None
    tasks = []
    try:
        reader = asyncio.StreamReader(limit=65536)
        protocol = asyncio.StreamReaderProtocol(reader)
        pipe = os.fdopen(read_fd, "rb", buffering=0)
        read_fd = None
        try:
            transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
        except BaseException:
            pipe.close()
            raise
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", "-u", "-c", _RUNNER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=workdir, env=_child_environment(),
            pass_fds=(write_fd,), start_new_session=True)
        os.close(write_fd)
        write_fd = None
        watchdog = asyncio.create_task(_memory_watchdog(process, memory_mb * 1024 * 1024))
        tasks = [asyncio.create_task(_capture_stream(process.stdout, max_output_chars * 4)),
                 asyncio.create_task(_capture_stream(process.stderr, max_output_chars * 4)),
                 asyncio.create_task(_capture_stream(reader, max_output_chars * 24 + 4096)),
                 asyncio.create_task(_wait_and_clean_group(process))]
        completion = asyncio.gather(*tasks)
        process.stdin.write(payload.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        timed_out = False
        try:
            captured = await asyncio.wait_for(asyncio.shield(completion), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_group(process)
            captured = await asyncio.wait_for(asyncio.shield(completion), timeout=5)
        if not watchdog.done():
            watchdog.cancel()
        outcome = (await asyncio.gather(watchdog, return_exceptions=True))[0]
        killed_for_memory = outcome is True
        (stdout, stdout_cut), (stderr, stderr_cut), (raw, raw_cut), exit_code = captured
        out = _clip(stdout.decode("utf-8", "replace"), max_output_chars)
        err = _clip(stderr.decode("utf-8", "replace"), max_output_chars)
        result = {"ok": False, "stdout": out, "stderr": err,
                  "exit_code": exit_code, "duration": round(loop.time() - started, 3),
                  "timed_out": timed_out and not killed_for_memory,
                  "truncated": stdout_cut or stderr_cut or len(stdout.decode("utf-8", "replace")) > max_output_chars
                               or len(stderr.decode("utf-8", "replace")) > max_output_chars,
                  "memory_enforced": outcome in (True, False) and not isinstance(outcome, BaseException)}
        if killed_for_memory:
            result.update(error="代码内存占用超过 %d MiB 上限，已终止进程组" % memory_mb,
                          limit_exceeded="memory")
            return result
        if timed_out:
            result["error"] = "代码执行超过 %.1f 秒，已终止进程组" % timeout
            return result
        try:
            decoded = json.loads(raw) if not raw_cut else None
        except (ValueError, RecursionError):
            decoded = None
        if not isinstance(decoded, dict) or type(decoded.get("ok")) is not bool:
            result["error"] = "子进程异常退出，返回码 %s；未收到完整结构化结果" % exit_code
            return result
        if not decoded["ok"]:
            result["error"] = _clip(str(decoded.get("result", "代码执行失败")), max_output_chars)
            if "MemoryError" in result["error"]:
                result.update(limit_exceeded="memory", error="内存限制触发：" + result["error"])
            return result
        if exit_code != 0:
            result["error"] = "子进程异常退出，返回码 %s" % exit_code
            return result
        value = _safe_value(decoded.get("result"))
        result.update(ok=True, result=value, truncated=result["truncated"] or bool(decoded.get("truncated")))
        return result
    finally:
        if process is not None:
            _kill_group(process)
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if watchdog is not None:
            await asyncio.gather(watchdog, return_exceptions=True)
        if completion is not None:
            await asyncio.gather(completion, return_exceptions=True)
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (Exception, asyncio.CancelledError):
                pass
        if transport is not None:
            transport.close()
        for descriptor in (write_fd, read_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if temporary:
            shutil.rmtree(workdir, ignore_errors=True)


def _clip(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（输出已截断，原始 %d 字符）" % len(text)


def _kill_group(process):
    """终止整个进程组；失败时退化为终止单个进程。"""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
