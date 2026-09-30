import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agentlab.agent import Agent, AgentConfig, SessionError
from agentlab.providers import ScriptedProvider
from agentlab.storage import SQLiteStore
from agentlab.tools import Tool, ToolRegistry
from agentlab.types import ModelResponse, ToolCall, Usage


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def agent(self, responses, **kwargs):
        return Agent(ScriptedProvider(responses), store=self.store, workspace=Path(self.tmp.name) / "work", **kwargs)

    async def test_tool_loop_and_follow_up(self):
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("calculator", {"expression": "6*7"})]),
                            ModelResponse("42"), ModelResponse("仍是 42")])
        result = await agent.run("计算", "math")
        self.assertEqual((result.status, result.output, result.steps, result.tool_calls), ("completed", "42", 2, 1))
        result = await agent.run("再说一遍", "math")
        self.assertEqual(result.output, "仍是 42")
        state = self.store.load_session("math")
        self.assertEqual([m["role"] for m in state["messages"]], ["user", "assistant", "tool", "assistant", "user", "assistant"])
        self.assertIn("tool_finished", [e["type"] for e in self.store.events("math")])

    async def test_write_checkpoint_survives_restart_and_executes_once(self):
        call = ToolCall("write_file", {"path": "notes.txt", "content": "approved"}, id="write_1")
        agent = self.agent([ModelResponse(tool_calls=[call])])
        result = await agent.run("write", "persist")
        self.assertEqual(result.status, "waiting_approval")
        self.assertFalse((agent.workspace / "notes.txt").exists())
        with self.assertRaises(SessionError):
            await agent.run("another", "persist")
        with self.assertRaises(ValueError):
            await agent.resume("persist", ["wrong_id"])
        restarted = self.agent([ModelResponse("written")])
        result = await restarted.resume("persist", ["write_1"])
        self.assertEqual(result.status, "completed")
        self.assertEqual((agent.workspace / "notes.txt").read_text(), "approved")
        with self.assertRaises(SessionError):
            await restarted.resume("persist", ["write_1"])

    async def test_denied_write_has_no_side_effect(self):
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("write_file", {"path": "no.txt", "content": "no"})]),
                            ModelResponse("取消")])
        first = await agent.run("write")
        last = await agent.resume(first.session_id, [])
        self.assertEqual(last.status, "completed")
        self.assertFalse((agent.workspace / "no.txt").exists())
        messages = self.store.load_session(first.session_id)["messages"]
        self.assertFalse(json.loads(messages[2]["content"])["ok"])

    async def test_approval_id_cannot_be_reused_for_second_write(self):
        agent = self.agent([
            ModelResponse(tool_calls=[ToolCall("write_file", {"path": "first", "content": "yes"}, "same_id")]),
            ModelResponse(tool_calls=[ToolCall("write_file", {"path": "second", "content": "no"}, "same_id")]),
            ModelResponse("done"),
        ])
        first = await agent.run("write", "id-scope")
        result = await agent.resume(first.session_id, ["same_id"])
        self.assertEqual(result.status, "failed")
        self.assertTrue((agent.workspace / "first").exists())
        self.assertFalse((agent.workspace / "second").exists())
        self.assertEqual(self.store.load_session(first.session_id)["decisions"], {})

    async def test_approval_cannot_move_to_different_workspace(self):
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("write_file", {"path": "target", "content": "yes"}, "id")])])
        result = await agent.run("write", "bound")
        other = Agent(ScriptedProvider([ModelResponse("done")]), store=self.store, workspace=Path(self.tmp.name) / "other")
        with self.assertRaises(SessionError):
            await other.resume(result.session_id, ["id"])
        self.assertFalse((other.workspace / "target").exists())
        self.assertEqual(self.store.load_session("bound")["status"], "waiting_approval")

    async def test_invalid_provider_usage_rejected(self):
        for usage in [Usage(-1, 2), Usage(True, 2), Usage(1, 2.5)]:
            agent = self.agent([ModelResponse("invalid", usage=usage)])
            self.assertEqual((await agent.run("bad")).status, "failed")

    async def test_non_json_provider_arguments_do_not_corrupt_checkpoint(self):
        for arguments in [{"value": object()}, {"value": float("nan")}, {1: "not a string key"}]:
            agent = self.agent([ModelResponse(tool_calls=[ToolCall("calculator", arguments)])])
            result = await agent.run("invalid")
            self.assertEqual(result.status, "failed")
            self.assertEqual(self.store.load_session(result.session_id)["pending"], [])

    async def test_tool_error_is_returned_to_model(self):
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("missing", {})]), ModelResponse("无法使用")])
        result = await agent.run("bad tool")
        self.assertEqual(result.status, "completed")
        self.assertFalse(json.loads(self.store.load_session(result.session_id)["messages"][2]["content"])["ok"])

    async def test_budgets_block_tools(self):
        calls = [ToolCall("calculator", {"expression": "1+1"}) for _ in range(2)]
        agent = self.agent([ModelResponse(tool_calls=calls)], config=AgentConfig(max_tool_calls=1))
        result = await agent.run("budget")
        self.assertEqual((result.status, result.tool_calls), ("limited", 0))
        self.assertEqual(len(self.store.load_session(result.session_id)["pending"]), 0)
        agent = self.agent([ModelResponse(tool_calls=calls, usage=Usage(5, 6))], config=AgentConfig(max_total_tokens=10))
        result = await agent.run("tokens")
        self.assertEqual((result.status, result.tool_calls), ("limited", 0))

    async def test_step_limit_and_context_limit(self):
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("calculator", {"expression": "1+1"})])], config=AgentConfig(max_steps=1))
        self.assertEqual((await agent.run("loop")).status, "limited")
        agent = self.agent([], config=AgentConfig(max_context_chars=100))
        self.assertEqual((await agent.run("x" * 200)).status, "limited")

    async def test_concurrent_session_and_cancellation(self):
        entered = asyncio.Event()
        class Slow:
            async def complete(self, messages, tools):
                entered.set()
                await asyncio.sleep(5)
                return ModelResponse("late")
        agent = Agent(Slow(), store=self.store, workspace=self.tmp.name)
        task = asyncio.create_task(agent.run("hello", "shared"))
        await entered.wait()
        with self.assertRaises(SessionError):
            await agent.run("racing", "shared")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.load_session("shared")["status"], "cancelled")
        self.assertTrue(self.store.acquire_session("shared", "next"))

    async def test_timeout(self):
        class Slow:
            async def complete(self, messages, tools):
                await asyncio.sleep(1)
        agent = Agent(Slow(), store=self.store, workspace=self.tmp.name, config=AgentConfig(run_timeout=0.01))
        result = await agent.run("wait")
        self.assertEqual(result.status, "limited")

    async def test_crash_recovery_never_replays_pending_write(self):
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("write_file", {"path": "never", "content": "no"})])])
        result = await agent.run("write", "crash")
        state = self.store.load_session(result.session_id)
        state["status"] = "running"
        state["in_flight"] = state["pending"][0]["id"]
        self.store.save_session("crash", state)
        with self.assertRaises(SessionError):
            await agent.run("new", "crash")
        recovered = agent.recover("crash")
        self.assertEqual(recovered.status, "failed")
        self.assertFalse((agent.workspace / "never").exists())
        self.assertEqual(self.store.load_session("crash")["messages"][-1]["role"], "tool")

    async def test_full_turn_context_trimming(self):
        agent = self.agent([ModelResponse("a" * 300), ModelResponse("done")],
                           config=AgentConfig(system_prompt="system", max_context_chars=500))
        await agent.run("first", "trim")
        await agent.run("second" * 20, "trim")
        # Provider receives only complete turns; persisted history remains complete.
        self.assertEqual(len(self.store.load_session("trim")["messages"]), 4)

    def test_config_validation(self):
        for kwargs in [{"max_steps": 0}, {"max_steps": True}, {"run_timeout": float("inf")}]:
            with self.assertRaises(ValueError):
                AgentConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
