"""Regression cases from the state-machine and data-isolation review."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentlab.agent import Agent, AgentConfig, SessionError
from agentlab.providers import ScriptedProvider
from agentlab.storage import SQLiteStore
from agentlab.tools import Tool, ToolRegistry
from agentlab.types import ModelResponse, ToolCall
from agentlab.workflows import Workflow, WorkflowStep


class WorkflowIsolationReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_output_is_not_visible_to_independent_step(self):
        async def private_branch(inputs):
            return {"private_note": "only for an explicit consumer"}

        async def public_branch(inputs):
            return sorted(inputs)

        result = await Workflow([
            WorkflowStep("private", private_branch),
            WorkflowStep("public", public_branch),
        ], concurrency=1).run({"topic": "agents"})
        self.assertEqual(result.outputs["public"], ["topic"])

    async def test_mutating_step_cannot_change_nested_initial_input(self):
        async def mutate(inputs):
            inputs["settings"]["labels"].append("changed")
            return "done"

        original = {"settings": {"labels": ["original"]}}
        result = await Workflow([WorkflowStep("mutate", mutate)]).run(original)
        self.assertTrue(result.ok)
        self.assertEqual(original, {"settings": {"labels": ["original"]}})
        self.assertEqual(result.outputs["settings"], {"labels": ["original"]})

    async def test_consumer_cannot_mutate_persisted_dependency_output(self):
        async def produce(inputs):
            return {"items": ["evidence"]}

        async def consume(inputs):
            inputs["produce"]["items"].append("consumer-only")
            return inputs["produce"]

        result = await Workflow([
            WorkflowStep("produce", produce),
            WorkflowStep("consume", consume, ["produce"]),
        ]).run()
        self.assertTrue(result.ok)
        self.assertEqual(result.outputs["produce"], {"items": ["evidence"]})
        self.assertEqual(result.outputs["consume"], {"items": ["evidence", "consumer-only"]})

    async def test_retry_receives_clean_nested_input(self):
        snapshots = []

        async def unreliable(inputs):
            snapshots.append(list(inputs["items"]))
            inputs["items"].append("partial-work")
            if len(snapshots) == 1:
                raise RuntimeError("retry this attempt")
            return "done"

        result = await Workflow([WorkflowStep("unreliable", unreliable, retries=1)]).run({"items": []})
        self.assertTrue(result.ok)
        self.assertEqual(snapshots, [[], []])


class AgentStateReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = SQLiteStore(self.root / "state.db")
        self.writes = []

        async def write(arguments, context):
            self.writes.append(arguments["value"])
            return "saved"

        self.tools = ToolRegistry()
        self.tools.register(Tool("write", "append an approved value",
            {"type": "object", "properties": {"value": {"type": "string"}},
             "required": ["value"], "additionalProperties": False}, write, risk="write"))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def make_agent(self, responses, **kwargs):
        return Agent(ScriptedProvider(responses), tools=self.tools, store=self.store,
                     workspace=self.root / "work", **kwargs)

    async def test_approval_is_consumed_and_new_call_requires_new_decision(self):
        agent = self.make_agent([
            ModelResponse(tool_calls=[ToolCall("write", {"value": "first"}, id="first")]),
            ModelResponse(tool_calls=[ToolCall("write", {"value": "second"}, id="second")]),
            ModelResponse("done"),
        ])
        first = await agent.run("save two items", "approval")
        self.assertEqual(first.status, "waiting_approval")
        second = await agent.resume("approval", ["first"])
        self.assertEqual(second.status, "waiting_approval")
        self.assertEqual(self.writes, ["first"])
        state = self.store.load_session("approval")
        self.assertEqual(state["decisions"], {})
        self.assertEqual([call["id"] for call in state["pending"]], ["second"])
        final = await agent.resume("approval", [])
        self.assertEqual(final.status, "completed")
        self.assertEqual(self.writes, ["first"])

    async def test_reused_call_id_fails_before_second_execution(self):
        call = ToolCall("write", {"value": "once"}, id="spent")
        agent = self.make_agent([ModelResponse(tool_calls=[call]), ModelResponse(tool_calls=[call])])
        await agent.run("write", "duplicate")
        result = await agent.resume("duplicate", ["spent"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.writes, ["once"])
        state = self.store.load_session("duplicate")
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["decisions"], {})
        self.assertEqual(state["tool_count"], 1)

    async def test_changed_workspace_rejects_resume_without_consuming_approval(self):
        agent = self.make_agent([ModelResponse(tool_calls=[ToolCall("write", {"value": "pending"}, id="pending")])])
        await agent.run("write", "identity")
        other = Agent(ScriptedProvider([ModelResponse("done")]), tools=self.tools, store=self.store,
                      workspace=self.root / "different")
        with self.assertRaises(SessionError):
            await other.resume("identity", ["pending"])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.load_session("identity")["status"], "waiting_approval")
        self.assertTrue(self.store.acquire_session("identity", "test-owner"))
        self.store.release_session("identity", "test-owner")

    async def test_changed_tool_set_denies_pending_instead_of_stranding_session(self):
        agent = self.make_agent([ModelResponse(tool_calls=[ToolCall("write", {"value": "pending"}, id="pending")])])
        await agent.run("write", "tools-changed")
        # 升级后工具定义变化（这里新增一个工具）：旧审批不能套用到新定义上。
        async def extra(arguments, context):
            return "extra"
        self.tools.register(Tool("extra", "a newly added tool",
            {"type": "object", "properties": {}, "additionalProperties": False}, extra))
        restarted = self.make_agent([ModelResponse("done after denial")])
        # 即使调用方传入了批准，也必须按拒绝处理。
        result = await restarted.resume("tools-changed", ["pending"])
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.writes, [])
        state = self.store.load_session("tools-changed")
        denial = next(m for m in state["messages"] if m["role"] == "tool")
        self.assertIn("工具集在等待审批期间发生变化", denial["content"])
        self.assertEqual(state["execution"], restarted._identity())
        self.assertIn("tools_changed", [e["type"] for e in self.store.events("tools-changed")])

    async def test_delete_session_removes_data_but_not_while_running(self):
        agent = self.make_agent([ModelResponse("hi")])
        await agent.run("hello", "doomed")
        self.assertTrue(self.store.acquire_session("doomed", "other-owner"))
        with self.assertRaises(SessionError):
            agent.delete("doomed")
        self.store.release_session("doomed", "other-owner")
        agent.delete("doomed")
        self.assertIsNone(self.store.load_session("doomed"))
        self.assertEqual(self.store.events("doomed"), [])
        with self.assertRaises(SessionError):
            agent.delete("doomed")

    async def test_changed_provider_model_rejects_resume(self):
        agent = self.make_agent([ModelResponse(tool_calls=[ToolCall("write", {"value": "pending"}, id="pending")])])
        agent.provider.model = "original-model"
        await agent.run("write", "model-identity")
        restarted = self.make_agent([ModelResponse("done")])
        restarted.provider.model = "different-model"
        with self.assertRaises(SessionError):
            await restarted.resume("model-identity", ["pending"])
        self.assertEqual(self.writes, [])

    async def test_active_seconds_accumulate_but_exclude_approval_wait(self):
        clock = SimpleNamespace(value=1000.0)
        agent_clock = SimpleNamespace(monotonic=lambda: clock.value, time=time.time)

        class TimedProvider:
            index = 0

            async def complete(self, messages, tools):
                self.index += 1
                if self.index == 1:
                    clock.value += 4.0
                    return ModelResponse(tool_calls=[ToolCall("write", {"value": "ok"}, id="timed")])
                clock.value += 2.0
                return ModelResponse("done")

        agent = Agent(TimedProvider(), tools=self.tools, store=self.store, workspace=self.root / "work",
                      config=AgentConfig(run_timeout=10))
        with patch("agentlab.agent.time", agent_clock):
            await agent.run("save", "time")
            self.assertEqual(self.store.load_session("time")["active_seconds"], 4.0)
            clock.value += 100.0  # Human reads the approval prompt.
            result = await agent.resume("time", ["timed"])
            self.assertEqual(result.status, "completed")
            self.assertEqual(self.store.load_session("time")["active_seconds"], 6.0)

    async def test_spent_time_budget_blocks_approved_tool_on_resume(self):
        agent = self.make_agent([ModelResponse(tool_calls=[ToolCall("write", {"value": "never"}, id="spent-time")])],
                                config=AgentConfig(run_timeout=10))
        await agent.run("write", "budget")
        state = self.store.load_session("budget")
        state["active_seconds"] = 10
        self.store.save_session("budget", state)
        result = await agent.resume("budget", ["spent-time"])
        self.assertEqual(result.status, "limited")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.load_session("budget")["pending"], [])


if __name__ == "__main__":
    unittest.main()
