"""工具协议、参数校验和有边界的内置工具。

扩展方式：注册 ``Tool(..., handler=my_handler)``，处理器接收 ``(arguments,
context)``，可以是 async 函数或普通函数。普通函数在线程中运行；超时只停止
等待，不能终止 Python 线程。因此会产生副作用的处理器应优先使用可取消的
async 实现，并自行保证原子性。这里的限制是教学防线，不是操作系统沙箱。
"""
import ast
import asyncio
import copy
import inspect
import json
import math
import operator
import os
from pathlib import Path
import re
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Callable, Dict

from .types import ToolCall


MAX_FILE_BYTES = 256 * 1024
MAX_DEPTH = 20
_SCHEMA_KEYS = {
    "type", "description", "title", "default", "properties", "required",
    "additionalProperties", "items", "enum", "minimum", "maximum",
    "minLength", "maxLength", "minItems", "maxItems",
}
_JSON_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


@dataclass
class ToolContext:
    """每次调用的能力上下文：工作目录、记忆库和会话身份。"""

    workspace: Path
    memory: Any = None
    session_id: str = ""
    max_output_chars: int = 8000


@dataclass
class Tool:
    """工具定义；write 风险级别必须得到调用方的显式批准。"""

    name: str
    description: str
    parameters: dict
    handler: Callable
    risk: str = "read"
    timeout: float = 10.0


def _json_value(value: Any, path: str = "$", depth: int = 0) -> None:
    """拒绝非 JSON 值及 NaN/Infinity，避免对象绕过参数校验。"""
    if depth > MAX_DEPTH:
        raise ValueError("%s: nesting exceeds %d levels" % (path, MAX_DEPTH))
    if value is None or type(value) in (str, bool):
        return
    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise ValueError("%s: number must be finite" % path)
        if type(value) is int and value.bit_length() > 4096:
            raise ValueError("%s: integer is too large" % path)
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json_value(item, "%s[%d]" % (path, index), depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("%s: object keys must be strings" % path)
            _json_value(item, "%s.%s" % (path, key), depth + 1)
        return
    raise ValueError("%s: unsupported JSON value %s" % (path, type(value).__name__))


def _check_schema(schema: dict, path: str = "$", depth: int = 0) -> None:
    if type(schema) is not dict:
        raise ValueError("%s: schema must be an object" % path)
    if depth > MAX_DEPTH:
        raise ValueError("schema nesting is too deep")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise ValueError("%s: unsupported schema keywords: %s" % (path, sorted(unknown)))
    if "type" in schema and (type(schema["type"]) is not str or schema["type"] not in _JSON_TYPES):
        raise ValueError("%s: schema type must be a supported single type" % path)
    if "properties" in schema:
        if type(schema["properties"]) is not dict:
            raise ValueError("%s: properties must be an object" % path)
        for name, item in schema["properties"].items():
            if type(name) is not str:
                raise ValueError("schema property names must be strings")
            _check_schema(item, path + "." + name, depth + 1)
    if "required" in schema:
        required = schema["required"]
        if (type(required) is not list or any(type(x) is not str for x in required)
                or len(required) != len(set(required))):
            raise ValueError("%s: required must be a list of unique names" % path)
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if type(extra) is dict:
            _check_schema(extra, path + ".*", depth + 1)
        elif type(extra) is not bool:
            raise ValueError("additionalProperties must be boolean or a schema")
    if "items" in schema:
        _check_schema(schema["items"], path + "[]", depth + 1)
    if "enum" in schema:
        if type(schema["enum"]) is not list or not schema["enum"]:
            raise ValueError("enum must be a non-empty list")
        _json_value(schema["enum"])
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError("%s must be a non-negative integer" % key)
    for key in ("minimum", "maximum"):
        if key in schema:
            if type(schema[key]) not in (int, float):
                raise ValueError("%s must be a finite number" % key)
            _json_value(schema[key])
    for low, high in (("minimum", "maximum"), ("minLength", "maxLength"),
                      ("minItems", "maxItems")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ValueError("%s must not exceed %s" % (low, high))


def _enum_equal(left: Any, right: Any) -> bool:
    """JSON 中 true 和 1 不是同一个枚举值。"""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(
            _enum_equal(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_enum_equal(a, b) for a, b in zip(left, right))
    return left == right


def _validate(value: Any, schema: dict, path: str = "$") -> None:
    kind = schema.get("type")
    matches = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "number": type(value) in (int, float),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if kind is not None and not matches[kind]:
        raise ValueError("%s: expected %s, got %s" % (path, kind, type(value).__name__))
    if "enum" in schema and not any(_enum_equal(value, x) for x in schema["enum"]):
        raise ValueError("%s: value is not in the allowed enum" % path)
    if type(value) is dict:
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ValueError("%s: missing required property '%s'" % (path, name))
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], path + "." + name)
            else:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    raise ValueError("%s: unexpected property '%s'" % (path, name))
                if type(extra) is dict:
                    _validate(item, extra, path + "." + name)
    elif type(value) is list:
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise ValueError("%s: array length is outside allowed bounds" % path)
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), "%s[%d]" % (path, index))
    elif type(value) is str:
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ValueError("%s: string length is outside allowed bounds" % path)
    elif type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError("%s: number is outside allowed bounds" % path)


