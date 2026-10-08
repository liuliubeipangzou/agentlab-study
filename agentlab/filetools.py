"""工作区文件工具：分页读取、写入、追加、精确编辑、glob 与 grep。

所有路径都是工作区内的相对路径，逐层用目录描述符加 O_NOFOLLOW 打开，不跟随任何符号链接。
写入先落到同目录临时文件再原子替换，并保留已有文件的权限位。

两类路径对模型不可见：
- ``.git/`` 下的一切（hooks 与 config 能让后续的 git 命令执行任意代码，请改用 git 工具）；
- ``.env`` 与 ``.env.*``（通常含密钥，读取后会进入模型请求），``.env.example`` 等模板除外。
可通过 ``ToolContext.settings["protected_paths"]`` 追加 fnmatch 风格的模式。
这是教学级防线，不是操作系统沙箱：``run_shell`` 与 ``run_python`` 不受它约束。
"""
import difflib
import fnmatch
import json
import os
import re
import secrets
import stat
import sys
from collections import namedtuple
from pathlib import Path

MAX_FILE_BYTES = 256 * 1024          # 整文件读取与 write_file 的上限
MAX_EDIT_BYTES = 2 * 1024 * 1024     # edit_file / append_file 能处理的文件大小
MAX_PAGED_BYTES = 8 * 1024 * 1024    # 分页读取的文件大小上限
MAX_LINE_CHARS = 2000                # 分页读取时单行最多显示的字符数
DEFAULT_PAGE_LINES = 2000
MAX_PAGE_LINES = 5000
MAX_SEARCH_FILE_BYTES = 1024 * 1024  # grep 跳过更大的文件
GREP_TIMEOUT = 20.0                  # 子进程里执行 grep 的时限（秒）
MAX_WALK_ENTRIES = 20000
MAX_WALK_DEPTH = 20

# 遍历时整体跳过的目录：依赖、缓存与版本库内部，几乎从来不是用户想搜的内容。
IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
                          ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".agentlab"})
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_Ctx = namedtuple("_Ctx", "workspace")


# ---- 受保护路径 ----

def _extra_patterns(settings):
    if isinstance(settings, dict):
        patterns = settings.get("protected_paths")
        if isinstance(patterns, (list, tuple)):
            return [p for p in patterns if isinstance(p, str) and p]
    return []


def protected_reason(relative_path, extra=()):
    """返回路径被保护的原因；不受保护时返回 None。"""
    parts = [part for part in relative_path.split("/") if part]
    if not parts:
        return None
    if ".git" in parts:
        return "文件工具不访问 .git 目录；版本库操作请使用 git 工具"
    name = parts[-1]
    if (name == ".env" or name.startswith(".env.")) and name not in _ENV_TEMPLATES:
        return "受保护的文件（通常含密钥）：" + relative_path
    for pattern in extra:
        if fnmatch.fnmatchcase(relative_path, pattern) or fnmatch.fnmatchcase(name, pattern):
            return "路径被配置为受保护：" + relative_path
    return None


def _check_path(relative_path, settings=None):
    path = Path(relative_path)
    if (not relative_path or "\x00" in relative_path or path.is_absolute()
            or any(part in ("..", ".") for part in relative_path.split("/"))
            or "\\" in relative_path or not path.name):
        raise ValueError("path must be a relative workspace path without '.', '..' or backslashes")
    reason = protected_reason(relative_path, _extra_patterns(settings))
    if reason:
        raise ValueError(reason)
    return path


