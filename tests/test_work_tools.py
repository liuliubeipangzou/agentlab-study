import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agentlab import cli
from agentlab.agent import Agent, AgentConfig, SessionError
from agentlab.providers import ScriptedProvider
from agentlab.storage import SQLiteStore
from agentlab.tools import Tool, ToolContext, ToolRegistry, create_builtin_tools
from agentlab.types import ModelResponse, ToolCall


class WorkToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve() / "work"
        self.workspace.mkdir()
        self.registry = create_builtin_tools()
        self.context = ToolContext(self.workspace)

    async def call(self, name, arguments, approved=False):
        return await self.registry.execute(ToolCall(name, arguments), self.context, approved=approved)

    def needs_approval(self, name, arguments):
        return self.registry.requires_approval(ToolCall(name, arguments))

    def test_new_tools_are_registered_with_expected_risk(self):
        names = {d["function"]["name"] for d in self.registry.definitions()}
        expected = {"append_file", "edit_file", "glob", "grep", "run_shell", "git", "now", "todo_write", "ask_user"}
        self.assertLessEqual(expected, names)
        for name in ("edit_file", "append_file", "run_shell"):
            self.assertTrue(self.needs_approval(name, {}), name)
        for name in ("glob", "grep", "now", "todo_write", "read_file"):
            self.assertFalse(self.needs_approval(name, {}), name)
        self.assertTrue(self.registry.is_interactive(ToolCall("ask_user", {"question": "?"})))
        self.assertFalse(self.registry.is_interactive(ToolCall("run_shell", {})))
        self.assertFalse(self.registry.is_interactive(ToolCall("nonexistent", {})))

    def test_git_approval_matrix(self):
        free = [["status"], ["status", "--short"], ["diff", "HEAD~1"], ["log", "--oneline", "-5"], ["show", "abc"],
                ["blame", "a.py"], ["branch"], ["branch", "-a"], ["branch", "--show-current"], ["tag"],
                ["tag", "-l"], ["remote"], ["remote", "-v"], ["stash", "list"], ["config", "--get", "user.name"],
                ["ls-files"], ["rev-parse", "HEAD"]]
        asks = [["commit", "-m", "x"], ["add", "."], ["checkout", "main"], ["push"], ["reset", "--hard"],
                ["clean", "-fd"], ["branch", "newbranch"], ["branch", "-D", "x"], ["tag", "v1"],
                ["remote", "add", "o", "url"], ["stash"], ["stash", "drop"], ["config", "user.name", "x"],
                ["diff", "--output=out.txt"], ["log", "--ext-diff"], ["rebase", "main"], ["fetch"], ["clone", "u"]]
        for args in free:
            with self.subTest(args=args):
                self.assertFalse(self.needs_approval("git", {"args": args}))
        for args in asks:
            with self.subTest(args=args):
                self.assertTrue(self.needs_approval("git", {"args": args}))
        # 畸形参数一律要求审批（fail closed）。
        for bad in ({}, {"args": []}, {"args": "status"}, {"args": [1]}, {"args": ["-c", "x=y", "status"]}):
            self.assertTrue(self.needs_approval("git", bad), bad)

    async def test_git_rejects_dangerous_global_and_transport_options(self):
        for args in (["-c", "core.pager=sh", "status"], ["--git-dir=/tmp/x", "status"],
                     ["fetch", "--upload-pack=evil"], ["status", "--exec-path=/tmp"]):
            with self.subTest(args=args):
                result = await self.call("git", {"args": args}, approved=True)
                self.assertFalse(result["ok"])

    async def test_run_shell_requires_approval_and_reports_failure_details(self):
        call = ToolCall("run_shell", {"command": "echo hi"})
        denied = await self.registry.execute(call, self.context)
        self.assertFalse(denied["ok"])
        self.assertIn("Approval required", denied["error"])
        ok = await self.call("run_shell", {"command": "echo hi; pwd"}, approved=True)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["value"]["stdout"].split()[0], "hi")
        self.assertIn(str(self.workspace), ok["value"]["stdout"])
        failed = await self.call("run_shell", {"command": "echo bad >&2; exit 4"}, approved=True)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["details"]["exit_code"], 4)
        self.assertIn("bad", failed["details"]["stderr"])

    async def test_failed_command_keeps_tail_of_long_output_within_budget(self):
        self.context.max_output_chars = 4000
        script = "i=0; while [ $i -lt 3000 ]; do echo noise-$i; i=$((i+1)); done; echo 'FAILED test_x: boom' >&2; exit 1"
        failed = await self.call("run_shell", {"command": script}, approved=True)
        self.assertFalse(failed["ok"])
        self.assertIn("FAILED test_x: boom", failed["details"]["stderr"])
        self.assertIn("noise-2999", failed["details"]["stdout"])
        self.assertLessEqual(len(json.dumps(failed, ensure_ascii=False)), 4000)

    async def test_command_cwd_is_confined_to_workspace(self):
        (self.workspace / "sub").mkdir()
        inside = await self.call("run_shell", {"command": "pwd", "cwd": "sub"}, approved=True)
        self.assertTrue(inside["value"]["stdout"].strip().endswith("/work/sub"))
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.workspace / "link").symlink_to(outside)
        for cwd in ("../outside", "/etc", "link", "missing", "sub/../..", ".git"):
            with self.subTest(cwd=cwd):
                self.assertFalse((await self.call("run_shell", {"command": "pwd", "cwd": cwd}, approved=True))["ok"])

    async def test_git_roundtrip_in_workspace_repository(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.assertFalse((await self.call("git", {"args": ["status"]}))["ok"])  # 还不是仓库
        self.assertTrue((await self.call("git", {"args": ["init", "-q"]}, approved=True))["ok"])
        for key, value in (("user.name", "Tester"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
            await self.call("git", {"args": ["config", key, value]}, approved=True)
        (self.workspace / "a.txt").write_text("one\n")
        status = await self.call("git", {"args": ["status", "--short"]})
        self.assertTrue(status["ok"])
        self.assertIn("a.txt", status["value"]["stdout"])
        await self.call("git", {"args": ["add", "a.txt"]}, approved=True)
        committed = await self.call("git", {"args": ["commit", "-q", "-m", "first"]}, approved=True)
        self.assertTrue(committed["ok"], committed)
        (self.workspace / "a.txt").write_text("one\ntwo\n")
        diff = await self.call("git", {"args": ["diff"]})
        self.assertIn("+two", diff["value"]["stdout"])
        log = await self.call("git", {"args": ["log", "--oneline"]})
        self.assertIn("first", log["value"]["stdout"])
        # 文件工具不能碰 .git，但 git 工具可以正常使用它。
        self.assertFalse((await self.call("read_file", {"path": ".git/HEAD"}))["ok"])
        self.assertFalse((await self.call("write_file", {"path": ".git/hooks/pre-commit", "content": "x"}, True))["ok"])

    async def test_now_and_ask_user_direct_execution(self):
        now = (await self.call("now", {}))["value"]
        self.assertRegex(now["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn(now["weekday"], ("周一", "周二", "周三", "周四", "周五", "周六", "周日"))
        # 交互式工具脱离 Agent 直接执行必须明确失败，而不是假装得到回答。
        direct = await self.call("ask_user", {"question": "继续吗？"})
        self.assertFalse(direct["ok"])
        self.assertIn("Agent", direct["error"])

    async def test_new_file_tools_through_registry(self):
        (self.workspace / "src").mkdir()
        await self.call("write_file", {"path": "src/app.py", "content": "def go():\n    return 1\n"}, approved=True)
        edited = await self.call("edit_file", {"path": "src/app.py", "old_string": "return 1",
                                               "new_string": "return 2"}, approved=True)
        self.assertTrue(edited["ok"])
        self.assertIn("+    return 2", edited["value"]["diff"])
        appended = await self.call("append_file", {"path": "src/app.py", "content": "go()\n"}, approved=True)
        self.assertEqual(appended["value"]["bytes_appended"], 5)
        found = await self.call("glob", {"pattern": "**/*.py"})
        self.assertEqual(found["value"]["matches"], ["src/app.py"])
        searched = await self.call("grep", {"pattern": "return \\d", "glob": "*.py"})
        self.assertEqual([(m["path"], m["line"]) for m in searched["value"]["matches"]], [("src/app.py", 2)])
        paged = await self.call("read_file", {"path": "src/app.py", "offset": 2, "limit": 1})
        self.assertEqual(paged["value"]["content"], "     2\t    return 2")
        self.assertEqual(paged["value"]["next_offset"], 3)


class AgentWorkFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root / "state.db")
        self.addCleanup(self.store.close)

    def agent(self, responses, tools=None, **kwargs):
        return Agent(ScriptedProvider(responses), tools=tools, store=self.store,
                     workspace=self.root / "work", **kwargs)

    async def test_todo_write_persists_emits_event_and_reaches_prompt(self):
        todos = [{"content": "读代码", "status": "completed"}, {"content": "改 bug", "status": "in_progress"},
                 {"content": "跑测试", "status": "pending"}]
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("todo_write", {"todos": todos}, "t1")]),
                            ModelResponse("开始改"), ModelResponse("接着做")])
        result = await agent.run("修 bug", "todo")
        self.assertEqual(result.status, "completed")
        state = self.store.load_session("todo")
        self.assertEqual(state["todos"], todos)
        event = next(e for e in self.store.events("todo") if e["type"] == "todos_updated")
        self.assertEqual(event["data"]["todos"], todos)
        # 下一次模型调用的系统提示里能看到清单；清单也跨 run 保留。
        system = agent.provider.calls[1]["messages"][0].content
        self.assertIn("[x] 读代码", system)
        self.assertIn("[~] 改 bug", system)
        self.assertIn("[ ] 跑测试", system)
        await agent.run("继续", "todo")
        self.assertIn("[~] 改 bug", agent.provider.calls[-1]["messages"][0].content)
        self.assertEqual(self.store.load_session("todo")["todos"], todos)

    async def test_todo_write_validation(self):
        bad = [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]
        agent = self.agent([ModelResponse(tool_calls=[ToolCall("todo_write", {"todos": bad}, "t1")]),
                            ModelResponse("好")])
        await agent.run("x", "todo-bad")
        state = self.store.load_session("todo-bad")
        self.assertEqual(state.get("todos"), [])
        tool_message = next(m for m in state["messages"] if m["role"] == "tool")
        self.assertFalse(json.loads(tool_message["content"])["ok"])
        for arguments in ({"todos": [{"content": "a", "status": "bogus"}]}, {"todos": [{"content": " ", "status": "pending"}]}):
            result = await create_builtin_tools().execute(
                ToolCall("todo_write", arguments), ToolContext(self.root / "work"))
            self.assertFalse(result["ok"], arguments)

    async def test_ask_user_pauses_and_resumes_with_answer(self):
        ask = ToolCall("ask_user", {"question": "用哪个数据库？", "options": ["SQLite", "Postgres"]}, "q1")
        agent = self.agent([ModelResponse("我需要确认。", [ask]), ModelResponse("好的，使用 Postgres。")])
        paused = await agent.run("建表", "ask")
        self.assertEqual(paused.status, "waiting_input")
        self.assertIn("用哪个数据库？", paused.output)
        self.assertEqual([c["id"] for c in paused.pending], ["q1"])
        self.assertIn("input_requested", [e["type"] for e in self.store.events("ask")])
        with self.assertRaises(SessionError):
            await agent.run("另一个任务", "ask")
        with self.assertRaises(SessionError):
            await agent.resume("ask", [])
        with self.assertRaises(ValueError):
            await agent.answer("ask", "wrong-id", "Postgres")
        with self.assertRaises(ValueError):
            await agent.answer("ask", "q1", "   ")
        self.assertEqual(self.store.load_session("ask")["status"], "waiting_input")  # 失败的回答不改变状态
        done = await agent.answer("ask", "q1", "Postgres")
        self.assertEqual((done.status, done.output), ("completed", "好的，使用 Postgres。"))
        tool_message = next(m for m in self.store.load_session("ask")["messages"] if m["role"] == "tool")
        self.assertEqual(json.loads(tool_message["content"]), {"ok": True, "value": {"answer": "Postgres"}})
        self.assertEqual(self.store.load_session("ask")["answers"], {})
        with self.assertRaises(SessionError):
            await agent.answer("ask", "q1", "again")

    async def test_ask_user_after_write_in_same_batch_runs_in_order(self):
        written = []

        async def write(arguments, context):
            written.append(arguments["value"])
            return "saved"

        tools = ToolRegistry()
        tools.register(Tool("write", "w", {"type": "object", "properties": {"value": {"type": "string"}},
                                           "required": ["value"], "additionalProperties": False}, write, risk="write"))
        tools.register(create_builtin_tools().get("ask_user"))
        calls = [ToolCall("write", {"value": "first"}, "w1"), ToolCall("ask_user", {"question": "继续？"}, "q1")]
        agent = self.agent([ModelResponse(tool_calls=calls), ModelResponse("全部完成")], tools=tools)
        first = await agent.run("go", "mixed")
        self.assertEqual(first.status, "waiting_approval")  # 先审批，再提问
        second = await agent.resume("mixed", ["w1"])
        self.assertEqual((second.status, written), ("waiting_input", ["first"]))
        self.assertEqual([c["id"] for c in second.pending], ["q1"])
        final = await agent.answer("mixed", "q1", "继续")
        self.assertEqual((final.status, written), ("completed", ["first"]))

    async def test_tool_change_while_waiting_input_cancels_approved_write(self):
        written = []

        async def write(arguments, context):
            written.append(arguments)
            return "saved"

        def build(extra=False):
            tools = ToolRegistry()
            tools.register(Tool("write", "w", {"type": "object", "properties": {}, "additionalProperties": False},
                                write, risk="write"))
            tools.register(create_builtin_tools().get("ask_user"))
            if extra:
                tools.register(Tool("extra", "新增的工具", {"type": "object", "properties": {},
                                                          "additionalProperties": False}, write))
            return tools

        calls = [ToolCall("ask_user", {"question": "确认？"}, "q1"), ToolCall("write", {}, "w1")]
        # auto-workspace 下写操作被规则自动放行，会话停在排在它前面的提问上：
        # 此时有一个“已批准但尚未执行”的调用。
        before = self.agent([ModelResponse(tool_calls=calls)], tools=build(),
                            config=AgentConfig(approval_mode="auto-workspace"))
        self.assertEqual((await before.run("go", "changed")).status, "waiting_input")
        self.assertEqual(self.store.load_session("changed")["decisions"], {"w1": True})
        # 升级工具集后再回答：旧批准（包括自动放行的）不能沿用，写操作必须被取消，会话也不能卡死。
        upgraded = self.agent([ModelResponse("收到")], tools=build(extra=True),
                              config=AgentConfig(approval_mode="auto-workspace"))
        final = await upgraded.answer("changed", "q1", "是")
        self.assertEqual(final.status, "completed")
        self.assertEqual(written, [])
        messages = {m["tool_call_id"]: json.loads(m["content"])
                    for m in self.store.load_session("changed")["messages"] if m["role"] == "tool"}
        self.assertEqual(messages["q1"]["value"], {"answer": "是"})
        self.assertFalse(messages["w1"]["ok"])
        self.assertIn("工具集", messages["w1"]["error"])
        kinds = [e["type"] for e in self.store.events("changed")]
        self.assertIn("tools_changed", kinds)
        self.assertIn("input_provided", kinds)

    def test_cli_question_id_resolution(self):
        agent_state = {"session_id": "s", "pending": [{"id": "a", "name": "ask_user", "arguments": {}},
                                                       {"id": "b", "name": "write_file", "arguments": {}}]}
        self.store.save_session("s", agent_state)
        self.assertEqual(cli._question_id(self.store, "s"), "a")
        self.assertEqual(cli._question_id(self.store, "s", "explicit"), "explicit")
        agent_state["pending"].append({"id": "c", "name": "ask_user", "arguments": {}})
        self.store.save_session("s", agent_state)
        with self.assertRaises(SessionError):
            cli._question_id(self.store, "s")
        self.store.save_session("s", dict(agent_state, pending=[]))
        with self.assertRaises(SessionError):
            cli._question_id(self.store, "s")
        with self.assertRaises(SessionError):
            cli._question_id(self.store, "missing")


if __name__ == "__main__":
    unittest.main()
