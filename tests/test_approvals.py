import json
import tempfile
import unittest
from pathlib import Path

from agentlab.agent import Agent, AgentConfig, SessionError
from agentlab.approvals import (DEFAULT_ALLOWED_COMMANDS, ApprovalPolicy, build_preview, command_tokens,
                                new_rule, parse_command, rule_matches, suggest_rule)
from agentlab.providers import ScriptedProvider
from agentlab.storage import SQLiteStore
from agentlab.tools import create_builtin_tools
from agentlab.types import ModelResponse, ToolCall


def shell(command, call_id=None):
    return ToolCall("run_shell", {"command": command}, call_id or "c" + str(abs(hash(command)) % 10**6))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tools = create_builtin_tools()

    def risk(self, call):
        return self.tools.call_risk(call)

    def test_command_parsing_rejects_shell_metacharacters_and_escapes(self):
        self.assertEqual(parse_command("pytest -k 'a and b'"), ["pytest", "-k", "a and b"])
        for bad in ("pytest; ls", "ls && ls", "ls | wc", "echo `id`", "echo $(id)", "echo $HOME", "ls > f", "cat < f",
                    "ls\nrm x", "echo 'open", "", "   ", "a\\b"):
            self.assertIsNone(parse_command(bad), bad)
        for bad in ("cat /etc/passwd", "cat ~/x", "cat ../x", "cat a/../../x", "pytest --basetemp=/tmp/x",
                    "ls --dir=../x"):
            self.assertIsNone(command_tokens(bad), bad)
        self.assertEqual(command_tokens("cat src/a.py"), ["cat", "src/a.py"])

    def test_mode_matrix(self):
        write = ToolCall("write_file", {"path": "a", "content": "x"}, "w")
        git_push = ToolCall("git", {"args": ["push"]}, "p")
        reset = ToolCall("git", {"args": ["reset", "--hard"]}, "r")
        http = ToolCall("http_request", {"url": "https://x.test/", "method": "POST"}, "h")
        cases = {
            "ask": [False, False, False, False, False],
            "auto-workspace": [True, False, False, False, False],
            "trust": [True, True, True, True, True],
        }
        for mode, expected in cases.items():
            policy = ApprovalPolicy(mode)
            actual = [policy.decide(write, self.risk(write)).allow,
                      policy.decide(shell("printf hi"), self.risk(shell("printf hi"))).allow,
                      policy.decide(git_push, self.risk(git_push)).allow,
                      policy.decide(reset, self.risk(reset)).allow,
                      policy.decide(http, self.risk(http)).allow]
            # 第二项 printf 不在默认清单里：只有 trust 放行。
            expected = list(expected)
            expected[1] = mode == "trust"
            self.assertEqual(actual, expected, mode)
        with self.assertRaises(ValueError):
            ApprovalPolicy("yolo")
        with self.assertRaises(ValueError):
            AgentConfig(approval_mode="yolo")

    def test_default_commands_only_in_auto_workspace_and_only_when_safe(self):
        policy = ApprovalPolicy("auto-workspace")
        for command in ("pytest tests/ -x", "python3 -m unittest discover -s tests", "ls -la src", "cat README.md",
                        "npm test", "npm run build", "go test ./...", "cargo check", "make test"):
            call = shell(command)
            self.assertTrue(policy.decide(call, self.risk(call)).allow, command)
        for command in ("pytest; rm -rf .", "pytest && curl x", "cat /etc/passwd", "npm run deploy", "make", "rm -rf build",
                        "python3 -c 'print(1)'", "curl http://x", "echo $SECRET", "ls ../other", "find . -delete"):
            call = shell(command)
            self.assertFalse(policy.decide(call, self.risk(call)).allow, command)
        ask = ApprovalPolicy("ask")
        self.assertFalse(ask.decide(shell("pytest"), "exec").allow)
        self.assertGreater(len(DEFAULT_ALLOWED_COMMANDS), 20)

    def test_rules_match_narrowly_and_never_cover_destructive(self):
        rule = new_rule(suggest_rule(shell("printf one"), "exec"))
        self.assertEqual(rule["match"], {"kind": "command_prefix", "prefix": ["printf"]})
        self.assertTrue(rule_matches(rule, shell("printf two"), "exec"))
        for command in ("printf one; rm -rf .", "printf $(id)", "printf /etc/x", "echo printf", "rm -rf ."):
            self.assertFalse(rule_matches(rule, shell(command), self.risk(shell(command))), command)
        self.assertFalse(rule_matches(rule, shell("printf x"), "destructive"))
        git_rule = new_rule(suggest_rule(ToolCall("git", {"args": ["commit", "-m", "x"]}, "g"), "exec"))
        self.assertTrue(rule_matches(git_rule, ToolCall("git", {"args": ["commit", "-m", "y"]}, "g2"), "exec"))
        self.assertFalse(rule_matches(git_rule, ToolCall("git", {"args": ["push"]}, "g3"), "network_write"))
        host_rule = new_rule(suggest_rule(ToolCall("http_request", {"url": "https://api.test/v1", "method": "POST"}, "h"),
                                          "network_write"))
        self.assertTrue(rule_matches(host_rule, ToolCall("http_request", {"url": "https://api.test/other"}, "h2"),
                                     "network_write"))
        self.assertFalse(rule_matches(host_rule, ToolCall("http_request", {"url": "https://evil.test/"}, "h3"),
                                      "network_write"))
        # 工具级规则只适用于工作区写入，不能把 exec 工具整个放开。
        tool_rule = {"id": "r", "tool": "run_python", "match": {"kind": "tool"}}
        self.assertFalse(rule_matches(tool_rule, ToolCall("run_python", {"code": "1"}, "x"), "exec"))

    def test_suggestions_refuse_blanket_and_destructive_commands(self):
        self.assertIsNone(suggest_rule(shell("rm -rf build"), "destructive"))
        for command in ("curl http://x", "find . -name x", "python3 -c 'print(1)'", "sed -i s/a/b/ f", "make",
                        "bash run.sh", "pytest; ls"):
            self.assertIsNone(suggest_rule(shell(command), "exec"), command)
        prefixes = {"pytest -x": ["pytest"], "python3 -m pytest -q": ["python3", "-m", "pytest"],
                    "npm run build": ["npm", "run", "build"], "npm test": ["npm", "test"],
                    "go test ./...": ["go", "test"], "cargo build --release": ["cargo", "build"]}
        for command, prefix in prefixes.items():
            self.assertEqual(suggest_rule(shell(command), "exec")["match"]["prefix"], prefix, command)
        self.assertIsNone(suggest_rule(ToolCall("run_python", {"code": "1"}, "x"), "exec"))
        self.assertIsNone(suggest_rule(ToolCall("git", {"args": ["push", "-f"]}, "x"), "destructive"))


class AgentApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "work"
        self.store = SQLiteStore(self.root / "state.db")
        self.addCleanup(self.store.close)

    def agent(self, responses, mode="ask"):
        return Agent(ScriptedProvider(responses), store=self.store, workspace=self.workspace,
                     config=AgentConfig(approval_mode=mode))

    def tool_results(self, session):
        return {m["tool_call_id"]: json.loads(m["content"])
                for m in self.store.load_session(session)["messages"] if m["role"] == "tool"}

    async def test_auto_workspace_writes_without_pausing_and_audits(self):
        write = ToolCall("write_file", {"path": "notes/a.txt", "content": "hi"}, "w1")
        agent = self.agent([ModelResponse(tool_calls=[write]), ModelResponse("done")], "auto-workspace")
        result = await agent.run("write", "auto")
        self.assertEqual(result.status, "completed")
        self.assertEqual((self.workspace / "notes/a.txt").read_text(), "hi")
        event = next(e for e in self.store.events("auto") if e["type"] == "approval_auto")
        self.assertEqual((event["data"]["tool"], event["data"]["risk"], event["data"]["source"]),
                         ("write_file", "write", "mode:auto-workspace"))
        audit = self.store.list_approvals("auto")
        self.assertEqual([(a["tool"], a["risk"], a["decision"], a["source"]) for a in audit],
                         [("write_file", "write", "auto", "mode:auto-workspace")])

    async def test_ask_mode_pauses_and_exec_still_asks_in_auto_workspace(self):
        for mode, call in (("ask", ToolCall("write_file", {"path": "a.txt", "content": "x"}, "w1")),
                           ("auto-workspace", shell("printf hello", "s1"))):
            agent = self.agent([ModelResponse(tool_calls=[call])], mode)
            result = await agent.run("go", "pause-" + mode)
            self.assertEqual(result.status, "waiting_approval", mode)
        self.assertFalse((self.workspace / "a.txt").exists())

    async def test_trust_mode_runs_everything_without_asking(self):
        agent = self.agent([ModelResponse(tool_calls=[shell("printf trusted > out.txt && rm -f out.txt", "s1")]),
                            ModelResponse("done")], "trust")
        self.assertEqual((await agent.run("go", "trust")).status, "completed")
        self.assertTrue(self.tool_results("trust")["s1"]["ok"])

    async def test_calls_before_the_gated_one_run_first(self):
        calls = [ToolCall("calculator", {"expression": "6*7"}, "c1"),
                 ToolCall("write_file", {"path": "a.txt", "content": "x"}, "w1"),
                 ToolCall("calculator", {"expression": "1+1"}, "c2")]
        agent = self.agent([ModelResponse(tool_calls=calls), ModelResponse("done")])
        paused = await agent.run("go", "order")
        self.assertEqual(paused.status, "waiting_approval")
        self.assertEqual(self.tool_results("order")["c1"]["value"], 42)  # 前面的只读调用没被写操作拖住
        self.assertNotIn("c2", self.tool_results("order"))               # 后面的按顺序等待
        self.assertEqual([c["id"] for c in paused.pending], ["w1", "c2"])
        approval = next(e for e in self.store.events("order") if e["type"] == "approval_requested")
        self.assertEqual([c["id"] for c in approval["data"]["calls"]], ["w1"])
        done = await agent.resume("order", ["w1"])
        self.assertEqual(done.status, "completed")
        self.assertEqual(self.tool_results("order")["c2"]["value"], 2)

    async def test_denial_feedback_reaches_the_model(self):
        write = ToolCall("write_file", {"path": "a.txt", "content": "x"}, "w1")
        agent = self.agent([ModelResponse(tool_calls=[write]), ModelResponse("改用别的方式")])
        await agent.run("go", "deny")
        result = await agent.resume("deny", [], feedback="不要写到根目录，改写到 docs/ 下")
        self.assertEqual(result.status, "completed")
        error = self.tool_results("deny")["w1"]["error"]
        self.assertIn("不要写到根目录", error)
        self.assertIn("请勿绕过审批", error)
        audit = self.store.list_approvals("deny")
        self.assertEqual((audit[0]["decision"], audit[0]["source"], audit[0]["feedback"]),
                         ("denied", "user", "不要写到根目录，改写到 docs/ 下"))
        self.assertFalse((self.workspace / "a.txt").exists())
        # 没有理由时保持原来的固定提示。
        plain = self.agent([ModelResponse(tool_calls=[ToolCall("write_file", {"path": "b", "content": "x"}, "w2")]),
                            ModelResponse("好")])
        await plain.run("go", "deny2")
        await plain.resume("deny2", [])
        self.assertEqual(self.tool_results("deny2")["w2"]["error"], "用户拒绝了该操作，请勿绕过审批")

    async def test_resume_validates_feedback_and_remember(self):
        agent = self.agent([ModelResponse(tool_calls=[shell("printf x", "s1")])])
        await agent.run("go", "validate")
        for kwargs in ({"feedback": "x" * 2001}, {"feedback": 5}, {"remember": "forever"}):
            with self.assertRaises(ValueError):
                await agent.resume("validate", [], **kwargs)
        with self.assertRaises(ValueError):
            await agent.resume("validate", ["not-pending"])
        self.assertEqual(self.store.load_session("validate")["status"], "waiting_approval")

    async def test_remember_for_session_auto_approves_same_kind_later(self):
        first = shell("printf one", "s1")
        second = shell("printf two", "s2")
        attack = shell("printf three; echo pwned", "s3")
        agent = self.agent([ModelResponse(tool_calls=[first]), ModelResponse(tool_calls=[second]),
                            ModelResponse(tool_calls=[attack]), ModelResponse("done")])
        paused = await agent.run("go", "remember")
        self.assertEqual(paused.status, "waiting_approval")
        # 批准并记住后，同一次运行里后续的同类命令不再询问；带 shell 元字符的变体仍然询问。
        again = await agent.resume("remember", ["s1"], remember="session")
        self.assertEqual(again.status, "waiting_approval")
        self.assertEqual([c["id"] for c in again.pending], ["s3"])
        results = self.tool_results("remember")
        self.assertEqual((results["s1"]["ok"], results["s2"]["ok"]), (True, True))
        self.assertEqual(results["s1"]["value"]["stdout"], "one")
        rules = self.store.load_session("remember")["approval_rules"]
        self.assertEqual([r["match"]["prefix"] for r in rules], [["printf"]])
        self.assertEqual(self.store.list_approval_rules(), [])
        sources = [a["source"] for a in self.store.list_approvals("remember")]
        self.assertEqual(sources, ["user", "rule:" + rules[0]["id"]])
        self.assertIn("approval_rule_added", [e["type"] for e in self.store.events("remember")])
        # 规则是会话级的：另一个会话仍要询问。
        other = self.agent([ModelResponse(tool_calls=[shell("printf one", "o1")])])
        self.assertEqual((await other.run("go", "someone-else")).status, "waiting_approval")

    async def test_remember_globally_and_revoke(self):
        agent = self.agent([ModelResponse(tool_calls=[shell("printf one", "s1")]), ModelResponse("done")])
        await agent.run("go", "g1")
        await agent.resume("g1", ["s1"], remember="global")
        (rule,) = self.store.list_approval_rules()
        self.assertEqual(rule["match"]["prefix"], ["printf"])
        auto = self.agent([ModelResponse(tool_calls=[shell("printf two", "t1")]), ModelResponse("ok")])
        self.assertEqual((await auto.run("go", "g2")).status, "completed")
        self.assertTrue(self.store.delete_approval_rule(rule["id"]))
        self.assertFalse(self.store.delete_approval_rule(rule["id"]))
        asks = self.agent([ModelResponse(tool_calls=[shell("printf three", "u1")])])
        self.assertEqual((await asks.run("go", "g3")).status, "waiting_approval")
        # 同一批里批准两个同类调用并记住，只会产生一条规则。
        both = self.agent([ModelResponse(tool_calls=[shell("printf a", "v1"), shell("printf b", "v2")]),
                           ModelResponse("done")])
        self.assertEqual((await both.run("go", "g4")).status, "waiting_approval")
        await both.resume("g4", ["v1", "v2"], remember="global")
        self.assertEqual(len(self.store.list_approval_rules()), 1)

    async def test_destructive_is_never_remembered_and_trust_still_runs_it(self):
        kill = shell("rm -rf build", "d1")
        agent = self.agent([ModelResponse(tool_calls=[kill])], "auto-workspace")
        self.assertEqual((await agent.run("go", "destructive")).status, "waiting_approval")
        await agent.resume("destructive", ["d1"], remember="session")
        self.assertEqual(self.store.load_session("destructive").get("approval_rules"), [])
        reset = ToolCall("git", {"args": ["reset", "--hard"]}, "d2")
        again = self.agent([ModelResponse(tool_calls=[reset])], "auto-workspace")
        self.assertEqual((await again.run("go", "destructive2")).status, "waiting_approval")

    async def test_audit_is_removed_with_the_session(self):
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("write_file", {"path": "a", "content": "x"}, "w1")]),
                            ModelResponse("done")], "auto-workspace")
        await agent.run("go", "audit-delete")
        self.assertEqual(len(self.store.list_approvals("audit-delete")), 1)
        agent.delete("audit-delete")
        self.assertEqual(self.store.list_approvals("audit-delete"), [])


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def preview(self, name, arguments):
        return build_preview(ToolCall(name, arguments, "p"), self.workspace)

    def test_write_file_new_and_existing(self):
        new = self.preview("write_file", {"path": "a.txt", "content": "hello\n"})
        self.assertEqual(new["kind"], "text")
        self.assertIn("新建文件 a.txt", new["title"])
        (self.workspace / "a.txt").write_text("hello\nworld\n")
        diff = self.preview("write_file", {"path": "a.txt", "content": "hello\nthere\n"})
        self.assertEqual(diff["kind"], "diff")
        self.assertIn("-world", diff["text"])
        self.assertIn("+there", diff["text"])
        same = self.preview("write_file", {"path": "a.txt", "content": "hello\nworld\n"})
        self.assertIn("没有变化", same["text"])

    def test_edit_file_previews_result_and_failures(self):
        (self.workspace / "m.py").write_text("x = 1\ny = 1\n")
        ok = self.preview("edit_file", {"path": "m.py", "old_string": "x = 1", "new_string": "x = 2"})
        self.assertEqual(ok["kind"], "diff")
        self.assertIn("+x = 2", ok["text"])
        missing = self.preview("edit_file", {"path": "m.py", "old_string": "z", "new_string": "q"})
        self.assertIn("没有找到", missing["text"])
        ambiguous = self.preview("edit_file", {"path": "m.py", "old_string": " = 1", "new_string": " = 2"})
        self.assertIn("出现了 2 次", ambiguous["text"])
        gone = self.preview("edit_file", {"path": "nope.py", "old_string": "a", "new_string": "b"})
        self.assertEqual(gone["kind"], "json")

    def test_commands_code_and_fallback(self):
        shell_preview = self.preview("run_shell", {"command": "pytest -x", "cwd": "api", "timeout": 60})
        self.assertEqual((shell_preview["kind"], shell_preview["text"]), ("command", "pytest -x"))
        self.assertIn("api", shell_preview["title"])
        self.assertEqual(self.preview("run_python", {"code": "print(1)"})["kind"], "code")
        git = self.preview("git", {"args": ["commit", "-m", "fix: a b"]})
        self.assertEqual(git["text"], "git commit -m 'fix: a b'")
        http = self.preview("http_request", {"url": "https://x.test/", "method": "POST", "body": "{}"})
        self.assertEqual((http["title"], http["text"]), ("POST https://x.test/", "{}"))
        unknown = self.preview("custom", {"a": 1})
        self.assertEqual(unknown["kind"], "json")
        broken = self.preview("write_file", {"path": 5})
        self.assertEqual(broken["kind"], "json")
        # 受保护文件的预览不会泄露内容。
        (self.workspace / ".env").write_text("SECRET=1\n")
        guarded = self.preview("write_file", {"path": ".env", "content": "SECRET=2\n"})
        self.assertNotIn("SECRET=1", guarded["text"])


if __name__ == "__main__":
    unittest.main()
