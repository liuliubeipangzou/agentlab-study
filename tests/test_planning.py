"""结构化输出校验、有限修复与计划执行的行为测试。"""
import asyncio
import copy
import json
import unittest

from agentlab.planning import Planner, StructuredOutputError, parse_structured, structured_complete
from agentlab.types import ModelResponse, ToolCall


ANSWER_SCHEMA = {
    "type": "object", "properties": {"answer": {"type": "integer"}},
    "required": ["answer"], "additionalProperties": False,
}


class RecordingProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        return self.responses.pop(0)


def step(name, dependencies=None):
    return {"name": name, "instruction": "run " + name, "depends_on": dependencies or []}


class StructuredParseTests(unittest.TestCase):
    def test_complete_json_and_outer_json_fence(self):
        for text in ('{"answer": 42}', '  {"answer": 42}\n', '```json\n{"answer": 42}\n```'):
            with self.subTest(text=text):
                self.assertEqual(parse_structured(text, ANSWER_SCHEMA), {"answer": 42})

    def test_rejects_duplicate_keys_even_in_nested_objects(self):
        for text in ('{"answer": 1, "answer": 2}',
                     '{"nested": {"same": 1, "same": 2}}',
                     '{"nested": [{"same": 1, "same": 2}]}'):
            with self.subTest(text=text), self.assertRaises(StructuredOutputError):
                parse_structured(text, {"type": "object"})

    def test_rejects_nonfinite_values_and_schema_mismatch(self):
        for text in ('{"answer": NaN}', '{"answer": Infinity}', '{"answer": -Infinity}',
                     '{"answer": 1e309}', '{"answer": true}', '{"answer": "42"}',
                     '{"answer": 42, "extra": "x"}', '{}', '[]'):
            with self.subTest(text=text), self.assertRaises(StructuredOutputError):
                parse_structured(text, ANSWER_SCHEMA)
        with self.assertRaises(StructuredOutputError):
            parse_structured('{"answer": 42}', {"type": "object", "oneOf": []})

    def test_rejects_wrapped_text_multiple_documents_and_large_or_deep_json(self):
        for text in ('Answer: {"answer": 42}', '{"answer": 42} trailing',
                     '{"answer": 42} {"answer": 43}',
                     '```python\n{"answer": 42}\n```', ' ' * 64001,
                     '[' * 1100 + '0' + ']' * 1100):
            with self.subTest(text=text[:50]), self.assertRaises(StructuredOutputError):
                parse_structured(text, ANSWER_SCHEMA)


class StructuredCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_invalid_reply_with_no_tool_permissions_or_bad_content_replay(self):
        provider = RecordingProvider([ModelResponse("SECRET invalid response"),
                                      ModelResponse('{"answer": 42}')])
        self.assertEqual(await structured_complete(provider, "answer", ANSWER_SCHEMA), {"answer": 42})
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([tools for _, tools in provider.calls], [[], []])
        messages = provider.calls[1][0]
        self.assertEqual([message.role for message in messages], ["system", "user", "user"])
        self.assertFalse(any("SECRET" in message.content for message in messages))
        self.assertTrue(any("校验" in message.content for message in messages))

    async def test_invalid_replies_use_exactly_retry_plus_one_attempts(self):
        for retries in (0, 1, 3):
            with self.subTest(retries=retries):
                provider = RecordingProvider([ModelResponse("invalid")] * 5)
                with self.assertRaises(StructuredOutputError):
                    await structured_complete(provider, "answer", ANSWER_SCHEMA, retries=retries)
                self.assertEqual(len(provider.calls), retries + 1)

    async def test_tool_calls_are_rejected_even_when_content_is_valid(self):
        provider = RecordingProvider([ModelResponse('{"answer": 42}', [ToolCall("write_file", {})]),
                                      ModelResponse('{"answer": 43}')])
        self.assertEqual(await structured_complete(provider, "answer", ANSWER_SCHEMA), {"answer": 43})
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(any(message.tool_calls for message in provider.calls[1][0]))

    async def test_invalid_limits_fail_before_provider_call(self):
        provider = RecordingProvider([])
        for retries in (-1, 4, True, 1.5):
            with self.subTest(retries=retries), self.assertRaises(ValueError):
                await structured_complete(provider, "answer", ANSWER_SCHEMA, retries=retries)
        for timeout in (0, -1, 301, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await structured_complete(provider, "answer", ANSWER_SCHEMA, timeout=timeout)
        self.assertEqual(provider.calls, [])

    async def test_timeout_cancels_provider_without_schema_retry(self):
        cancelled = asyncio.Event()

        class SlowProvider:
            calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        provider = SlowProvider()
        with self.assertRaises(asyncio.TimeoutError):
            await structured_complete(provider, "answer", ANSWER_SCHEMA, retries=3, timeout=.01)
        self.assertEqual(provider.calls, 1)
        self.assertTrue(cancelled.is_set())


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_plan_and_invalid_dependency_graphs(self):
        good = [step("compose", ["research"]), step("research")]
        provider = RecordingProvider([ModelResponse(json.dumps({"steps": good}))])
        self.assertEqual(await Planner(provider).plan("write tutorial"), good)
        bad_plans = [([step("same"), step("same")], "duplicate"),
                     ([step("a", ["missing"])], "missing"),
                     ([step("a", ["b"]), step("b", ["a"])], "cycle"),
                     ([step("a", ["a"])], "cycle")]
        for plan, error in bad_plans:
            with self.subTest(plan=plan):
                provider = RecordingProvider([ModelResponse(json.dumps({"steps": plan}))])
                with self.assertRaisesRegex(ValueError, error):
                    await Planner(provider).plan("invalid task")

    async def test_execute_passes_only_declared_dependencies_and_correct_instructions(self):
        calls = {}

        async def runner(instruction, dependencies):
            calls[instruction] = dependencies
            return {"instruction": instruction, "inputs": sorted(dependencies)}

        plan = [step("finish", ["research"]), step("unrelated"), step("research")]
        result = await Planner(None).execute(plan, runner, concurrency=1)
        self.assertTrue(result.ok)
        self.assertEqual(calls["run unrelated"], {})
        self.assertEqual(calls["run research"], {})
        self.assertEqual(calls["run finish"], {"research": {"instruction": "run research", "inputs": []}})
        self.assertEqual(result.outputs["finish"], {"instruction": "run finish", "inputs": ["research"]})

    async def test_invalid_execution_plan_runs_no_steps(self):
        calls = []

        async def runner(instruction, dependencies):
            calls.append(instruction)

        for plan in ([step("a", ["missing"])], [step("a", ["a"])], [],
                     [{"name": "a", "instruction": "run a", "depends_on": [], "extra": 1}]):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                await Planner(None).execute(plan, runner)
        self.assertEqual(calls, [])

    async def test_execution_snapshots_dependency_lists_before_awaiting_runner(self):
        started = asyncio.Event()
        release = asyncio.Event()
        plan = [step("source"), step("consume", ["source"])]
        observed = []

        async def runner(instruction, dependencies):
            if instruction == "run source":
                started.set()
                await release.wait()
                return "source value"
            observed.append(dependencies)
            return "done"

        task = asyncio.create_task(Planner(None).execute(plan, runner))
        try:
            await asyncio.wait_for(started.wait(), 1)
            plan[1]["depends_on"][:] = ["unexpected"]
            release.set()
            result = await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(observed, [{"source": "source value"}])

    async def test_runner_failure_skips_dependents_and_keeps_independent_results(self):
        calls = []

        async def runner(instruction, dependencies):
            calls.append(instruction)
            if instruction == "run fail":
                raise RuntimeError("dependency unavailable")
            return "ready"

        result = await Planner(None).execute(
            [step("fail"), step("dependent", ["fail"]), step("independent")], runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.statuses, {"fail": "failed", "dependent": "skipped", "independent": "success"})
        self.assertNotIn("run dependent", calls)
        self.assertEqual(result.outputs, {"independent": "ready"})


if __name__ == "__main__":
    unittest.main()
