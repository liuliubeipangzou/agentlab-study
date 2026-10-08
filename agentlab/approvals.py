"""审批策略：一次需要审批的调用，是自动放行还是询问用户；以及给审批界面用的预览。

三种模式：

- ``ask``：逐次询问，只有用户记住的规则可以放行（库的默认值）；
- ``auto-workspace``：工作区内的文件与记忆写入（``write``）自动放行，``run_shell`` 里不含 shell
  元字符、不指向工作区之外、且在默认清单中的命令（查看类命令与测试/构建命令）自动放行；
  其余的 ``exec``、``network_write``、``destructive`` 仍然询问；
- ``trust``：全部自动放行，包括 ``destructive``。只在用户显式开启时使用。

任何模式下，文件工具对 ``.git/`` 与 ``.env*`` 的保护都照常生效——那是工具自身的硬边界，不属于审批。

两个必须知道的事实：
1. 自动放行测试/构建命令，意味着 Agent 刚写下的代码可以不经确认地被执行。要避免，使用 ``ask`` 模式，
   或在设置里取消不想要的默认命令。
2. ``destructive`` 永远不能被“记住”：规则不会匹配它，只有 ``trust`` 模式会放行。
"""
import difflib
import json
import shlex
import uuid
from collections import namedtuple
from typing import Optional
from urllib.parse import urlsplit

from . import filetools

MODES = ("ask", "auto-workspace", "trust")

RISK_LABELS = {"read": "只读", "write": "写入工作区", "exec": "执行命令/修改仓库",
               "network_write": "向外部写入", "destructive": "难以撤销"}

# 含这些字符的命令可以串联、重定向或展开变量，不做自动放行，也不能被规则匹配。
_SHELL_META = frozenset(";&|`$<>()\n\r\\")

# 默认自动放行的命令（按 token 前缀匹配）：查看类命令，以及测试/构建/静态检查。
DEFAULT_ALLOWED_COMMANDS = (
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "tree", "stat", "file", "diff", "sort", "uniq",
    "which", "echo", "date", "du", "df",
    "pytest", "python -m pytest", "python3 -m pytest", "python -m unittest", "python3 -m unittest",
    "python --version", "python3 --version", "node --version",
    "npm test", "npm run test", "npm run lint", "npm run build", "npm run typecheck",
    "cargo test", "cargo check", "cargo build", "cargo clippy", "cargo fmt --check",
    "go test", "go build", "go vet", "make test", "make check", "make lint",
)

# 这些命令的第二个词才是真正的动作，记住规则时取前两个词（python -m x 取前三个）。
_MULTI_WORD = frozenset({"git", "npm", "npx", "python", "python3", "cargo", "go", "make", "pip", "pip3",
                         "yarn", "pnpm", "docker", "kubectl"})

# 单个词就能做任何事的命令：即使用户批准过一次，也不提供“总是允许”，否则等于放行整类操作。
_BLANKET_UNSAFE = frozenset({"curl", "wget", "ssh", "scp", "rsync", "nc", "find", "xargs", "eval", "exec", "sh",
                             "bash", "zsh", "env", "node", "perl", "ruby", "awk", "sed", "mv", "cp", "chmod",
                             "chown", "kill", "pkill", "killall", "open", "tar", "unzip", "ln", "touch", "tee"})

Decision = namedtuple("Decision", "allow source rule_id")
_Ctx = namedtuple("_Ctx", "workspace settings")