def open_parent(context, relative_path, create=False):
    """使用目录描述符逐层打开，拒绝工作区内任何符号链接。

    O_NOFOLLOW 与 dir_fd 避免先检查再跟随链接的常见竞态。工作区须为可信的
    本机目录；这不防御其他进程重命名已打开的父目录或挂载点。
    """
    path = _check_path(relative_path, getattr(context, "settings", None))
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise RuntimeError("secure file tools require POSIX O_NOFOLLOW and dir_fd support")
    workspace = Path(context.workspace).expanduser().absolute()
    if workspace.is_symlink():
        raise ValueError("workspace must not be a symlink")
    workspace = workspace.resolve(strict=True)
    parent_fd = os.open(str(workspace), _DIR_FLAGS)
    try:
        for component in path.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            child_fd = os.open(component, _DIR_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd, path.name
    except BaseException:
        os.close(parent_fd)
        raise


def _regular_stat(parent_fd, filename):
    try:
        target = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(target.st_mode):
        raise ValueError("refuses symlinks and non-regular files")
    return target


def _read_bytes(parent_fd, filename, limit, what="file"):
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("%s accepts regular files only" % what)
        if info.st_size > limit:
            raise ValueError("file exceeds %d byte limit" % limit)
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file exceeds %d byte limit" % limit)
        return data, info


def _atomic_replace(parent_fd, filename, data, mode):
    """同目录临时文件 + 原子替换；不会跟随已有文件链接。"""
    temporary = ".agentlab-" + secrets.token_hex(12) + ".tmp"
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=parent_fd)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        created = False
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


# ---- 读取 ----

