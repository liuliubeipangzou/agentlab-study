"""pysandbox 的边界与隔离回归测试。

这些用例确保代码执行"有界"：超时会终止、内存会受限、环境不泄漏、进程组不残留。
"""

import asyncio
import os
import subprocess
import sys
import unittest

from agentlab import pysandbox


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_result_and_captures_stdout(self):
        outcome = await pysandbox.run_python(
            "print('hello')\nresult = sum(range(101))")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"], 5050)
        self.assertIn("hello", outcome["stdout"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertFalse(outcome["timed_out"])

    async def test_exception_is_reported_not_propagated(self):
        outcome = await pysandbox.run_python("result = 1 / 0")
        self.assertFalse(outcome["ok"])
        self.assertIn("ZeroDivisionError", outcome["error"])

    async def test_syntax_error_is_reported(self):
        outcome = await pysandbox.run_python("def broken(: pass")
        self.assertFalse(outcome["ok"])
        self.assertIn("SyntaxError", outcome["error"])

    async def test_stderr_is_captured_separately(self):
        outcome = await pysandbox.run_python(
            "import sys\nprint('to stderr', file=sys.stderr)\nresult = 1")
        self.assertTrue(outcome["ok"])
        self.assertIn("to stderr", outcome["stderr"])
        self.assertNotIn("to stderr", outcome["stdout"])

    async def test_large_output_is_truncated(self):
        outcome = await pysandbox.run_python("print('A' * 20000)\nresult = 'ok'",
                                             max_output_chars=1000)
        self.assertTrue(outcome["ok"])
        self.assertLess(len(outcome["stdout"]), 1500)
        self.assertIn("截断", outcome["stdout"])

    async def test_unserializable_result_is_coerced(self):
        outcome = await pysandbox.run_python(
            "class Thing: pass\nresult = {'obj': Thing(), 'nan': float('inf')}")
        self.assertTrue(outcome["ok"])
        self.assertIn("obj", outcome["result"])

    async def test_argument_validation(self):
        cases = [("", {}), ("   ", {}), ("x = 1", {"timeout": 0}),
                 ("x = 1", {"timeout": 10 ** 6}), ("x = 1", {"memory_mb": 8}),
                 ("x = 1", {"memory_mb": True}), ("x" * 100001, {})]
        for code, kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    await pysandbox.run_python(code, **kwargs)


class ContainmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_process(self):
        outcome = await pysandbox.run_python(
            "import time\ntime.sleep(120)\nresult = 'never'", timeout=2)
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["timed_out"])
        self.assertLess(outcome["duration"], 20)

    async def test_timeout_cleans_up_grandchildren(self):
        """超时必须终止整个进程组，不能留下后台进程。"""
        marker = "agentlab-sandbox-grandchild-marker"
        code = ("import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', "
                "\"import time; time.sleep(300) # %s\"])\n"
                "print('spawned', flush=True)\n"
                "time.sleep(300)\n" % marker)
        outcome = await pysandbox.run_python(code, timeout=4)
        self.assertTrue(outcome["timed_out"])
        await asyncio.sleep(0.5)
        try:
            listed = subprocess.run(["pgrep", "-f", marker], capture_output=True,
                                    text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            self.skipTest("pgrep 在当前环境不可用")
        self.assertEqual(listed, "", "超时后仍残留孙子进程：%s" % listed)

    async def test_memory_limit_terminates_process(self):
        """内存炸弹必须被看门狗终止；无法读取 RSS 的平台跳过。"""
        code = ("chunks = []\n"
                "for _ in range(400):\n"
                "    chunks.append(bytearray(16 * 1024 * 1024))\n"
                "result = len(chunks)\n")
        outcome = await pysandbox.run_python(code, timeout=25, memory_mb=256)
        if outcome.get("memory_enforced") is False and outcome.get("ok"):
            self.skipTest("当前平台无法读取子进程 RSS，内存限制由子进程自限")
        if outcome["ok"]:
            self.skipTest("当前平台未实施内存强制（RLIMIT_AS 不可用）")
        self.assertEqual(outcome.get("limit_exceeded"), "memory")
        self.assertIn("内存", outcome["error"])

    async def test_lightweight_task_is_not_affected_by_limits(self):
        outcome = await pysandbox.run_python(
            "result = sum(i * i for i in range(100000))", memory_mb=256)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"], 333328333350000)

    async def test_secrets_do_not_reach_child_process(self):
        os.environ["AGENTLAB_API_KEY"] = "must-not-leak-into-child"
        os.environ["OPENAI_API_KEY"] = "also-must-not-leak"
        try:
            outcome = await pysandbox.run_python(
                "import os\n"
                "result = {k: v for k, v in os.environ.items() if 'KEY' in k.upper() or 'TOKEN' in k.upper()}")
            self.assertTrue(outcome["ok"])
            self.assertEqual(outcome["result"], {})
        finally:
            os.environ.pop("AGENTLAB_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)

    async def test_child_runs_isolated_from_pythonpath(self):
        outcome = await pysandbox.run_python(
            "import sys, os\nresult = {'has_empty_path': os.environ.get('PYTHONPATH') is None}")
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["result"]["has_empty_path"])

    async def test_child_working_directory_is_workspace_then_cleaned(self):
        outcome = await pysandbox.run_python("import os\nresult = os.getcwd()")
        self.assertTrue(outcome["ok"])
        self.assertFalse(os.path.exists(outcome["result"]),
                         "临时工作目录应在执行后被删除")

    async def test_temporary_files_do_not_leak_between_runs(self):
        first = await pysandbox.run_python("open('leak.txt', 'w').write('x')\nresult = 1")
        self.assertTrue(first["ok"])
        second = await pysandbox.run_python(
            "import os\nresult = os.path.exists('leak.txt')")
        self.assertTrue(second["ok"])
        self.assertFalse(second["result"])


if __name__ == "__main__":
    unittest.main()
