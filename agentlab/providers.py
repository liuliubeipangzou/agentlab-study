"""模型适配器：离线规则演示、脚本测试，以及 OpenAI 兼容 Chat Completions。

HTTP 使用标准库；阻塞请求由 asyncio.to_thread 隔离。协程被取消时，已经开始的
网络线程无法被强行终止，仍可能运行至 socket timeout，且请求可能已产生费用。
"""
import asyncio
import ipaddress
import json
import math
import os
import shlex
import socket
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from typing import List

from .types import Message, ModelResponse, ToolCall, Usage


class ProviderError(RuntimeError):
    """可向用户显示的适配器错误；不包含密钥或远端响应正文。"""


class ProviderConfigurationError(ProviderError):
    """模型连接配置无效。"""


class _RetryableError(ProviderError):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class DemoProvider:
    """确定性规则模拟器，不是 LLM。适合离线观察工具执行、记忆与事件流。"""

    async def complete(self, messages: List[Message], tools: List[dict]) -> ModelResponse:
        latest = next((i for i in range(len(messages) - 1, -1, -1)
                       if messages[i].role == "user"), None)
        if latest is None:
            return ModelResponse(content="离线 Demo 已就绪。输入 /help 查看学习命令。")
        turn = messages[latest + 1:]
        results = [m.content for m in turn if m.role == "tool"]
        if results:
            return ModelResponse(content="离线 Demo 工具执行结果：\n" + "\n".join(results))
        # If a tool call exists without a result, do not issue it a second time.
        if any(m.tool_calls for m in turn if m.role == "assistant"):
            return ModelResponse(content="离线 Demo：等待工具结果，本轮不会重复调用。")
        command = messages[latest].content.strip()
        head, _, rest = command.partition(" ")
        name = None
        arguments = {}
        if head == "/calc" and rest.strip():
            name, arguments = "calculator", {"expression": rest.strip()}
        elif head == "/search" and rest.strip():
            name, arguments = "search_knowledge", {"query": rest.strip(), "limit": 5}
        elif head == "/recall" and rest.strip():
            name, arguments = "recall", {"query": rest.strip()}
        elif head in ("/read", "/write", "/remember"):
            try:
                parts = shlex.split(rest)
            except ValueError:
                return ModelResponse(content="命令引号未闭合；路径包含空格时请用双引号包围。")
            if head == "/read" and len(parts) == 1:
                name, arguments = "read_file", {"path": parts[0]}
            elif head == "/write" and len(parts) >= 2:
                name, arguments = "write_file", {"path": parts[0], "content": " ".join(parts[1:])}
            elif head == "/remember" and len(parts) >= 2:
                name, arguments = "remember", {"key": parts[0], "value": " ".join(parts[1:])}
        if name:
            available = {t.get("function", t).get("name") for t in tools}
            if name not in available:
                return ModelResponse(content="离线 Demo：当前未注册工具 " + name + "。")
            return ModelResponse(tool_calls=[ToolCall(name=name, arguments=arguments)])
        return ModelResponse(content=(
            "当前为离线 Demo：这是确定性规则模拟器，不是真实大语言模型。\n"
            "建议按“消息 → 模型决策 → 工具执行 → 工具结果 → 最终回复”观察 Agent 循环。\n"
            "可用命令：\n"
            "  /calc (2 + 3) * 4\n"
            "  /search Agent 工具调用\n"
            "  /read notes.txt\n"
            "  /write notes.txt 学习笔记（需要写入权限）\n"
            "  /remember goal 学会构建 Agent\n"
            "  /recall goal\n"
            "如需开放式问答，请配置 API Key 并选择 openai provider。"
        ))


class ScriptedProvider:
    """依次返回预设响应；calls 保存参数快照，便于检查 Agent 协议。"""

    def __init__(self, responses: List[ModelResponse]):
        self.responses = list(responses)
        self.calls = []
        self._index = 0

    async def complete(self, messages: List[Message], tools: List[dict]) -> ModelResponse:
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        if self._index >= len(self.responses):
            raise ProviderError("ScriptedProvider 没有剩余的预设响应。")
        result = deepcopy(self.responses[self._index])
        self._index += 1
        return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_nonfinite(value):
    raise ValueError("JSON must be finite")


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _strict_json(text):
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON must be finite")
        return number
    return json.loads(text, parse_constant=_reject_nonfinite,
                      parse_float=finite_float, object_pairs_hook=_object_without_duplicates)