def _paged_read(arguments, context):
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", DEFAULT_PAGE_LINES)
    parent_fd, filename = open_parent(context, arguments["path"])
    try:
        data, _ = _read_bytes(parent_fd, filename, MAX_PAGED_BYTES, "read_file")
    finally:
        os.close(parent_fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("文件不是 UTF-8 文本，无法分页读取") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    budget = max(1000, int(getattr(context, "max_output_chars", 8000)) - 1500)
    used, taken = 0, []
    for number in range(offset, min(total, offset + limit - 1) + 1):
        line = lines[number - 1]
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "…（本行已截断）"
        rendered = "%6d\t%s" % (number, line)
        cost = len(json.dumps(rendered, ensure_ascii=False))
        if taken and used + cost > budget:
            break
        taken.append(rendered)
        used += cost
    end = offset + len(taken) - 1
    result = {"path": arguments["path"], "start_line": offset, "end_line": end if taken else offset - 1,
              "total_lines": total, "content": "\n".join(taken),
              "next_offset": end + 1 if taken and end < total else None}
    if offset > total:
        result["note"] = "offset 超过文件总行数 %d" % total
    return result


def read_file(arguments, context):
    """不带 offset/limit 时读取整个文件（≤256 KiB）；带任一参数则按行分页，文件可达 8 MiB。"""
    if "offset" in arguments or "limit" in arguments:
        return _paged_read(arguments, context)
    parent_fd, filename = open_parent(context, arguments["path"])
    try:
        try:
            data, _ = _read_bytes(parent_fd, filename, MAX_FILE_BYTES, "read_file")
        except ValueError as error:
            if "exceeds" in str(error):
                raise ValueError("文件超过 %d 字节；请用 offset/limit 参数按行分页读取" % MAX_FILE_BYTES) from None
            raise
        return data.decode("utf-8")
    finally:
        os.close(parent_fd)


# ---- 写入 ----

def write_file(arguments, context):
    """整文件写入；已有文件保留权限位。"""
    content = arguments["content"].encode("utf-8")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("content exceeds %d byte limit" % MAX_FILE_BYTES)
    parent_fd, filename = open_parent(context, arguments["path"], create=True)
    try:
        try:
            target = _regular_stat(parent_fd, filename)
        except ValueError:
            raise ValueError("write_file refuses symlinks and non-regular files") from None
        mode = stat.S_IMODE(target.st_mode) if target is not None else 0o600
        _atomic_replace(parent_fd, filename, content, mode)
        return {"path": arguments["path"], "bytes_written": len(content)}
    finally:
        os.close(parent_fd)


def append_file(arguments, context):
    """在文件末尾追加内容（不存在则创建）。长内容可拆成多次追加，避免单次输出被截断。"""
    addition = arguments["content"].encode("utf-8")
    parent_fd, filename = open_parent(context, arguments["path"], create=True)
    try:
        try:
            target = _regular_stat(parent_fd, filename)
        except ValueError:
            raise ValueError("append_file refuses symlinks and non-regular files") from None
        existing = b""
        mode = 0o600
        if target is not None:
            existing, _ = _read_bytes(parent_fd, filename, MAX_EDIT_BYTES, "append_file")
            mode = stat.S_IMODE(target.st_mode)
        combined = existing + addition
        if len(combined) > MAX_EDIT_BYTES:
            raise ValueError("追加后文件将超过 %d 字节上限" % MAX_EDIT_BYTES)
        _atomic_replace(parent_fd, filename, combined, mode)
        return {"path": arguments["path"], "bytes_appended": len(addition), "total_bytes": len(combined)}
    finally:
        os.close(parent_fd)


def _line_numbers(text, needle, limit=5):
    numbers, start = [], 0
    while len(numbers) < limit:
        index = text.find(needle, start)
        if index < 0:
            break
        numbers.append(text.count("\n", 0, index) + 1)
        start = index + max(1, len(needle))
    return numbers


def edit_file(arguments, context):
    """精确字符串替换。old_string 必须在文件中唯一（除非 replace_all），避免改错位置。"""
    old, new = arguments["old_string"], arguments["new_string"]
    replace_all = bool(arguments.get("replace_all", False))
    if not old:
        raise ValueError("old_string 不能为空；要创建文件请用 write_file")
    if old == new:
        raise ValueError("old_string 与 new_string 相同，没有可修改的内容")
    parent_fd, filename = open_parent(context, arguments["path"])
    try:
        try:
            data, info = _read_bytes(parent_fd, filename, MAX_EDIT_BYTES, "edit_file")
        except FileNotFoundError:
            raise ValueError("文件不存在：%s（新建文件请用 write_file）" % arguments["path"]) from None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("文件不是 UTF-8 文本，无法编辑") from None
        count = text.count(old)
        if count == 0:
            raise ValueError("在文件中没有找到 old_string；请先用 read_file 查看当前内容，"
                             "并保证缩进、空白和换行与原文完全一致")
        if count > 1 and not replace_all:
            raise ValueError("old_string 在文件中出现 %d 次（行号 %s）；请补充上下文使其唯一，"
                             "或设置 replace_all=true" % (count, ", ".join(map(str, _line_numbers(text, old)))))
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        encoded = updated.encode("utf-8")
        if len(encoded) > MAX_EDIT_BYTES:
            raise ValueError("编辑后文件将超过 %d 字节上限" % MAX_EDIT_BYTES)
        _atomic_replace(parent_fd, filename, encoded, stat.S_IMODE(info.st_mode))
        diff = "\n".join(difflib.unified_diff(
            text.splitlines(), updated.splitlines(), "a/" + arguments["path"], "b/" + arguments["path"],
            n=3, lineterm=""))
        if len(diff) > 6000:
            diff = diff[:6000] + "\n…（diff 已截断）"
        return {"path": arguments["path"], "replacements": count if replace_all else 1,
                "bytes_written": len(encoded), "diff": diff}
    finally:
        os.close(parent_fd)


# ---- 遍历、glob、grep ----

def _expand_braces(pattern, limit=50):
    start = pattern.find("{")
    if start < 0:
        return [pattern]
    depth, parts, last = 0, [], start + 1
    for index in range(start, len(pattern)):
        char = pattern[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parts.append(pattern[last:index])
                head, tail = pattern[:start], pattern[index + 1:]
                expanded = []
                for part in parts:
                    expanded.extend(_expand_braces(head + part + tail, limit))
                    if len(expanded) > limit:
                        raise ValueError("glob 花括号展开过多")
                return expanded
        elif char == "," and depth == 1:
            parts.append(pattern[last:index])
            last = index + 1
    return [pattern]


def _glob_regex(pattern):
    out, index, size = [], 0, len(pattern)
    while index < size:
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                index += 2
                if pattern[index:index + 1] == "/":
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 2)
            if end < 0:
                out.append(re.escape(char))
            else:
                body = pattern[index + 1:end]
                negate = body.startswith("!") or body.startswith("^")
                body = body[1:] if negate else body
                out.append("[" + ("^" if negate else "") + body.replace("\\", "\\\\") + "]")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def compile_glob(pattern):
    """glob → 正则。不含 / 的模式匹配任意深度的文件名；支持 **、?、[...] 与 {a,b}。"""
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 500:
        raise ValueError("pattern 必须是 1 到 500 字符的非空字符串")
    pattern = pattern.strip()
    if pattern.startswith("./"):
        pattern = pattern[2:]
    branches = []
    for alternative in _expand_braces(pattern):
        if "/" not in alternative:
            alternative = "**/" + alternative
        branches.append(_glob_regex(alternative))
    try:
        return re.compile("^(?:" + "|".join(branches) + ")$")
    except re.error:
        raise ValueError("无效的 glob 模式：" + pattern) from None


def _split_dir(path):
    if path in ("", None, "."):
        return []
    parts = path.strip("/").split("/")
    if (any(part in ("", ".", "..") for part in parts) or "\\" in path or "\x00" in path
            or path.startswith("/")):
        raise ValueError("path 必须是工作区内的相对目录")
    return parts


def iter_files(workspace, start="", extra=(), budget=None):
    """深度优先产出 start 目录下的普通文件相对路径（相对 start，POSIX 分隔符，按名称排序）。

    不跟随符号链接，跳过 IGNORED_DIRS 与受保护文件。budget 是 {"visited": n, "truncated": bool}，
    用于限制遍历规模。
    """
    budget = budget if budget is not None else {"visited": 0, "truncated": False}
    parts = _split_dir(start)
    if protected_reason("/".join(parts), extra):
        raise ValueError(protected_reason("/".join(parts), extra))
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise RuntimeError("secure file tools require POSIX O_NOFOLLOW and dir_fd support")
    fd = os.open(str(Path(workspace).expanduser().resolve(strict=True)), _DIR_FLAGS)
    try:
        for part in parts:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError:
        os.close(fd)
        raise ValueError("目录不存在或不可访问：" + "/".join(parts)) from None

    base = "/".join(parts)

    def scan(directory_fd, prefix, depth):
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if budget["visited"] >= MAX_WALK_ENTRIES:
                budget["truncated"] = True
                return
            budget["visited"] += 1
            name = entry.name
            relative = prefix + name
            full = (base + "/" + relative) if base else relative
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name in IGNORED_DIRS or depth >= MAX_WALK_DEPTH or protected_reason(full, extra):
                        continue
                    try:
                        child = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
                    except OSError:
                        continue
                    try:
                        yield from scan(child, relative + "/", depth + 1)
                    finally:
                        os.close(child)
                elif entry.is_file(follow_symlinks=False):
                    if name.startswith(".agentlab-") and name.endswith(".tmp"):
                        continue
                    if protected_reason(full, extra):
                        continue
                    yield relative
            except OSError:
                continue

    try:
        yield from scan(fd, "", 0)
    finally:
        os.close(fd)


def glob_files(arguments, context):
    regex = compile_glob(arguments["pattern"])
    limit = arguments.get("limit", 200)
    extra = _extra_patterns(getattr(context, "settings", None))
    budget = {"visited": 0, "truncated": False}
    matches, more = [], False
    for relative in iter_files(context.workspace, arguments.get("path", ""), extra, budget):
        if regex.match(relative):
            if len(matches) >= limit:
                more = True
                break
            matches.append(relative)
    base = arguments.get("path", "").strip("/")
    if base:
        matches = [base + "/" + item for item in matches]
    return {"count": len(matches), "matches": matches, "truncated": more or budget["truncated"],
            "note": "已跳过 .git、node_modules、虚拟环境、缓存目录与受保护文件。"}


def grep_tree(payload):
    """在工作区内搜索文本。纯函数，供子进程执行器调用。"""
    flags = re.IGNORECASE if payload.get("ignore_case") else 0
    source = re.escape(payload["pattern"]) if payload.get("fixed") else payload["pattern"]
    try:
        regex = re.compile(source, flags)
    except re.error as error:
        raise ValueError("无效的正则表达式：%s（若要按字面文本搜索，请设置 fixed=true）" % error) from None
    selector = compile_glob(payload["glob"]) if payload.get("glob") else None
    extra = payload.get("extra_protected") or []
    context_lines = payload.get("context", 0)
    limit = payload.get("limit", 100)
    base = (payload.get("path") or "").strip("/")
    budget = {"visited": 0, "truncated": False}
    workspace_ctx = _Ctx(payload["workspace"])
    matches, searched, more = [], 0, False
    for relative in iter_files(payload["workspace"], base, extra, budget):
        if selector is not None and not selector.match(relative):
            continue
        full = (base + "/" + relative) if base else relative
        try:
            parent_fd, filename = open_parent(workspace_ctx, full)
        except (ValueError, OSError):
            continue
        try:
            data, _ = _read_bytes(parent_fd, filename, MAX_SEARCH_FILE_BYTES, "grep")
        except (ValueError, OSError):
            continue
        finally:
            os.close(parent_fd)
        if b"\x00" in data[:8000]:
            continue
        searched += 1
        lines = data.decode("utf-8", "replace").split("\n")
        for index, line in enumerate(lines):
            shown = line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"
            if regex.search(shown):
                if len(matches) >= limit:
                    more = True
                    break
                item = {"path": full, "line": index + 1, "text": shown}
                if context_lines:
                    item["before"] = [x[:MAX_LINE_CHARS] for x in lines[max(0, index - context_lines):index]]
                    item["after"] = [x[:MAX_LINE_CHARS] for x in lines[index + 1:index + 1 + context_lines]]
                matches.append(item)
        if more:
            break
    return {"count": len(matches), "matches": matches, "files_searched": searched,
            "truncated": more or budget["truncated"]}


# 在独立子进程里执行正则：模型给出的模式可能引发灾难性回溯，线程无法被中断，子进程可以被杀掉。
_GREP_RUNNER = """
import json, sys
sys.path.insert(0, %r)
from agentlab import filetools
payload = json.loads(sys.stdin.read())
try:
    print(json.dumps({"ok": True, "result": filetools.grep_tree(payload)}, ensure_ascii=False))
except (ValueError, OSError) as error:
    print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
"""


async def grep_files(arguments, context):
    from . import pysandbox
    from .tools import ToolExecutionError
    if len(arguments["pattern"]) > 500:
        raise ValueError("pattern 不能超过 500 字符")
    payload = {"workspace": str(Path(context.workspace).expanduser().resolve()),
               "pattern": arguments["pattern"], "fixed": bool(arguments.get("fixed", False)),
               "ignore_case": bool(arguments.get("ignore_case", False)),
               "glob": arguments.get("glob"), "path": arguments.get("path", ""),
               "context": arguments.get("context", 0), "limit": arguments.get("limit", 100),
               "extra_protected": _extra_patterns(getattr(context, "settings", None))}
    runner = _GREP_RUNNER % str(Path(__file__).resolve().parent.parent)
    outcome = await pysandbox.run_command(
        [sys.executable, "-I", "-B", "-c", runner], cwd=context.workspace, timeout=GREP_TIMEOUT, memory_mb=1024,
        max_output_chars=200000, stdin_text=json.dumps(payload))
    if outcome.get("timed_out"):
        raise ValueError("搜索超过 %g 秒已终止：目录太大或正则过于复杂。请缩小 path/glob，或设置 fixed=true 按字面搜索"
                         % GREP_TIMEOUT)
    try:
        decoded = json.loads(outcome["stdout"])
    except (ValueError, KeyError):
        raise ToolExecutionError("搜索进程异常退出", outcome) from None
    if not decoded.get("ok"):
        raise ValueError(decoded.get("error") or "搜索失败")
    return decoded["result"]
