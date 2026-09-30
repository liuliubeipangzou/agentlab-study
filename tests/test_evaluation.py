"""回归评测要区分内容、工具顺序和状态，并隔离用例会话。"""
from types import SimpleNamespace
import unittest

from agentlab.evaluation import EvalCase, evaluate


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def factory_for(specifications, seen):
        pending = iter(specifications)

        def factory():
            spec = next(pending)
            events = [{"type": "tool_started", "data": {"name": name}} for name in spec.get("tools", [])]
            # 包含非工具事件和 completed 事件，防止错误地重复计数工具。
            events.insert(0, {"type": "run_started", "data": {}})
            events.append({"type": "tool_completed", "data": {"name": "ignored"}})

            def read_events(session_id):
                if session_id != spec["session_id"]:
                    raise AssertionError("evaluation read the wrong session")
                return events

            class Agent:
                store = SimpleNamespace(events=read_events)

                async def run(self, prompt):
                    seen.append((spec["session_id"], prompt, id(self)))
                    return SimpleNamespace(session_id=spec["session_id"], output=spec.get("output", ""),
                                           status=spec.get("status", "completed"))

            return Agent()

        return factory

    async def test_each_failure_dimension_is_reported_and_aggregate_is_correct(self):
        cases = [EvalCase("success", "good", contains=["答案", "42"], expected_tools=["calculator"]),
                 EvalCase("content failure", "bad content", contains=["missing"], expected_tools=["calculator"]),
                 EvalCase("tool failure", "bad tools", contains=["42"], expected_tools=["calculator"]),
                 EvalCase("status failure", "bad status", contains=["42"], expected_tools=["calculator"])]
        specs = [{"session_id": "a", "output": "答案是42", "tools": ["calculator"]},
                 {"session_id": "b", "output": "42", "tools": ["calculator"]},
                 {"session_id": "c", "output": "42", "tools": ["read_file"]},
                 {"session_id": "d", "output": "42", "tools": ["calculator"], "status": "failed"}]
        seen = []
        report = await evaluate(self.factory_for(specs, seen), cases)
        self.assertEqual((report["total"], report["passed"], report["pass_rate"]), (4, 1, .25))
        self.assertEqual([row["passed"] for row in report["cases"]], [True, False, False, False])
        self.assertEqual([row["checks"] for row in report["cases"]], [
            {"status": True, "content": True, "tools": True},
            {"status": True, "content": False, "tools": True},
            {"status": True, "content": True, "tools": False},
            {"status": False, "content": True, "tools": True},
        ])
        self.assertEqual(report["cases"][2]["actual_tools"], ["read_file"])
        self.assertEqual(report["cases"][0]["output"], "答案是42")
        self.assertEqual([(session, prompt) for session, prompt, _ in seen],
                         [("a", "good"), ("b", "bad content"), ("c", "bad tools"), ("d", "bad status")])

    async def test_tool_order_duplicates_and_extra_calls_are_checked_exactly(self):
        traces = [["read_file", "calculator"], ["calculator", "read_file"],
                  ["read_file", "calculator", "calculator"], ["read_file"], []]
        specs = [{"session_id": str(index), "tools": trace} for index, trace in enumerate(traces)]
        cases = [EvalCase(str(index), "task", expected_tools=["read_file", "calculator"])
                 for index in range(len(traces))]
        report = await evaluate(self.factory_for(specs, []), cases)
        self.assertEqual([row["checks"]["tools"] for row in report["cases"]],
                         [True, False, False, False, False])
        self.assertEqual(report["passed"], 1)

    async def test_custom_status_and_all_required_content_fragments(self):
        cases = [EvalCase("approval", "write", contains=["confirm"], expected_status="waiting_approval"),
                 EvalCase("both substrings", "explain", contains=["Agent", "memory"])]
        specs = [{"session_id": "a", "status": "waiting_approval", "output": "please confirm"},
                 {"session_id": "b", "output": "Agent only"}]
        report = await evaluate(self.factory_for(specs, []), cases)
        self.assertTrue(report["cases"][0]["passed"])
        self.assertFalse(report["cases"][1]["checks"]["content"])

    async def test_factory_is_called_for_each_case(self):
        agents = []

        def factory():
            class OnceOnlyAgent:
                calls = 0
                store = SimpleNamespace(events=lambda session_id: [])

                async def run(self, prompt):
                    self.calls += 1
                    if self.calls != 1:
                        raise AssertionError("agent reused across evaluation cases")
                    return SimpleNamespace(session_id="isolated", output="ok", status="completed")

            agent = OnceOnlyAgent()
            agents.append(agent)
            return agent

        report = await evaluate(factory, [EvalCase("a", "one"), EvalCase("b", "two")])
        self.assertEqual(len(agents), 2)
        self.assertIsNot(agents[0], agents[1])
        self.assertEqual(report["passed"], 2)

    async def test_empty_suite_is_defined_and_does_not_build_agent(self):
        def forbidden_factory():
            self.fail("empty suite must not instantiate an agent")

        self.assertEqual(await evaluate(forbidden_factory, []),
                         {"total": 0, "passed": 0, "pass_rate": 0.0, "cases": []})


if __name__ == "__main__":
    unittest.main()