def _valid_identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in value))


class OpenAICompatibleProvider:
    """OpenAI Chat Completions 的小型 HTTP 适配器（不依赖厂商 SDK）。

    仅对连接错误、429、5xx 重试，最多 max_retries 次，每次退避最多 5 秒。
    所有重定向均禁止，防止 Authorization 被转发至其他主机。
    """

    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(self, model: str, api_key: str,
                 base_url: str = "https://api.openai.com/v1", timeout: float = 30,
                 max_retries: int = 2, max_output_tokens: int = 2048):
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigurationError("请设置非空模型名 AGENTLAB_MODEL。")
        if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise ProviderConfigurationError("请设置有效的 AGENTLAB_API_KEY。")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ProviderConfigurationError("timeout 必须为有限正数。")
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise ProviderConfigurationError("max_retries 必须是 0 到 5 之间的整数。")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ProviderConfigurationError("max_output_tokens 必须是正整数。")
        try:
            url = urllib.parse.urlsplit(base_url)
            hostname = url.hostname
            _ = url.port  # Force malformed port validation.
            local = hostname == "localhost"
            if hostname:
                try:
                    local = local or ipaddress.ip_address(hostname).is_loopback
                except ValueError:
                    pass
            if (not hostname or url.username is not None or url.password is not None
                    or url.query or url.fragment or any(c.isspace() for c in base_url)
                    or not (url.scheme == "https" or (url.scheme == "http" and local))):
                raise ValueError("unsafe URL")
        except (ValueError, TypeError, AttributeError):
            raise ProviderConfigurationError("base_url 需要 HTTPS；仅 localhost/回环地址允许 HTTP，且不可包含认证、查询或片段。") from None
        self.model = model.strip()
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens

    async def complete(self, messages: List[Message], tools: List[dict]) -> ModelResponse:
        try:
            payload = self._serialize(messages, tools)
        except (TypeError, ValueError, KeyError, AttributeError):
            raise ProviderError("请求包含无效消息、工具定义或非有限 JSON 值。") from None
        for attempt in range(self.max_retries + 1):
            try:
                data = await asyncio.to_thread(self._request, payload)
                return self._parse(data)
            except _RetryableError as exc:
                if attempt == self.max_retries:
                    raise ProviderError(str(exc)) from None
                delay = min(5.0, max(0.25 * (2 ** attempt), exc.retry_after))
                await asyncio.sleep(delay)
        raise ProviderError("模型请求失败。")  # Defensive; loop always returns or raises.

    def _serialize(self, messages, tools):
        serialized = []
        for message in messages:
            if message.role not in {"system", "user", "assistant", "tool"} or not isinstance(message.content, str):
                raise ValueError("Invalid message")
            item = {"role": message.role, "content": message.content}
            if message.tool_calls:
                if message.role != "assistant":
                    raise ValueError("Only assistant can call tools")
                calls = []
                ids = set()
                for call in message.tool_calls:
                    if not _valid_identifier(call.id) or call.id in ids or not _valid_identifier(call.name) or not isinstance(call.arguments, dict):
                        raise ValueError("Invalid tool call")
                    ids.add(call.id)
                    calls.append({"id": call.id, "type": "function", "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False, allow_nan=False),
                    }})
                item["tool_calls"] = calls
            if message.role == "tool":
                if not _valid_identifier(message.tool_call_id):
                    raise ValueError("Tool result needs call ID")
                item["tool_call_id"] = message.tool_call_id
            serialized.append(item)
        payload = {"model": self.model, "messages": serialized,
                   "max_tokens": self.max_output_tokens}
        if urllib.parse.urlsplit(self.base_url).hostname == "api.deepseek.com":
            # DeepSeek 默认开启 thinking。当前消息协议不保存 reasoning_content，
            # 显式使用官方非思考模式，保证工具结果回传后的下一轮请求合法。
            payload["thinking"] = {"type": "disabled"}
        if tools:
            serialized_tools = []
            names = set()
            for tool in tools:
                function = tool.get("function", tool)
                if (not isinstance(function, dict) or not _valid_identifier(function.get("name"))
                        or function["name"] in names or not isinstance(function.get("parameters"), dict)):
                    raise ValueError("Invalid tool definition")
                names.add(function["name"])
                serialized_tools.append({"type": "function", "function": function})
            payload["tools"] = serialized_tools
            payload["tool_choice"] = "auto"
        return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def _request(self, payload: bytes):
        request = urllib.request.Request(self.base_url + "/chat/completions", data=payload,
                                         headers={"Authorization": "Bearer " + self._api_key,
                                                  "Content-Type": "application/json", "Accept": "application/json"},
                                         method="POST")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise ProviderError("模型响应超过 4 MiB 限制。")
            return _strict_json(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = exc.code
            retry_after = 0.0
            if exc.headers:
                try:
                    candidate = float(exc.headers.get("Retry-After", "0"))
                    retry_after = min(5.0, max(0.0, candidate)) if math.isfinite(candidate) else 0.0
                except (ValueError, TypeError):
                    pass
            exc.close()
            if code == 429 or 500 <= code <= 599:
                raise _RetryableError("模型 API 暂时不可用（HTTP %d）；请稍后重试。" % code, retry_after) from None
            if code in (401, 403):
                raise ProviderError("模型 API 认证或访问权限失败（HTTP %d）；请检查配置。" % code) from None
            if 300 <= code <= 399:
                raise ProviderError("已阻止模型 API HTTP 重定向；请配置最终 HTTPS 地址。") from None
            raise ProviderError("模型 API 拒绝请求（HTTP %d）。" % code) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise _RetryableError("无法连接模型 API 或请求超时；请检查网络与地址。") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("模型 API 返回无效 JSON。") from None

    def _parse(self, data):
        try:
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                raise ValueError("Missing choices")
            choice = data["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("Missing assistant message")
            message = choice["message"]
            if message.get("role", "assistant") != "assistant":
                raise ValueError("Invalid role")
            content = message.get("content")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise ValueError("Invalid content")
            raw_calls = message.get("tool_calls", [])
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list):
                raise ValueError("Invalid calls")
            calls, ids = [], set()
            for raw in raw_calls:
                if not isinstance(raw, dict) or raw.get("type") != "function" or not _valid_identifier(raw.get("id")):
                    raise ValueError("Invalid call")
                if raw["id"] in ids or not isinstance(raw.get("function"), dict):
                    raise ValueError("Duplicate ID or invalid function")
                ids.add(raw["id"])
                function = raw["function"]
                if not _valid_identifier(function.get("name")) or not isinstance(function.get("arguments"), str):
                    raise ValueError("Invalid function")
                arguments = _strict_json(function["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Arguments must be object")
                # Covers overflowing JSON exponents (1e999), not just NaN/Infinity tokens.
                json.dumps(arguments, allow_nan=False)
                calls.append(ToolCall(id=raw["id"], name=function["name"], arguments=arguments))
            usage_data = data.get("usage", {})
            if usage_data is None:
                usage_data = {}
            if not isinstance(usage_data, dict):
                raise ValueError("Invalid usage")
            incoming, outgoing = usage_data.get("prompt_tokens", 0), usage_data.get("completion_tokens", 0)
            if type(incoming) is not int or type(outgoing) is not int or incoming < 0 or outgoing < 0:
                raise ValueError("Invalid token count")
            # Never execute truncated tool arguments even when the JSON happens to parse.
            if choice.get("finish_reason") == "length":
                raise ProviderError("模型响应达到输出上限；请提高 max_output_tokens 或缩短请求。")
            if choice.get("finish_reason") == "content_filter":
                raise ProviderError("模型 API 未提供可用响应（content_filter）。")
            if not content and not calls:
                raise ValueError("Empty response")
            return ModelResponse(content=content, tool_calls=calls,
                                 usage=Usage(input_tokens=incoming, output_tokens=outgoing))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ProviderError("模型 API 响应结构无效；需要 Chat Completions 文本或函数工具调用。") from None


def provider_from_env(provider: str = "demo"):
    """创建 provider；demo 完全离线，openai 显式要求模型名及 API Key。"""
    if provider == "demo":
        return DemoProvider()
    if provider in ("openai", "openai-compatible"):
        return OpenAICompatibleProvider(
            model=os.environ.get("AGENTLAB_MODEL", ""),
            api_key=os.environ.get("AGENTLAB_API_KEY", ""),
            base_url=os.environ.get("AGENTLAB_BASE_URL", "https://api.openai.com/v1"),
        )
    raise ProviderConfigurationError("未知 provider；请选择 demo 或 openai。")