def parse_command(command):
    """把命令拆成 token；含 shell 元字符、引号不平衡或为空时返回 None。"""
    if not isinstance(command, str) or not command.strip() or len(command) > 8000:
        return None
    if any(ch in _SHELL_META for ch in command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    return tokens or None


def _escapes_workspace(token):
    for part in (token, token.split("=", 1)[-1]):
        if part.startswith(("/", "~")) or ".." in part.split("/"):
            return True
    return False


def command_tokens(command):
    """可用于自动放行/规则匹配的命令 token；不安全时返回 None。"""
    tokens = parse_command(command)
    if tokens is None or any(_escapes_workspace(token) for token in tokens):
        return None
    return tokens


def _has_prefix(tokens, prefix):
    return bool(prefix) and tokens[:len(prefix)] == list(prefix)


def rule_matches(rule, call, risk):
    """用户记住的规则是否放行这次调用。destructive 永远不匹配。"""
    if risk in ("read", "destructive") or not isinstance(rule, dict) or rule.get("tool") != call.name:
        return False
    match = rule.get("match") or {}
    kind, arguments = match.get("kind"), call.arguments
    if kind == "tool":
        return risk == "write"
    if kind == "command_prefix":
        tokens = command_tokens(arguments.get("command"))
        prefix = match.get("prefix")
        return tokens is not None and isinstance(prefix, list) and _has_prefix(tokens, prefix)
    if kind == "git_sub":
        args = arguments.get("args")
        return isinstance(args, list) and bool(args) and args[0] == match.get("sub")
    if kind == "host":
        try:
            return urlsplit(str(arguments.get("url", ""))).hostname == match.get("host")
        except ValueError:
            return False
    return False


def suggest_rule(call, risk):
    """“总是允许此类操作”会记住的规则；无法安全地归纳时返回 None（此时界面不提供该选项）。"""
    if risk in ("read", "destructive"):
        return None
    arguments = call.arguments
    if call.name == "run_shell":
        tokens = command_tokens(arguments.get("command"))
        if tokens is None:
            return None
        size = 1
        if tokens[0] in _MULTI_WORD:
            # python -c / npm --foo 这类以选项开头的写法没有稳定的“动作词”，归纳成裸命令会放得太宽。
            if len(tokens) == 1 or (tokens[1].startswith("-") and not (tokens[1] == "-m" and len(tokens) > 2)):
                return None
            size = 3 if tokens[1] in ("-m", "run") and len(tokens) > 2 else 2  # python -m pytest、npm run build
        elif tokens[0] in _BLANKET_UNSAFE:
            return None
        prefix = tokens[:size]
        return {"tool": call.name, "match": {"kind": "command_prefix", "prefix": prefix},
                "label": "以 “%s” 开头的命令" % " ".join(prefix)}
    if call.name == "git":
        args = arguments.get("args")
        if isinstance(args, list) and args and isinstance(args[0], str):
            return {"tool": "git", "match": {"kind": "git_sub", "sub": args[0]},
                    "label": "git %s（不含破坏性选项）" % args[0]}
        return None
    if call.name == "http_request":
        try:
            host = urlsplit(str(arguments.get("url", ""))).hostname
        except ValueError:
            host = None
        return {"tool": call.name, "match": {"kind": "host", "host": host},
                "label": "对 %s 的写请求" % host} if host else None
    if risk == "write":
        return {"tool": call.name, "match": {"kind": "tool"}, "label": "所有 %s 操作" % call.name}
    return None


def new_rule(suggestion):
    return dict(suggestion, id="r_" + uuid.uuid4().hex[:10])


class ApprovalPolicy:
    """决定一次需要审批的调用能否自动放行。无状态，规则由调用方传入或从 store 读取。"""

    def __init__(self, mode="ask", store=None, allowed_commands=DEFAULT_ALLOWED_COMMANDS):
        if mode not in MODES:
            raise ValueError("approval_mode 必须是 %s 之一" % "、".join(MODES))
        self.mode = mode
        self.store = store
        self.allowed = [command.split() for command in allowed_commands]

    def global_rules(self):
        lister = getattr(self.store, "list_approval_rules", None)
        return lister() if callable(lister) else []

    def decide(self, call, risk, session_rules=()):
        if risk == "read":
            return Decision(True, "read", None)
        if self.mode == "trust":
            return Decision(True, "mode:trust", None)
        if self.mode == "auto-workspace":
            if risk == "write":
                return Decision(True, "mode:auto-workspace", None)
            if call.name == "run_shell" and risk == "exec":
                tokens = command_tokens(call.arguments.get("command"))
                if tokens is not None and any(_has_prefix(tokens, prefix) for prefix in self.allowed):
                    return Decision(True, "default-command", None)
        if risk != "destructive":
            for rule in list(session_rules) + list(self.global_rules()):
                if rule_matches(rule, call, risk):
                    return Decision(True, "rule:" + str(rule.get("id")), rule.get("id"))
        return Decision(False, "", None)


# ---- 预览 ----

def _diff(old, new, name, limit=6000):
    text = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "a/" + name, "b/" + name,
                                          n=3, lineterm=""))
    return text if len(text) <= limit else text[:limit] + "\n…（diff 已截断）"


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit] + "\n…（已截断，共 %d 字符）" % len(text)


def build_preview(call, workspace):
    """审批界面展示的内容：文件改动显示 diff，命令与代码原样显示。读取失败时退回到参数 JSON。"""
    arguments, name = call.arguments, call.name
    context = _Ctx(workspace, None)
    try:
        if name == "write_file":
            path, content = arguments["path"], arguments["content"]
            try:
                current = filetools.read_file({"path": path}, context)
            except (FileNotFoundError, ValueError, OSError):
                current = None
            if current is None:
                return {"kind": "text", "title": "新建文件 %s（%d 字符）" % (path, len(content)),
                        "text": _clip(content, 3000)}
            return {"kind": "diff", "title": "覆盖写入 " + path,
                    "text": _diff(current, content, path) or "（内容没有变化）"}
        if name == "edit_file":
            path, old, new = arguments["path"], arguments["old_string"], arguments["new_string"]
            current = filetools.read_file({"path": path}, context)
            count = current.count(old)
            if count == 0:
                return {"kind": "text", "title": "编辑 " + path, "text": "（当前文件中没有找到 old_string，这次编辑会失败）"}
            if count > 1 and not arguments.get("replace_all"):
                return {"kind": "text", "title": "编辑 " + path,
                        "text": "（old_string 出现了 %d 次且未设置 replace_all，这次编辑会失败）" % count}
            updated = current.replace(old, new) if arguments.get("replace_all") else current.replace(old, new, 1)
            return {"kind": "diff", "title": "编辑 " + path, "text": _diff(current, updated, path)}
        if name == "append_file":
            return {"kind": "text", "title": "追加到 " + arguments["path"], "text": _clip(arguments["content"], 3000)}
        if name == "run_shell":
            where = arguments.get("cwd") or "工作区根目录"
            return {"kind": "command", "title": "在 %s 执行（超时 %s 秒）" % (where, arguments.get("timeout", 120)),
                    "text": arguments["command"]}
        if name == "run_python":
            return {"kind": "code", "title": "执行 Python 代码（超时 %s 秒）" % arguments.get("timeout", 20),
                    "text": _clip(arguments["code"], 6000)}
        if name == "git":
            return {"kind": "command", "title": "git", "text": "git " + shlex.join(arguments["args"])}
        if name == "http_request":
            body = arguments.get("body") or ""
            return {"kind": "text", "title": "%s %s" % (arguments.get("method", "GET"), arguments["url"]),
                    "text": _clip(body, 1500) if body else "（没有请求体）"}
    except (KeyError, TypeError, AttributeError, ValueError, OSError):
        pass
    return {"kind": "json", "title": name, "text": _clip(json.dumps(arguments, ensure_ascii=False, indent=2), 4000)}
