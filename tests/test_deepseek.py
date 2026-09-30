"""DeepSeek non-thinking compatibility; fake credentials and no HTTP requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentlab.agent import Agent
from agentlab.providers import OpenAICompatibleProvider
from agentlab.storage import SQLiteStore
from agentlab.types import Message


def response(content="Mock answer", tool_calls=None, incoming=3, outgoing=2):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": incoming, "completion_tokens": outgoing},
    }


class DeepSeekCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, base_url):
        return OpenAICompatibleProvider(
            model="deepseek-flash", api_key="unit-test-placeholder-not-a-real-key",
            base_url=base_url, max_retries=0,
        )

    async def test_agent_tool_roundtrip_disables_thinking_on_both_requests(self):
        provider = self.provider("https://api.deepseek.com")
        call_id = "call_deepseek_calc_1"
        tool_response = response(None, [{
            "id": call_id, "type": "function",
            "function": {"name": "calculator", "arguments": '{"expression":"6 * 7"}'},
        }], incoming=7, outgoing=3)
        final_response = response("6 × 7 = 42。", incoming=4, outgoing=2)
        with tempfile.TemporaryDirectory(prefix="agentlab-deepseek-test-") as temporary:
            with SQLiteStore(":memory:") as store:
                agent = Agent(provider, store=store, workspace=Path(temporary) / "work")
                # Mock the transport below complete(), preserving request serialization,
                # response parsing, the real Agent state machine and calculator tool.
                with patch.object(provider, "_request", side_effect=[tool_response, final_response]) as transport, \
                        patch("agentlab.providers.urllib.request.build_opener", side_effect=AssertionError("Network forbidden in tests")):
                    result = await agent.run("请计算 6 × 7")
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.output, "6 × 7 = 42。")
                self.assertEqual((result.steps, result.tool_calls), (2, 1))
                self.assertEqual(result.usage.total_tokens, 16)
                self.assertEqual(transport.call_count, 2)
                requests = [json.loads(call.args[0]) for call in transport.call_args_list]
                for request in requests:
                    self.assertEqual(request["thinking"], {"type": "disabled"})
                    self.assertEqual(request["model"], "deepseek-flash")
                    self.assertEqual(request["tool_choice"], "auto")
                self.assertFalse(any(message["role"] == "tool" for message in requests[0]["messages"]))
                assistant = requests[1]["messages"][-2]
                tool = requests[1]["messages"][-1]
                self.assertEqual(assistant["role"], "assistant")
                self.assertEqual(assistant["tool_calls"][0]["id"], call_id)
                self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "calculator")
                self.assertEqual(tool["role"], "tool")
                self.assertEqual(tool["tool_call_id"], call_id)
                self.assertEqual(json.loads(tool["content"]), {"ok": True, "value": 42})

    async def test_official_root_and_v1_use_flash_with_thinking_disabled(self):
        for base_url in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            with self.subTest(base_url=base_url):
                provider = self.provider(base_url)
                with patch.object(provider, "_request", return_value=response()) as transport, \
                        patch("agentlab.providers.urllib.request.build_opener", side_effect=AssertionError("Network forbidden in tests")):
                    result = await provider.complete([Message("user", "hello")], [])
                request = json.loads(transport.call_args.args[0])
                self.assertEqual(request["model"], "deepseek-flash")
                self.assertEqual(request["thinking"], {"type": "disabled"})
                self.assertEqual(result.content, "Mock answer")
                self.assertEqual(provider.base_url, base_url)

    async def test_other_hosts_never_receive_deepseek_specific_fields(self):
        base_urls = (
            "https://api.openai.com/v1",
            "http://localhost:8000/v1",
            "https://api.deepseek.com.evil.example/v1",
            "https://sub.api.deepseek.com/v1",
            "https://example.test/api.deepseek.com",
        )
        for base_url in base_urls:
            with self.subTest(base_url=base_url):
                # The same model name intentionally proves this is scoped by the
                # exact endpoint hostname rather than model name or URL substrings.
                provider = self.provider(base_url)
                with patch.object(provider, "_request", return_value=response()) as transport, \
                        patch("agentlab.providers.urllib.request.build_opener", side_effect=AssertionError("Network forbidden in tests")):
                    await provider.complete([Message("user", "hello")], [])
                request = json.loads(transport.call_args.args[0])
                self.assertNotIn("thinking", request)


if __name__ == "__main__":
    unittest.main()
