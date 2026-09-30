import asyncio
import unittest

from agentlab.workflows import Workflow, WorkflowStep


async def nothing(outputs):
    return None


class WorkflowValidationTests(unittest.TestCase):
    def test_rejects_duplicate_missing_and_cyclic_dependencies(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            Workflow([WorkflowStep("same", nothing), WorkflowStep("same", nothing)])
        with self.assertRaisesRegex(ValueError, "missing"):
            Workflow([WorkflowStep("a", nothing, ["absent"])])
        with self.assertRaisesRegex(ValueError, "cycle"):
            Workflow([WorkflowStep("a", nothing, ["b"]), WorkflowStep("b", nothing, ["a"])])
        with self.assertRaisesRegex(ValueError, "cycle"):
            Workflow([WorkflowStep("a", nothing, ["a"])])

    def test_rejects_invalid_execution_limits(self):
        for concurrency in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                Workflow([], concurrency=concurrency)
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Workflow([WorkflowStep("a", nothing, timeout=timeout)])
        with self.assertRaises(ValueError):
            Workflow([WorkflowStep("a", nothing, retries=-1)])


class WorkflowExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependencies_receive_outputs_even_in_reverse_order(self):
        async def source(outputs):
            return outputs["seed"] + 1

        async def transform(outputs):
            return outputs["source"] * 2

        result = await Workflow([
            WorkflowStep("transform", transform, ["source"]),
            WorkflowStep("source", source),
        ]).run({"seed": 20})
        self.assertTrue(result.ok)
        self.assertEqual(result.outputs, {"seed": 20, "source": 21, "transform": 42})
        self.assertEqual(result.errors, {})

    async def test_independent_steps_run_concurrently_with_a_bound(self):
        active = 0
        peak = 0
        two_started = asyncio.Event()
        release = asyncio.Event()

        async def worker(outputs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            try:
                await release.wait()
                return 1
            finally:
                active -= 1

        workflow = Workflow([WorkflowStep(str(i), worker) for i in range(5)], concurrency=2)
        running = asyncio.create_task(workflow.run())
        await asyncio.wait_for(two_started.wait(), timeout=1)
        self.assertEqual(peak, 2)
        release.set()
        result = await asyncio.wait_for(running, timeout=1)
        self.assertEqual(peak, 2)
        self.assertEqual(sum(result.outputs.values()), 5)
        self.assertTrue(result.ok)

    async def test_failure_skips_descendants_but_independent_branch_finishes(self):
        async def fail(outputs):
            raise RuntimeError("service unavailable")

        async def should_not_run(outputs):
            self.fail("dependent step must be skipped")

        async def independent(outputs):
            await asyncio.sleep(0.01)
            return "finished"

        result = await Workflow([
            WorkflowStep("grandchild", should_not_run, ["child"]),
            WorkflowStep("child", should_not_run, ["failure"]),
            WorkflowStep("failure", fail),
            WorkflowStep("independent", independent),
        ]).run()
        self.assertFalse(result.ok)
        self.assertEqual(result.statuses, {"grandchild": "skipped", "child": "skipped",
                                          "failure": "failed", "independent": "success"})
        self.assertIn("service unavailable", result.errors["failure"])
        self.assertEqual(result.outputs["independent"], "finished")
        self.assertNotIn("child", result.outputs)

    async def test_retry_is_bounded_and_eventually_succeeds(self):
        attempts = 0

        async def unstable(outputs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("temporary")
            return "ready"

        result = await Workflow([WorkflowStep("retry", unstable, retries=2)]).run()
        self.assertTrue(result.ok)
        self.assertEqual(attempts, 3)
        self.assertEqual(result.outputs["retry"], "ready")

    async def test_exhausted_retries_and_timeout_release_tasks(self):
        attempts = 0
        cleaned = 0

        async def slow(outputs):
            nonlocal attempts, cleaned
            attempts += 1
            try:
                await asyncio.Event().wait()
            finally:
                cleaned += 1

        result = await Workflow([WorkflowStep("slow", slow, retries=1, timeout=0.01)]).run()
        self.assertEqual(attempts, 2)
        self.assertEqual(cleaned, 2)
        self.assertEqual(result.statuses["slow"], "failed")
        self.assertIn("TimeoutError", result.errors["slow"])

    async def test_caller_cancellation_cancels_all_active_steps(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def waiting(outputs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        task = asyncio.create_task(Workflow([WorkflowStep("waiting", waiting)]).run())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())

    async def test_step_snapshot_cannot_add_keys_to_shared_outputs(self):
        async def modifies_snapshot(outputs):
            outputs["unexpected"] = "no"
            return outputs["seed"]

        result = await Workflow([WorkflowStep("result", modifies_snapshot)]).run({"seed": 1})
        self.assertNotIn("unexpected", result.outputs)
        with self.assertRaisesRegex(ValueError, "collide"):
            await Workflow([WorkflowStep("seed", nothing)]).run({"seed": 1})

    async def test_nonasync_callable_reports_step_error(self):
        result = await Workflow([WorkflowStep("bad", lambda _: "not awaitable")]).run()
        self.assertEqual(result.statuses["bad"], "failed")
        self.assertIn("awaitable", result.errors["bad"])


if __name__ == "__main__":
    unittest.main()