def validate_schema(value: Any, schema: dict) -> None:
    """校验本模块支持的 JSON Schema 子集；失败抛 ValueError，成功返回 None。

    可用于工具参数、模型结构化输出和计划。未知关键字直接拒绝，避免把未实现
    的约束误当作已经生效。数字不接收 bool，也拒绝 NaN、Infinity 和超深嵌套。
    """
    _json_value(schema)
    _check_schema(schema)
    _json_value(value)
    _validate(value, schema)


class ToolRegistry:
    """工具白名单；模型提供的工具名、参数和批准状态都在这里检查。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("register expects a Tool")
        if type(tool.name) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name):
            raise ValueError("tool name must contain 1-64 letters, digits, underscores or hyphens")
        if type(tool.description) is not str:
            raise ValueError("tool description must be a string")
        if tool.name in self._tools:
            raise ValueError("tool already registered: %s" % tool.name)
        if tool.risk not in ("read", "write"):
            raise ValueError("risk must be 'read' or 'write'")
        if (type(tool.timeout) not in (int, float) or not math.isfinite(tool.timeout)
                or tool.timeout <= 0):
            raise ValueError("timeout must be a positive finite number")
        if not callable(tool.handler):
            raise ValueError("tool handler must be callable")
        _json_value(tool.parameters)
        _check_schema(tool.parameters)
        if tool.parameters.get("type") != "object":
            raise ValueError("tool parameters must declare type: object")
        self._tools[tool.name] = Tool(
            name=tool.name, description=tool.description,
            parameters=copy.deepcopy(tool.parameters), handler=tool.handler,
            risk=tool.risk, timeout=tool.timeout)

    def get(self, name: str) -> Tool:
        """返回工具定义的副本，避免外部修改注册表的权限和参数规则。"""
        tool = self._tools[name]
        return Tool(tool.name, tool.description, copy.deepcopy(tool.parameters),
                    tool.handler, tool.risk, tool.timeout)

    def definitions(self) -> list:
        return [{"type": "function", "function": {
            "name": tool.name, "description": tool.description,
            "parameters": copy.deepcopy(tool.parameters),
        }} for tool in self._tools.values()]

    def requires_approval(self, call: ToolCall) -> bool:
        if type(call.name) is not str:
            return False
        tool = self._tools.get(call.name)
        return tool is not None and tool.risk == "write"

    async def execute(self, call: ToolCall, context: ToolContext, approved: bool = False) -> dict:
        """失败作为结构化结果返回；取消信号继续向上传播。"""
        try:
            if type(context.max_output_chars) is not int or context.max_output_chars < 128:
                raise ValueError("max_output_chars must be an integer >= 128")
            if type(call.name) is not str:
                raise ValueError("tool name must be a string")
            tool = self._tools.get(call.name)
            if tool is None:
                raise ValueError("Unknown tool: %s" % call.name)
            if tool.risk == "write" and approved is not True:
                raise PermissionError("Approval required for write tool: %s" % call.name)
            _json_value(call.arguments)
            _validate(call.arguments, tool.parameters)
            arguments = copy.deepcopy(call.arguments)

            async def invoke() -> Any:
                if inspect.iscoroutinefunction(tool.handler):
                    return await tool.handler(arguments, context)
                result = await asyncio.to_thread(tool.handler, arguments, context)
                if inspect.isawaitable(result):
                    return await result
                return result

            value = await asyncio.wait_for(invoke(), timeout=tool.timeout)
            _json_value(value)
            rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(rendered) > context.max_output_chars:
                # 将预览也按 JSON 编码后的长度截断；引号和控制字符会增加长度。
                result = {"truncated": True, "preview": "", "original_chars": len(rendered)}
                low, high = 0, min(len(rendered), context.max_output_chars)
                while low < high:
                    midpoint = (low + high + 1) // 2
                    result["preview"] = rendered[:midpoint]
                    if len(json.dumps(result, ensure_ascii=False)) <= context.max_output_chars:
                        low = midpoint
                    else:
                        high = midpoint - 1
                result["preview"] = rendered[:low]
                value = result
            return {"ok": True, "value": value}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Tool '%s' timed out after %ss" % (tool.name, tool.timeout)}
        except Exception as error:
            message = "%s: %s" % (type(error).__name__, error)
            limit = context.max_output_chars if type(context.max_output_chars) is int else 8000
            return {"ok": False, "error": message[:max(128, limit)]}


def _safe_number(number: Any) -> Any:
    if type(number) not in (int, float) or not math.isfinite(number) or abs(number) > 1e100:
        raise ValueError("calculator accepts finite real values with magnitude <= 1e100")
    return number


async def _calculator(arguments: dict, context: ToolContext) -> Any:
    """仅解释数字 AST；不使用 eval，不允许函数、属性和变量访问。"""
    expression = arguments["expression"]
    tree = ast.parse(expression.strip(), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("calculator expression is too complex (maximum 64 AST nodes)")
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return _safe_number(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
            value = visit(node.operand)
            return _safe_number(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if type(node.op) is ast.Pow:
                if abs(right) > 100:
                    raise ValueError("calculator exponent magnitude must be <= 100")
                if left != 0 and math.log10(abs(left)) * right > 100:
                    raise ValueError("calculator power result would be too large")
                return _safe_number(operator.pow(left, right))
            if type(node.op) in binary:
                return _safe_number(binary[type(node.op)](left, right))
        raise ValueError("calculator allows only numbers, parentheses and + - * / // % **")

    return visit(tree)


def _open_parent(context: ToolContext, relative_path: str, create: bool = False):
    """使用目录描述符逐层打开，拒绝工作区内任何符号链接。

    O_NOFOLLOW 与 dir_fd 避免先检查再跟随链接的常见竞态。工作区须为可信的
    本机目录；这不防御其他进程重命名已打开的父目录或挂载点。
    """
    path = Path(relative_path)
    if (not relative_path or "\x00" in relative_path or path.is_absolute()
            or any(part in ("..", ".") for part in relative_path.split("/"))
            or "\\" in relative_path or not path.name):
        raise ValueError("path must be a relative workspace path without '.', '..' or backslashes")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise RuntimeError("secure file tools require POSIX O_NOFOLLOW and dir_fd support")
    workspace = Path(context.workspace).expanduser().absolute()
    if workspace.is_symlink():
        raise ValueError("workspace must not be a symlink")
    workspace = workspace.resolve(strict=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(str(workspace), flags)
    try:
        for component in path.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd, path.name
    except BaseException:
        os.close(parent_fd)
        raise


def _read_file(arguments: dict, context: ToolContext) -> str:
    """读取有大小上限的 UTF-8 普通文件；避免跟随符号链接和 FIFO。"""
    parent_fd, filename = _open_parent(context, arguments["path"])
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("read_file accepts regular files only")
            if file_stat.st_size > MAX_FILE_BYTES:
                raise ValueError("file exceeds %d byte limit" % MAX_FILE_BYTES)
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("file exceeds %d byte limit" % MAX_FILE_BYTES)
            return data.decode("utf-8")
    finally:
        os.close(parent_fd)


def _write_file(arguments: dict, context: ToolContext) -> dict:
    """同目录临时文件 + 原子替换；不会跟随已有文件链接。"""
    content = arguments["content"].encode("utf-8")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("content exceeds %d byte limit" % MAX_FILE_BYTES)
    parent_fd, filename = _open_parent(context, arguments["path"], create=True)
    temporary = ".agentlab-" + secrets.token_hex(12) + ".tmp"
    created = False
    try:
        try:
            target_stat = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            raise ValueError("write_file refuses symlinks and non-regular files")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=parent_fd)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        created = False
        return {"path": arguments["path"], "bytes_written": len(content)}
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


async def _memory_call(context: ToolContext, method: str, *args, **kwargs) -> Any:
    if context.memory is None:
        raise ValueError("memory is not configured for this tool context")
    callback = getattr(context.memory, method)
    if inspect.iscoroutinefunction(callback):
        return await callback(*args, **kwargs)
    # 内置 SQLite 记忆默认使用创建连接的线程；其短操作留在当前线程。
    result = callback(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


async def _search(arguments: dict, context: ToolContext) -> Any:
    return await _memory_call(context, "search", arguments["query"], limit=arguments.get("limit", 5))


async def _remember(arguments: dict, context: ToolContext) -> dict:
    await _memory_call(context, "remember", context.session_id, arguments["key"], arguments["value"])
    return {"remembered": arguments["key"]}


async def _recall(arguments: dict, context: ToolContext) -> Any:
    return await _memory_call(context, "recall", context.session_id, arguments["query"])


def _parameters(properties: dict, required: list) -> dict:
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


def create_builtin_tools() -> ToolRegistry:
    """创建教学工具集：计算、受限文件操作、知识检索和会话记忆。"""
    registry = ToolRegistry()
    registry.register(Tool("calculator", "计算数字表达式，支持 + - * / // % ** 和括号。",
        _parameters({"expression": {"type": "string", "minLength": 1, "maxLength": 256}},
                    ["expression"]), _calculator))
    path = {"type": "string", "minLength": 1, "maxLength": 1024,
            "description": "工作区内的相对文件路径，不允许符号链接。"}
    registry.register(Tool("read_file", "读取工作区内的 UTF-8 文件，最大 256 KiB。",
        _parameters({"path": path}, ["path"]), _read_file))
    registry.register(Tool("write_file", "写入工作区内的 UTF-8 文件并创建父目录，需要确认。",
        _parameters({"path": path, "content": {"type": "string", "maxLength": MAX_FILE_BYTES}},
                    ["path", "content"]), _write_file, risk="write"))
    registry.register(Tool("search_knowledge", "从本地知识库搜索相关文档片段。",
        _parameters({"query": {"type": "string", "minLength": 1, "maxLength": 2000},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                    ["query"]), _search))
    registry.register(Tool("remember", "将键值记忆保存到当前会话，需要确认。",
        _parameters({"key": {"type": "string", "minLength": 1, "maxLength": 200},
                     "value": {"type": "string", "maxLength": 8000}}, ["key", "value"]),
        _remember, risk="write"))
    registry.register(Tool("recall", "按关键词读取当前会话的键值记忆，空查询读取全部。",
        _parameters({"query": {"type": "string", "maxLength": 2000}}, ["query"]), _recall))
    return registry
