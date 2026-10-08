"""Provider contract tests; all HTTP is mocked, no credentials or paid requests."""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import AsyncMock, MagicMock, patch

from agentlab.providers import (DemoProvider, OpenAICompatibleProvider,
                                ProviderConfigurationError, ProviderError,
                                ScriptedProvider, _NoRedirect, provider_from_env)
from agentlab.types import Message, ModelResponse, ToolCall


def definition(name="calculator"):
    return {"name": name, "description": "test", "parameters": {"type": "object"}}


def answer(content="你好", calls=None, **extra):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    result = {"choices": [{"message": message, "finish_reason": "stop"}]}
    result.update(extra)
    return result


def call_data(identifier="call_1", name="calculator", arguments='{"expression":"1+2"}'):
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://example.test", code, "secret-key-server-echo",
                                  headers or {}, io.BytesIO(b"secret-key-server-body"))


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_and_tool_availability(self):
        cases = [
            ("/calc (2 + 3) * 4", "calculator", {"expression": "(2 + 3) * 4"}),
            ("/search Agent", "search_knowledge", {"query": "Agent", "limit": 5}),
            ('/read "hello world.txt"', "read_file", {"path": "hello world.txt"}),
            ('/write "hello world.txt" Hello Python', "write_file", {"path": "hello world.txt", "content": "Hello Python"}),
            ("/remember goal learn agent", "remember", {"key": "goal", "value": "learn agent"}),
            ("/recall goal", "recall", {"query": "goal"}),
        ]
        demo = DemoProvider()
        for command, name, arguments in cases:
            with self.subTest(command=command):
                response = await demo.complete([Message("user", command)], [definition(name)])
                self.assertEqual(response.tool_calls[0].name, name)
                self.assertEqual(response.tool_calls[0].arguments, arguments)
        missing = await demo.complete([Message("user", "/calc 1+1")], [])
        self.assertFalse(missing.tool_calls)
        self.assertIn("未注册", missing.content)

    async def test_latest_turn_only_and_no_repeat(self):
        messages = [Message("user", "/calc 1+1"),
                    Message("assistant", tool_calls=[ToolCall("calculator", {"expression": "1+1"})]),
                    Message("tool", "2", tool_call_id="old"),
                    Message("assistant", "2"), Message("user", "/calc 3+3")]
        demo = DemoProvider()
        response = await demo.complete(messages, [definition()])
        self.assertEqual(response.tool_calls[0].arguments, {"expression": "3+3"})
        messages.append(Message("assistant", tool_calls=response.tool_calls))
        self.assertFalse((await demo.complete(messages, [definition()])).tool_calls)
        messages.append(Message("tool", "6", tool_call_id=response.tool_calls[0].id))
        final = await demo.complete(messages, [definition()])
        self.assertIn("6", final.content)
        self.assertNotIn("2", final.content)

    async def test_guide_is_honest_and_bad_input_is_helpful(self):
        demo = DemoProvider()
        self.assertIn("不是真实", (await demo.complete([Message("user", "你好")], [])).content)
        self.assertIn("引号", (await demo.complete([Message("user", '/read "unterminated')], [])).content)
        self.assertIn("就绪", (await demo.complete([], [])).content)

    async def test_scripted_snapshots_and_exhaustion(self):
        provider = ScriptedProvider([ModelResponse("first")])
        messages = [Message("user", "hi")]
        self.assertEqual((await provider.complete(messages, [])).content, "first")
        messages[0].content = "changed"
        self.assertEqual(provider.calls[0]["messages"][0].content, "hi")
        with self.assertRaises(ProviderError):
            await provider.complete(messages, [])


class APIProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = OpenAICompatibleProvider("test-model", "secret-test-key", max_retries=2)

    async def invoke(self, response, messages=None, tools=None):
        transport = MagicMock(return_value=response)
        with patch.object(self.provider, "_request", transport):
            result = await self.provider.complete(messages or [Message("user", "你好")], tools or [])
        return result, transport

    async def test_serializes_round_trip_tool_protocol_and_usage(self):
        request_call = ToolCall("calculator", {"expression": "1+2"}, "call_a")
        messages = [Message("system", "help"), Message("user", "calculate"),
                    Message("assistant", tool_calls=[request_call]),
                    Message("tool", "3", tool_call_id="call_a")]
        response, transport = await self.invoke(
            answer(None, [call_data(), call_data("call_2", "recall", '{"query":"goal"}')],
                   usage={"prompt_tokens": 100, "completion_tokens": 20}),
            messages, [definition(), {"type": "function", "function": definition("recall")}])
        self.assertEqual(response.usage.total_tokens, 120)
        self.assertEqual(len(response.tool_calls), 2)
        self.assertEqual(response.tool_calls[1].arguments, {"query": "goal"})
        payload = json.loads(transport.call_args[0][0])
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["messages"][2]["tool_calls"][0]["id"], "call_a")
        self.assertEqual(payload["messages"][3]["tool_call_id"], "call_a")
        self.assertEqual(json.loads(payload["messages"][2]["tool_calls"][0]["function"]["arguments"]), {"expression": "1+2"})

    async def test_invalid_responses_rejected_before_execution(self):
        bad = [None, [], {}, {"choices": []}, answer(42), answer(None),
               answer(None, [call_data(arguments="[]")]),
               answer(None, [call_data(arguments='{"value":NaN}')]),
               answer(None, [call_data(arguments='{"value":1e999}')]),
               answer(None, [call_data(arguments='{"x":1,"x":2}')]),
               answer(None, [call_data(identifier="")]),
               answer(None, [call_data(identifier="bad\nidentifier")]),
               answer(None, [call_data(), call_data()]),
               answer("ok", usage=[]), answer("ok", usage={"prompt_tokens": True}),
               answer("ok", usage={"completion_tokens": -1})]
        truncated = answer(None, [call_data()])
        truncated["choices"][0]["finish_reason"] = "length"
        bad.append(truncated)
        for response in bad:
            with self.subTest(response=response):
                with self.assertRaises(ProviderError):
                    await self.invoke(response)

    async def test_nonfinite_outbound_values_rejected_without_request(self):
        with patch.object(self.provider, "_request") as request:
            with self.assertRaises(ProviderError):
                await self.provider.complete([Message("assistant", tool_calls=[ToolCall("calculator", {"x": float("nan")})])], [])
            request.assert_not_called()

    async def test_null_optional_tool_calls_and_usage_are_accepted(self):
        data = answer("done", usage=None)
        data["choices"][0]["message"]["tool_calls"] = None
        response, _ = await self.invoke(data)
        self.assertEqual(response.content, "done")
        self.assertEqual(response.usage.total_tokens, 0)

    async def test_retry_429_and_500_then_success(self):
        opener = MagicMock()
        opener.open.side_effect = [http_error(429, {"Retry-After": "99999"}),
                                   http_error(503), io.BytesIO(json.dumps(answer()).encode())]
        with patch("agentlab.providers.urllib.request.build_opener", return_value=opener), \
                patch("agentlab.providers.asyncio.sleep", new_callable=AsyncMock) as sleep:
            response = await self.provider.complete([Message("user", "hi")], [])
        self.assertEqual(response.content, "你好")
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [30.0, 2.0])

    async def test_retry_exhaustion_is_bounded_and_sanitized(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("secret-test-key-private-url")
        with patch("agentlab.providers.urllib.request.build_opener", return_value=opener), \
                patch("agentlab.providers.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(ProviderError) as raised:
                await self.provider.complete([Message("user", "hi")], [])
        self.assertEqual(opener.open.call_count, 3)
        self.assertNotIn("secret", str(raised.exception))

    async def test_http_failures_do_not_retry_or_echo_server_body(self):
        for status in (400, 401, 403, 404, 302, 307):
            with self.subTest(status=status):
                opener = MagicMock()
                opener.open.side_effect = http_error(status)
                with patch("agentlab.providers.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(ProviderError) as raised:
                        await self.provider.complete([Message("user", "hi")], [])
                self.assertEqual(opener.open.call_count, 1)
                self.assertNotIn("secret", str(raised.exception))

    async def test_network_payload_headers_and_json_validation(self):
        for raw in (b"not-json-secret", b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}'):
            with self.subTest(raw=raw):
                opener = MagicMock()
                opener.open.return_value = io.BytesIO(raw)
                with patch("agentlab.providers.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(ProviderError):
                        await self.provider.complete([Message("user", "hi")], [])
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
                self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-key")
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)

    async def test_response_size_limit(self):
        opener = MagicMock()
        opener.open.return_value = io.BytesIO(b"x" * 17)
        with patch.object(self.provider, "MAX_RESPONSE_BYTES", 16), \
                patch("agentlab.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError):
                await self.provider.complete([Message("user", "hi")], [])

    def test_config_validation_and_local_http(self):
        for url in ("http://api.example.test/v1", "ftp://localhost/v1", "https://user:secret@api.test/v1",
                    "https://api.test/v1?secret=x", "https://api.test/v1#fragment", "https://api.test:bad/v1", "https://api.test/ bad"):
            with self.subTest(url=url), self.assertRaises(ProviderConfigurationError):
                OpenAICompatibleProvider("model", "key", base_url=url)
        for url in ("http://localhost:8000/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1", "https://api.example.test/v1"):
            OpenAICompatibleProvider("model", "key", base_url=url)
        for kwargs in ({"timeout": float("nan")}, {"timeout": 0}, {"max_retries": -1}, {"max_retries": 6}, {"max_output_tokens": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderConfigurationError):
                OpenAICompatibleProvider("model", "key", **kwargs)
        with self.assertRaises(ProviderConfigurationError):
            OpenAICompatibleProvider("", "key")
        with self.assertRaises(ProviderConfigurationError):
            OpenAICompatibleProvider("model", "key\nheader")

    def test_redirect_handler_never_creates_followup_request(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    def test_environment_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(provider_from_env(), DemoProvider)
            with self.assertRaises(ProviderConfigurationError):
                provider_from_env("openai")
        with patch.dict(os.environ, {"AGENTLAB_MODEL": "example-model", "AGENTLAB_API_KEY": "example-key", "AGENTLAB_BASE_URL": "http://localhost:11434/v1"}, clear=True):
            provider = provider_from_env("openai")
            self.assertEqual(provider.model, "example-model")
            self.assertEqual(provider.base_url, "http://localhost:11434/v1")
        with self.assertRaises(ProviderConfigurationError):
            provider_from_env("unknown")


if __name__ == "__main__":
    unittest.main()


class FakeStreamResponse:
    """模拟真实 HTTPResponse 的流式读取协议。

    生产代码用 `readline(size)` 读取（而非迭代），这样取消时可以关闭底层 socket
    唤醒阻塞中的读取。fixture 必须实现同一协议，否则测到的是假象。
    """

    def __init__(self, text):
        self._data = (text + "\0").encode("utf-8")
        self._position = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        self.close()
        return False

    def readline(self, size=-1):
        if self._position >= len(self._data):
            return b""
        end = self._data.find(b"\n", self._position)
        end = len(self._data) if end < 0 else end + 1
        if isinstance(size, int) and size > 0:
            end = min(end, self._position + size)
        chunk = self._data[self._position:end]
        self._position = end
        return chunk

    def close(self):
        self.closed = True

    # 兼容仍需迭代的读取路径。
    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line


def sse(*events, usage=None, terminate=True):
    """把若干字典或原始字符串拼成 SSE 文本。

    默认补上 usage 帧与 `[DONE]`：生产代码请求了 include_usage，并在缺少用量时
    判定为不完整流。
    """
    parts = []
    for event in events:
        if isinstance(event, str):
            parts.append(event)
        else:
            parts.append("data: " + json.dumps(event, ensure_ascii=False))
    if usage is None:
        usage = {"prompt_tokens": 3, "completion_tokens": 2}
    if usage is not False:
        parts.append("data: " + json.dumps({"choices": [], "usage": usage}, ensure_ascii=False))
    if terminate:
        parts.append("data: [DONE]")
    return "\n\n".join(parts) + "\n\n"


def delta(content, finish=None):
    return {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}


def tool_fragment(index, identifier=None, name=None, arguments=None, finish=None):
    """构造一个工具调用增量片段；字段省略即表示该片段不携带该字段。"""
    fragment = {"index": index}
    if identifier is not None:
        fragment["id"] = identifier
    function = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    if function:
        fragment["function"] = function
    return {"choices": [{"delta": {"tool_calls": [fragment]}, "finish_reason": finish}]}


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    """流式解析：必须容忍注释、分片与工具调用增量。

    服务端要求 `stream_options.include_usage`，因此 fixture 必须带 usage 帧，
    否则会被判为不完整流。
    """

    def setUp(self):
        self.provider = OpenAICompatibleProvider("test-model", "sk-unit-test-placeholder",
                                                 base_url="https://api.example.test/v1")

    def stream_with(self, text):
        opener = MagicMock()
        opener.open.return_value = FakeStreamResponse(text)
        return patch("agentlab.providers.urllib.request.build_opener", return_value=opener), opener

    async def test_concatenates_deltas_and_reports_usage(self):
        text = sse(delta("你好"), ": keep-alive", delta("，世界"), delta("！", "stop"),
                   {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
                   "data: [DONE]")
        context, _ = self.stream_with(text)
        pieces = []
        with context:
            response = await self.provider.stream([Message("user", "hi")], pieces.append)
        self.assertEqual(pieces, ["你好", "，世界", "！"])
        self.assertEqual(response.content, "你好，世界！")
        self.assertEqual("".join(pieces), response.content)
        self.assertEqual(response.tool_calls, [])
        self.assertEqual((response.usage.input_tokens, response.usage.output_tokens), (11, 7))

    async def test_request_declares_stream_and_omits_tools(self):
        context, opener = self.stream_with(sse(delta("hi", "stop")))
        with context:
            await self.provider.stream([Message("user", "hi")], None)
        payload = json.loads(opener.open.call_args.args[0].data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    async def test_length_finish_reason_is_rejected(self):
        """被截断的流式响应不能当作完整回答。"""
        context, _ = self.stream_with(sse(delta("半句话", "length")))
        with context:
            with self.assertRaises(ProviderError):
                await self.provider.stream([Message("user", "hi")], None)

    async def test_content_filter_is_rejected(self):
        context, _ = self.stream_with(sse(delta("", "content_filter")))
        with context:
            with self.assertRaises(ProviderError):
                await self.provider.stream([Message("user", "hi")], None)

    async def test_empty_stream_is_rejected(self):
        context, _ = self.stream_with(sse(delta("", "stop")))
        with context:
            with self.assertRaises(ProviderError):
                await self.provider.stream([Message("user", "hi")], None)

    async def test_invalid_json_chunk_is_rejected(self):
        context, _ = self.stream_with("data: {not json}\n\n")
        with context:
            with self.assertRaises(ProviderError):
                await self.provider.stream([Message("user", "hi")], None)

    async def test_missing_usage_is_rejected(self):
        """请求了 include_usage 却拿不到用量，说明流不完整。

        这里刻意报错而不是把用量记为 0：静默记 0 会让上层预算统计与实际计费脱节。
        """
        context, _ = self.stream_with(sse(delta("hi", "stop"), usage=False))
        with context:
            with self.assertRaises(ProviderError) as raised:
                await self.provider.stream([Message("user", "hi")], None)
        self.assertIn("用量", str(raised.exception))

    async def test_missing_callback_is_allowed(self):
        context, _ = self.stream_with(sse(delta("ok", "stop")))
        with context:
            response = await self.provider.stream([Message("user", "hi")], None)
        self.assertEqual(response.content, "ok")

    async def test_retry_then_success(self):
        opener = MagicMock()
        opener.open.side_effect = [http_error(503), FakeStreamResponse(sse(delta("恢复", "stop")))]
        with patch("agentlab.providers.urllib.request.build_opener", return_value=opener), \
                patch("agentlab.providers.asyncio.sleep", new_callable=AsyncMock):
            response = await self.provider.stream([Message("user", "hi")], None)
        self.assertEqual(response.content, "恢复")
        self.assertEqual(opener.open.call_count, 2)

    async def test_auth_failure_is_not_retried_and_is_sanitized(self):
        opener = MagicMock()
        opener.open.side_effect = http_error(401)
        with patch("agentlab.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as raised:
                await self.provider.stream([Message("user", "hi")], None)
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn("secret-key-server", str(raised.exception))


class StreamingToolCallTests(unittest.IsolatedAsyncioTestCase):
    """流式工具调用：分片参数必须被完整重组，且不能在流结束前执行。

    工具参数以增量片段到达（`{"que` + `ry": "x"}`），必须按 index 对齐拼接后
    再交给上层；截断或编号不连续都要拒绝。
    """

    def setUp(self):
        self.provider = OpenAICompatibleProvider("test-model", "sk-unit-test-placeholder",
                                                 base_url="https://api.example.test/v1")
        self.tools = [
            {"type": "function", "function": {"name": "calculator",
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "web_search",
                                              "parameters": {"type": "object"}}},
        ]

    def stream_with(self, text):
        opener = MagicMock()
        opener.open.return_value = FakeStreamResponse(text)
        return patch("agentlab.providers.urllib.request.build_opener", return_value=opener), opener

    async def test_single_tool_call_fragments_are_joined(self):
        text = sse(tool_fragment(0, identifier="call_1", name="web_search", arguments='{"que'),
                   tool_fragment(0, arguments='ry": "BM25"}'),
                   tool_fragment(0, finish="tool_calls"))
        context, _ = self.stream_with(text)
        with context:
            response = await self.provider.stream([Message("user", "hi")], None, tools=self.tools)
        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual(call.name, "web_search")
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.arguments, {"query": "BM25"})

    async def test_interleaved_tool_calls_are_kept_separate(self):
        text = sse(tool_fragment(0, identifier="call_a", name="calculator", arguments='{"expression":'),
                   tool_fragment(1, identifier="call_b", name="web_search", arguments='{"query":'),
                   tool_fragment(0, arguments='"1+1"}'),
                   tool_fragment(1, arguments='"x"}'),
                   tool_fragment(0, finish="tool_calls"))
        context, _ = self.stream_with(text)
        with context:
            response = await self.provider.stream([Message("user", "hi")], None, tools=self.tools)
        by_name = {call.name: call for call in response.tool_calls}
        self.assertEqual(set(by_name), {"calculator", "web_search"})
        self.assertEqual(by_name["calculator"].arguments, {"expression": "1+1"})
        self.assertEqual(by_name["web_search"].arguments, {"query": "x"})

    async def test_tools_are_sent_and_include_usage_requested(self):
        text = sse(tool_fragment(0, identifier="c1", name="calculator", arguments="{}", finish="tool_calls"))
        context, opener = self.stream_with(text)
        with context:
            await self.provider.stream([Message("user", "hi")], None, tools=self.tools)
        payload = json.loads(opener.open.call_args.args[0].data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        names = [t["function"]["name"] for t in payload.get("tools", [])]
        self.assertEqual(names, ["calculator", "web_search"])

    async def test_finish_reason_must_match_tool_calls(self):
        """声明 tool_calls 却没有片段（或反之）属于协议不一致。"""
        text = sse(tool_fragment(0, finish="tool_calls"))
        context, _ = self.stream_with(text)
        with context:
            with self.assertRaises(ProviderError):
                await self.provider.stream([Message("user", "hi")], None, tools=self.tools)

    async def test_non_contiguous_tool_index_is_rejected(self):
        text = sse(tool_fragment(0, identifier="a", name="calculator", arguments="{}"),
                   tool_fragment(2, identifier="b", name="web_search", arguments="{}"),
                   tool_fragment(0, finish="tool_calls"))
        context, _ = self.stream_with(text)
        with context:
            with self.assertRaises(ProviderError):
                await self.provider.stream([Message("user", "hi")], None, tools=self.tools)

    async def test_incomplete_stream_without_finish_is_rejected(self):
        """没有 finish_reason 就结束的流不能当作完整响应。"""
        text = sse(tool_fragment(0, identifier="a", name="calculator", arguments="{}"),
                   terminate=True)
        context, _ = self.stream_with(text)
        with context:
            with self.assertRaises(ProviderError):
                await self.provider.stream([Message("user", "hi")], None, tools=self.tools)

    async def test_reader_failure_reports_exception_type(self):
        """底层异常必须保留类型名，否则无从诊断（曾经只报"读取失败"）。"""
        class BrokenResponse(FakeStreamResponse):
            def readline(self, size=-1):
                raise AttributeError("readline is not available")

        opener = MagicMock()
        opener.open.return_value = BrokenResponse("")
        with patch("agentlab.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as raised:
                await self.provider.stream([Message("user", "hi")], None)
        self.assertIn("AttributeError", str(raised.exception))
