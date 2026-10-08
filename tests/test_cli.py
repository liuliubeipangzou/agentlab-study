"""Hermetic offline CLI integration tests using fresh subprocesses and temp data.

The environment is constructed from scratch: no real API credentials, proxies,
user workspace, or user state are read. Only demo commands and missing-config
validation run; no request to a model service is made.
"""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agentlab-cli-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data_dir = self.root / "custom data"
        self.workspace = self.root / "custom workspace"
        # Deliberately do not copy os.environ, even to strip API keys afterwards.
        self.environment = {
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # 这些测试验证“写入需要审批”的流程；默认的 auto-workspace 有单独的测试。
            "AGENTLAB_APPROVAL_MODE": "ask",
        }

    def invoke(self, *arguments, as_json=True, provider="demo", expected=0):
        command = [sys.executable, "-m", "agentlab", "--provider", provider,
                   "--data-dir", str(self.data_dir), "--workspace", str(self.workspace)]
        if as_json:
            command.append("--json")
        command.extend(arguments)
        process = subprocess.run(command, cwd=self.root, env=self.environment,
                                 text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(process.returncode, expected,
                         "Command: %r\nstdout:\n%s\nstderr:\n%s" % (arguments, process.stdout, process.stderr))
        if as_json and expected == 0:
            return json.loads(process.stdout)
        return process

    def test_default_mode_is_auto_workspace_and_flag_overrides_it(self):
        env = dict(self.environment)
        env.pop("AGENTLAB_APPROVAL_MODE")
        self.environment = env  # 不设置模式：命令行默认 auto-workspace
        auto = self.invoke("run", "/write auto.txt hello")
        self.assertEqual(auto["status"], "completed")
        self.assertEqual((self.workspace / "auto.txt").read_text(), "hello")
        asked = self.invoke("--approval-mode", "ask", "run", "/write asked.txt hello")
        self.assertEqual(asked["status"], "waiting_approval")
        self.assertFalse((self.workspace / "asked.txt").exists())
        bad = self.invoke("--approval-mode", "yolo", "run", "/calc 1+1", expected=2, as_json=False)
        self.assertIn("invalid choice", bad.stderr)
        self.environment = dict(env, AGENTLAB_APPROVAL_MODE="nonsense")
        wrong = self.invoke("run", "/calc 1+1", expected=2, as_json=False)
        self.assertIn("approval_mode", wrong.stderr)

    def test_waiting_output_shows_preview_and_hints_for_remembering(self):
        (self.workspace).mkdir(parents=True, exist_ok=True)
        (self.workspace / "a.txt").write_text("old\n")
        process = self.invoke("run", "/write a.txt new", as_json=False)
        self.assertIn("风险：写入工作区", process.stdout)
        self.assertIn("覆盖写入 a.txt", process.stdout)
        self.assertIn("| -old", process.stdout)
        self.assertIn("| +new", process.stdout)
        self.assertIn("--remember session", process.stdout)
        self.assertIn("--reason", process.stdout)

    def test_deny_reason_remember_and_audit_commands(self):
        waiting = self.invoke("run", "/write one.txt 1")
        denied = self.invoke("deny", waiting["session_id"], "--reason", "请改用 docs 目录")
        self.assertEqual(denied["status"], "completed")
        messages = self.invoke("inspect", waiting["session_id"])["messages"]
        self.assertIn("请改用 docs 目录", next(m for m in messages if m["role"] == "tool")["content"])
        self.assertFalse((self.workspace / "one.txt").exists())
        second = self.invoke("run", "/write two.txt 2")
        approved = self.invoke("approve", second["session_id"], "--all", "--remember", "global", "--reason", "x")
        self.assertEqual(approved["status"], "completed")
        rules = self.invoke("rules")
        self.assertEqual([(r["tool"], r["match"]) for r in rules], [("write_file", {"kind": "tool"})])
        # 全局规则对新会话生效：同类写入不再暂停；撤销后恢复询问。
        auto = self.invoke("run", "/write three.txt 3")
        self.assertEqual(auto["status"], "completed")
        audit = self.invoke("approvals", auto["session_id"])
        self.assertEqual([(a["tool"], a["decision"], a["source"]) for a in audit],
                         [("write_file", "auto", "rule:" + rules[0]["id"])])
        self.assertEqual(self.invoke("rules", "--delete", rules[0]["id"]), [])
        self.assertEqual(self.invoke("run", "/write four.txt 4")["status"], "waiting_approval")
        self.invoke("rules", "--delete", "missing", expected=2, as_json=False)
        self.invoke("approvals", "missing", expected=2, as_json=False)

    def test_budget_flags_reach_agent_and_session_can_be_deleted(self):
        # 预算参数应真正生效：一步上限会让需要两步的任务触顶；--max-tokens/--timeout 也被接受。
        process = self.invoke("--max-steps", "1", "--max-tokens", "100000", "--timeout", "30",
                              "run", "/calc 2 + 2", "--session", "budgeted", expected=1)  # limited 退出码为 1
        limited = json.loads(process.stdout)
        self.assertEqual(limited["status"], "limited")
        self.assertIn("达到模型调用步数上限", limited["output"])
        self.assertEqual(self.invoke("delete", "budgeted"), {"deleted": "budgeted"})
        missing = self.invoke("inspect", "budgeted", expected=2, as_json=False)
        self.assertIn("未找到会话", missing.stderr)

    def test_run_continues_session_across_processes_in_custom_directories(self):
        first = self.invoke("run", "/calc (6 + 1) * 6", "--session", "persistent-session")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["tool_calls"], 1)
        self.assertIn("42", first["output"])
        second = self.invoke("run", "/calc 9 * 9", "--session", first["session_id"])
        self.assertEqual(second["status"], "completed")
        self.assertIn("81", second["output"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        state = self.invoke("inspect", first["session_id"])
        users = [m["content"] for m in state["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["/calc (6 + 1) * 6", "/calc 9 * 9"])
        self.assertTrue((self.data_dir / "agentlab.sqlite3").is_file())
        self.assertTrue(self.workspace.is_dir())
        self.assertFalse((self.root / ".agentlab").exists())
        self.assertFalse((self.root / "workspace").exists())
        sessions = self.invoke("sessions")
        self.assertEqual([s["session_id"] for s in sessions], [first["session_id"]])

    def test_write_requires_approval_and_preserves_checkpoint(self):
        waiting = self.invoke("run", '/write "notes/learning notes.txt" "approved content"')
        self.assertEqual(waiting["status"], "waiting_approval")
        target = self.workspace / "notes" / "learning notes.txt"
        self.assertFalse(target.exists())
        self.assertEqual(waiting["pending"][0]["arguments"]["content"], "approved content")
        blocked = self.invoke("run", "/calc 1+1", "--session", waiting["session_id"], expected=2)
        self.assertIn("审批", blocked.stderr)
        before = self.invoke("inspect", waiting["session_id"])
        self.assertEqual(before["pending"], waiting["pending"])
        completed = self.invoke("approve", waiting["session_id"], "--call", waiting["pending"][0]["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(target.read_text(encoding="utf-8"), "approved content")
        after = self.invoke("inspect", waiting["session_id"])
        self.assertEqual(after["pending"], [])
        self.assertIsNone(after["in_flight"])
        trace = self.invoke("trace", waiting["session_id"])
        kinds = [event["type"] for event in trace]
        self.assertIn("approval_requested", kinds)
        self.assertIn("approval_resolved", kinds)
        self.assertLess(kinds.index("approval_resolved"), kinds.index("tool_started"))
        self.assertEqual(kinds.count("tool_started"), 1)

    def test_printed_approval_command_works_with_custom_paths(self):
        pending = self.invoke("run", "/write suggested.txt correct-directory", as_json=False)
        approval_line = next(line for line in pending.stdout.splitlines() if line.startswith("批准："))
        printed = shlex.split(approval_line.partition("：")[2])
        self.assertIn(str(self.data_dir), printed)
        self.assertIn(str(self.workspace), printed)
        # Use the same absolute interpreter; the minimal environment has no PATH.
        process = subprocess.run([sys.executable] + printed[1:], cwd=self.root,
                                 env=self.environment, text=True, encoding="utf-8",
                                 capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual((self.workspace / "suggested.txt").read_text(), "correct-directory")
        self.assertFalse((self.root / "workspace" / "suggested.txt").exists())

    def test_deny_prevents_write_and_closes_tool_protocol(self):
        waiting = self.invoke("run", "/write denied.txt do-not-write")
        finished = self.invoke("deny", waiting["session_id"])
        self.assertEqual(finished["status"], "completed")
        self.assertIn("拒绝", finished["output"])
        self.assertFalse((self.workspace / "denied.txt").exists())
        state = self.invoke("inspect", waiting["session_id"])
        tool_messages = [message for message in state["messages"] if message["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], waiting["pending"][0]["id"])
        self.assertFalse(json.loads(tool_messages[0]["content"])["ok"])
        # A denied checkpoint permits subsequent user turns.
        result = self.invoke("run", "/calc 2+2", "--session", waiting["session_id"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("4", result["output"])

    def test_memory_persists_but_is_isolated_between_sessions(self):
        waiting = self.invoke("run", "/remember goal learn-agent-with-tests", "--session", "memory-owner")
        self.assertEqual(waiting["status"], "waiting_approval")
        self.invoke("approve", waiting["session_id"], "--all")
        recalled = self.invoke("run", "/recall goal", "--session", waiting["session_id"])
        self.assertEqual(recalled["status"], "completed")
        self.assertIn("learn-agent-with-tests", recalled["output"])
        other = self.invoke("run", "/recall goal", "--session", "other-session")
        self.assertEqual(other["status"], "completed")
        self.assertNotIn("learn-agent-with-tests", other["output"])

    def test_rag_ingestion_search_and_agent_citations(self):
        knowledge = self.root / "knowledge input"
        knowledge.mkdir()
        source = knowledge / "agent-learning.md"
        source.write_text("# Agent 学习\n工具审批可以保护文件写入。检查点恢复不会自动重放工具。\n", encoding="utf-8")
        ingested = self.invoke("ingest", str(knowledge))
        self.assertEqual(ingested["documents"], 1)
        self.assertEqual(ingested["added"], 1)
        self.assertGreater(ingested["chunks"], 0)
        repeated = self.invoke("ingest", str(knowledge))
        self.assertEqual(repeated["unchanged"], 1)
        hits = self.invoke("search", "检查点恢复", "--limit", "2")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["source"], str(source.resolve()))
        self.assertIn("不会自动重放", hits[0]["text"])
        result = self.invoke("run", "/search 检查点恢复")
        self.assertEqual(result["status"], "completed")
        self.assertIn("agent-learning.md", result["output"])
        self.assertIn("不会自动重放", result["output"])

    def test_workflow_and_evaluation_are_offline_and_successful(self):
        workflow = self.invoke("workflow")
        self.assertEqual(workflow["errors"], {})
        self.assertEqual(workflow["statuses"], {"calculate": "success", "research": "success", "report": "success"})
        self.assertIn("60", workflow["outputs"]["calculate"])
        self.assertIn(workflow["outputs"]["calculate"], workflow["outputs"]["report"])
        report = self.invoke("eval")
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["passed"], report["total"])
        self.assertEqual(report["pass_rate"], 1.0)
        self.assertTrue(all(case["passed"] for case in report["cases"]))

    def test_tools_command_lists_callable_contracts(self):
        tools = self.invoke("tools")
        names = {tool["function"]["name"] for tool in tools}
        # 基础能力与联网/执行能力都必须暴露给模型。
        self.assertLessEqual({"calculator", "read_file", "write_file", "search_knowledge",
                              "remember", "recall"}, names)
        self.assertLessEqual({"web_search", "fetch_url", "http_request", "run_python"}, names)
        for tool in tools:
            self.assertEqual(tool["type"], "function")
            self.assertEqual(tool["function"]["parameters"]["type"], "object")
            self.assertTrue(tool["function"]["description"])

    def test_missing_live_configuration_fails_locally_without_traceback(self):
        process = self.invoke("run", "hello", provider="openai", expected=2)
        self.assertIn("AGENTLAB_MODEL", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertEqual(process.stdout, "")
        # 离线演示模型仍然可用，但需要显式选择：真实模型已是默认路径。
        self.assertEqual(self.invoke("run", "/calc 7*8", provider="demo")["status"], "completed")
        # 默认 provider 在没有凭据时必须给出可读错误，而不是静默降级为演示模式。
        process = self.invoke("run", "hello", provider="openai", expected=2)
        # 校验顺序为先模型名、后 API Key，两者都缺失时报出前者即可。
        self.assertTrue("AGENTLAB_API_KEY" in process.stderr or "AGENTLAB_MODEL" in process.stderr,
                        process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertNotIn("离线 Demo", process.stdout)


if __name__ == "__main__":
    unittest.main()
