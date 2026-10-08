import asyncio
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agentlab.agent import Agent
from agentlab.providers import OpenAICompatibleProvider
from agentlab.server import APIError, App, DELTA_LIMIT, MAX_BODY
from agentlab.storage import SQLiteStore
from agentlab.types import ModelResponse, ToolCall


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict("os.environ", {"AGENTLAB_API_KEY": "", "AGENTLAB_MODEL": "deepseek-flash",
                                                       "AGENTLAB_BASE_URL": "https://api.deepseek.com",
                                                       "AGENTLAB_ALLOW_DEMO": "1",
                                                       "AGENTLAB_PROVIDER": "demo"})
        self.environment.start()
        self.app = App(self.root / "data", self.root / "work")
        self.addCleanup(self.app.close)
        self.server = self.app.create_server(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.request("GET", "/api/bootstrap")[1]["csrf_token"]

    def tearDown(self):
        self.app.close()
        self.thread.join(timeout=2)
        self.environment.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        outgoing = {"Host": "127.0.0.1:{}".format(self.port)}
        data = None
        if method == "POST":
            outgoing["Content-Type"] = "application/json"
            if token and hasattr(self, "token"):
                outgoing["X-AgentLab-Token"] = self.token
            data = json.dumps(body or {}).encode("utf-8")
        outgoing.update(headers or {})
        try:
            connection.request(method, path, body=data, headers=outgoing)
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            value = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def wait_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, job, _ = self.request("GET", "/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        self.fail("background job did not finish")

    def test_bootstrap_health_and_security_headers(self):
        status, body, headers = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["provider"], "demo")
        self.assertFalse(body["config"]["has_api_key"])
        self.assertEqual(body["stats"], {"sessions": 0, "documents": 0, "chunks": 0})
        self.assertTrue(body["tools"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("GET", "/api/health")[1], {"app": "agentlab", "status": "ok"})

    def test_rejects_cross_origin_bad_host_missing_token_and_large_body(self):
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/run", {"prompt": "hello"}, token=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/run", {"prompt": "hello"},
                                     headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/run", headers={"Content-Length": str(MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request("POST", "/api/run", headers={"Content-Type": "text/plain"})[0], 415)
        status, error, _ = self.request("DELETE", "/api/sessions/anything")
        self.assertEqual(status, 405)
        self.assertIsInstance(error["error"], str)

    def test_real_agent_run_persists_trace_and_title(self):
        status, submitted, _ = self.request("POST", "/api/run", {"prompt": "/calc 6 * 7"})
        self.assertEqual(status, 200)
        job = self.wait_job(submitted["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["status"], "completed")
        self.assertIn("42", job["result"]["output"])
        self.assertIn("tool_finished", [event["type"] for event in job["events"]])
        state = self.request("GET", "/api/sessions/" + submitted["session_id"])[1]
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["active"])
        self.assertIsNone(state["active_job_id"])
        self.assertEqual(len(state["events"]), len(job["events"]))
        self.assertEqual(self.request("GET", "/api/sessions")[1]["sessions"][0]["title"], "/calc 6 * 7")

    def test_write_requires_explicit_approval_and_cannot_be_replayed(self):
        submitted = self.request("POST", "/api/run", {"prompt": "/write notes.txt approved"})[1]
        first = self.wait_job(submitted["job_id"])
        self.assertEqual(first["result"]["status"], "waiting_approval")
        target = self.root / "work" / "notes.txt"
        self.assertFalse(target.exists())
        session_id = submitted["session_id"]
        ids = [call["id"] for call in first["result"]["pending"]]
        approved = self.request("POST", "/api/sessions/" + session_id + "/approve",
                                {"approved_call_ids": ids})[1]
        self.assertEqual(self.wait_job(approved["job_id"])["result"]["status"], "completed")
        self.assertEqual(target.read_text(), "approved")
        replay = self.request("POST", "/api/sessions/" + session_id + "/approve",
                              {"approved_call_ids": ids})[1]
        self.assertEqual(self.wait_job(replay["job_id"])["status"], "failed")

    def test_conditional_approval_is_flagged_by_server_for_ui(self):
        """http_request 的 POST 风险级别是 read，但需要审批；前端必须能据此显示勾选框并批准。"""
        from agentlab.providers import ScriptedProvider
        provider = ScriptedProvider([ModelResponse("", [
            ToolCall("http_request", {"url": "https://example.com/", "method": "GET"}, "get1"),
            ToolCall("http_request", {"url": "https://example.com/", "method": "POST", "body": "x"}, "post1")])])
        with patch.object(self.app, "_provider", return_value=provider):
            submitted = self.request("POST", "/api/run", {"prompt": "call api", "session_id": "cond"})[1]
            job = self.wait_job(submitted["job_id"])
        self.assertEqual(job["result"]["status"], "waiting_approval", job["result"])
        state = self.request("GET", "/api/sessions/cond")[1]
        flags = {call["id"]: call["needs_approval"] for call in state["pending"]}
        self.assertEqual(flags, {"get1": False, "post1": True})
        definition = next(x for x in self.request("GET", "/api/bootstrap")[1]["tools"]
                          if x["function"]["name"] == "http_request")
        self.assertEqual(definition["risk"], "read")

    def test_delete_session_via_api(self):
        submitted = self.request("POST", "/api/run", {"prompt": "/calc 1 + 1", "session_id": "gone"})[1]
        self.wait_job(submitted["job_id"])
        self.assertEqual(self.request("POST", "/api/sessions/gone/delete")[1], {"deleted": "gone"})
        self.assertEqual(self.request("GET", "/api/sessions/gone")[0], 404)
        self.assertEqual(self.request("POST", "/api/sessions/gone/delete")[0], 404)

    def test_config_key_is_memory_only_and_configuration_makes_no_model_request(self):
        key = "sk-fake-server-test-secret"
        with patch("agentlab.server.OpenAICompatibleProvider.complete", new_callable=AsyncMock) as complete:
            status, saved, _ = self.request("POST", "/api/config", {
                "provider": "openai", "model": "test-model", "base_url": "https://example.invalid/v1", "api_key": key})
            self.assertEqual(status, 200)
            self.assertTrue(saved["config"]["has_api_key"])
            self.assertNotIn(key, json.dumps(saved))
            self.assertNotIn(key, (self.root / "data" / "web-settings.json").read_text())
            self.assertNotIn("api_key", (self.root / "data" / "web-settings.json").read_text())
            self.assertNotIn(key, json.dumps(self.request("GET", "/api/bootstrap")[1]))
            self.request("POST", "/api/config", {"api_key": ""})
            self.assertTrue(self.app.public_config()["has_api_key"])
            complete.assert_not_called()
            complete.return_value = ModelResponse("connected")
            submitted = self.request("POST", "/api/connection-test")[1]
            self.assertEqual(self.wait_job(submitted["job_id"])["result"]["content"], "connected")
            self.assertEqual(complete.await_count, 1)

    def test_active_job_cancellation_and_configuration_guard(self):
        entered = threading.Event()
        class SlowProvider:
            async def complete(self, messages, tools):
                entered.set()
                await asyncio.Event().wait()

        with patch.object(self.app, "_provider", return_value=SlowProvider()):
            submitted = self.request("POST", "/api/run", {"prompt": "slow", "session_id": "slow"})[1]
            self.assertTrue(entered.wait(2))
            state = self.request("GET", "/api/sessions/slow")[1]
            self.assertTrue(state["active"])
            self.assertEqual(state["active_job_id"], submitted["job_id"])
            self.assertEqual(self.request("POST", "/api/config", {"model": "changed"})[0], 409)
            self.assertEqual(self.request("POST", "/api/run", {"prompt": "race", "session_id": "slow"})[0], 409)
            self.assertEqual(self.request("POST", "/api/cancel", {"job_id": submitted["job_id"]})[1], {"ok": True})
            self.assertEqual(self.wait_job(submitted["job_id"])["status"], "cancelled")
            self.assertEqual(self.request("GET", "/api/sessions/slow")[1]["status"], "cancelled")

    def test_pending_online_checkpoint_can_restore_its_original_configuration(self):
        key = "sk-fake-restore-key"
        provider = OpenAICompatibleProvider("previous-model", key, "https://previous.example/v1")
        old_agent = Agent(provider, store=self.app.store, tools=self.app.tools, workspace=self.root / "work")
        call = ToolCall("write_file", {"path": "restored.txt", "content": "restored"}, id="restore")
        # 产品默认开启流式，因此模型调用会走 stream() 而不是 complete()。两处都 mock，
        # 否则测试会向真实网络发请求（曾经因此失败，且掩盖了真实的协议路径）。
        with patch("agentlab.server.OpenAICompatibleProvider.stream", new_callable=AsyncMock) as stream, \
                patch("agentlab.server.OpenAICompatibleProvider.complete", new_callable=AsyncMock) as complete:
            # 两条路径共享同一份剧本。注意 chat/completions 的两个类引用指向同一个
            # 类对象，因此 old_agent（无 on_delta，走 complete）与恢复任务（有 on_delta，
            # 走 stream）会竞争同一个计数器；按"第一次给工具调用、之后给结论"推进即可。
            plan = [ModelResponse(tool_calls=[call]), ModelResponse("done")]
            state = {"index": 0}

            async def queued(messages, on_delta=None, tools=None):
                index = min(state["index"], len(plan) - 1)
                state["index"] += 1
                return plan[index]

            stream.side_effect = queued
            complete.side_effect = queued
            result = asyncio.run(old_agent.run("restore an earlier CLI session", "old-online"))
            self.assertEqual(result.status, "waiting_approval")
            self.assertEqual(self.app.public_config()["provider"], "demo")
            status, _, _ = self.request("POST", "/api/config", {
                "provider": "openai", "model": "previous-model",
                "base_url": "https://previous.example/v1", "api_key": key})
            self.assertEqual(status, 200)
            submitted = self.request("POST", "/api/sessions/old-online/approve", {"approved_call_ids": ["restore"]})[1]
            job = self.wait_job(submitted["job_id"])
            self.assertEqual(job["result"]["status"], "completed")
            self.assertEqual((self.root / "work" / "restored.txt").read_text(), "restored")

    def test_background_job_limit_is_enforced(self):
        class SlowProvider:
            async def complete(self, messages, tools):
                await asyncio.Event().wait()

        with patch.object(self.app, "_provider", return_value=SlowProvider()):
            jobs = []
            for _ in range(self.app.MAX_ACTIVE_JOBS):
                status, result, _ = self.request("POST", "/api/connection-test")
                self.assertEqual(status, 200)
                jobs.append(result["job_id"])
            self.assertEqual(self.request("POST", "/api/connection-test")[0], 429)
            for job_id in jobs:
                self.request("POST", "/api/cancel", {"job_id": job_id})
            for job_id in jobs:
                self.assertEqual(self.wait_job(job_id)["status"], "cancelled")

    def test_shutdown_cancels_active_agent_and_saves_its_final_checkpoint(self):
        entered = threading.Event()
        class SlowProvider:
            async def complete(self, messages, tools):
                entered.set()
                await asyncio.Event().wait()

        with patch.object(self.app, "_provider", return_value=SlowProvider()):
            self.request("POST", "/api/run", {"prompt": "shutdown test", "session_id": "shutdown"})
            self.assertTrue(entered.wait(2))
            self.app.close()
        self.assertFalse(self.app._thread.is_alive())
        self.assertFalse(self.server.serving.is_set())
        with SQLiteStore(self.root / "data" / "agentlab.sqlite3") as reopened:
            self.assertEqual(reopened.load_session("shutdown")["status"], "cancelled")
            self.assertTrue(reopened.acquire_session("shutdown", "next-owner"))

    def test_knowledge_import_search_and_path_validation(self):
        status, imported, _ = self.request("POST", "/api/knowledge/import", {
            "files": [{"name": "lesson.md", "content": "知识检索可以帮助智能体引用来源。Agent memory persists facts."}]})
        self.assertEqual(status, 200)
        self.assertEqual(imported["documents"], 1)
        knowledge = self.request("GET", "/api/knowledge")[1]
        self.assertEqual(knowledge["stats"]["documents"], 1)
        self.assertEqual(knowledge["documents"][0]["chunks"], 1)
        hits = self.request("POST", "/api/knowledge/search", {"query": "知识检索"})[1]["results"]
        self.assertEqual(Path(hits[0]["source"]).name, "lesson.md")
        self.assertEqual(self.request("POST", "/api/knowledge/import", {
            "files": [{"name": "../escape.md", "content": "no"}]})[0], 400)
        self.assertFalse((self.root / "data" / "escape.md").exists())
        self.assertEqual(self.request("GET", "/api/docs/../../server.py")[0], 404)
        self.assertEqual(self.request("GET", "/../agentlab/server.py")[0], 404)

    def test_example_workflow_and_evaluation_use_real_components(self):
        self.assertEqual(self.request("POST", "/api/knowledge/example")[0], 200)
        workflow = self.request("POST", "/api/workflow")[1]
        report = self.wait_job(workflow["job_id"])
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["result"]["statuses"], {"calculate": "success", "research": "success", "report": "success"})
        self.assertTrue(report["events"])
        evaluation = self.request("POST", "/api/evaluate")[1]
        report = self.wait_job(evaluation["job_id"])
        self.assertEqual(report["result"]["passed"], 3)
        self.assertEqual(report["result"]["total"], 3)

    def test_recover_closes_interrupted_checkpoint_without_writing(self):
        submitted = self.request("POST", "/api/run", {"prompt": "/write never.txt no", "session_id": "crash"})[1]
        self.wait_job(submitted["job_id"])
        state = self.app.store.load_session("crash")
        state["status"] = "running"
        state["in_flight"] = state["pending"][0]["id"]
        self.app.store.save_session("crash", state)
        status, recovered, _ = self.request("POST", "/api/sessions/crash/recover")
        self.assertEqual(status, 200)
        self.assertEqual(recovered["status"], "failed")
        self.assertFalse((self.root / "work" / "never.txt").exists())

    def test_loopback_only(self):
        with self.assertRaises(ValueError):
            App(self.root / "unsafe", self.root / "work", host="0.0.0.0")

    def test_installed_resource_fallback_without_repository_root(self):
        resources = self.root / "installed-resources"
        (resources / "docs").mkdir(parents=True)
        (resources / "knowledge").mkdir()
        (resources / "docs" / "architecture.md").write_text("installed architecture", encoding="utf-8")
        (resources / "docs" / "README.md").write_text("installed README", encoding="utf-8")
        (resources / "knowledge" / "lesson.md").write_text("installed knowledge memory", encoding="utf-8")
        with patch("agentlab.server.ROOT", self.root / "missing-repository"), patch("agentlab.server.RESOURCES", resources):
            self.assertEqual(self.request("GET", "/api/docs/architecture")[1]["content"], "installed architecture")
            self.assertEqual(self.request("GET", "/api/docs/README.md")[1]["content"], "installed README")
            status, imported, _ = self.request("POST", "/api/knowledge/example")
            self.assertEqual(status, 200)
            self.assertEqual(imported["documents"], 1)

    def test_invalid_saved_configuration_shows_nonsecret_repair_warning(self):
        directory = self.root / "bad-settings"
        directory.mkdir()
        (directory / "web-settings.json").write_text(
            json.dumps({"provider": "invalid", "api_key": "secret-never-echo"}), encoding="utf-8")
        recovered = App(directory, self.root / "repair-work")
        try:
            bootstrap = recovered.api("GET", "/api/bootstrap", {})
            self.assertEqual(bootstrap["config"]["provider"], "demo")
            self.assertIn("重新保存", bootstrap["warning"])
            self.assertNotIn("secret-never-echo", json.dumps(bootstrap))
            recovered.configure({"provider": "demo", "model": "deepseek-flash", "base_url": "https://api.deepseek.com"})
            self.assertEqual(recovered.api("GET", "/api/bootstrap", {})["warning"], "")
        finally:
            recovered.close()


if __name__ == "__main__":
    unittest.main()


class IncrementalEventTests(unittest.TestCase):
    """增量事件接口：必须通过真实 HTTP 验证。

    这一层曾经出过一个只有走 HTTP 才会暴露的 bug：handler 在转发前用
    `urlsplit(self.path).path` 把查询串剥掉了，导致 api() 永远读到空的 since，
    每次都重发全量事件。直接调用 app.api() 的测试无法发现它。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict("os.environ", {
            "AGENTLAB_API_KEY": "", "AGENTLAB_MODEL": "deepseek-flash",
            "AGENTLAB_BASE_URL": "https://api.deepseek.com",
            "AGENTLAB_ALLOW_DEMO": "1", "AGENTLAB_PROVIDER": "demo"})
        self.environment.start()
        self.app = App(self.root / "data", self.root / "work")
        self.addCleanup(self.app.close)
        self.server = self.app.create_server(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.token = self.request("GET", "/api/bootstrap")[1]["csrf_token"]

    def _stop_server(self):
        self.app.close()
        self.thread.join(timeout=2)
        self.environment.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        outgoing = {"Host": "127.0.0.1:{}".format(self.port)}
        data = None
        if method == "POST":
            outgoing["Content-Type"] = "application/json"
            outgoing["X-AgentLab-Token"] = self.token
            data = json.dumps(body or {}).encode("utf-8")
        try:
            connection.request(method, path, body=data, headers=outgoing)
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            value = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, value
        finally:
            connection.close()

    def wait_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, job = self.request("GET", "/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        self.fail("background job did not finish")

    def _completed_job(self):
        status, result = self.request("POST", "/api/run", {"prompt": "/calc 6*7"})
        self.assertEqual(status, 200)
        job = self.wait_job(result["job_id"])
        self.assertEqual(job["status"], "completed")
        return job

    def test_events_endpoint_returns_full_history_by_default(self):
        job = self._completed_job()
        status, payload = self.request("GET", "/api/jobs/{}/events".format(job["job_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(payload["since"], 0)
        self.assertEqual(len(payload["events"]), payload["total"])
        self.assertGreater(payload["total"], 0)

    def test_since_returns_only_new_events(self):
        """查询串必须真正传到 api()，否则这里会返回全量。"""
        job = self._completed_job()
        total = job["total"] if "total" in job else None
        status, full = self.request("GET", "/api/jobs/{}/events".format(job["job_id"]))
        total = full["total"]
        self.assertGreater(total, 1)
        status, partial = self.request(
            "GET", "/api/jobs/{}/events?since={}".format(job["job_id"], total - 1))
        self.assertEqual(status, 200)
        self.assertEqual(len(partial["events"]), 1)
        self.assertEqual(partial["events"][0], full["events"][-1])

    def test_cursor_at_end_returns_empty_and_never_repeats(self):
        """游标等于总数时必须返回空，否则前端会反复重渲染同一批事件。"""
        job = self._completed_job()
        _, full = self.request("GET", "/api/jobs/{}/events".format(job["job_id"]))
        for cursor in (full["total"], full["total"] + 1, 99999):
            with self.subTest(cursor=cursor):
                status, payload = self.request(
                    "GET", "/api/jobs/{}/events?since={}".format(job["job_id"], cursor))
                self.assertEqual(status, 200)
                self.assertEqual(payload["events"], [])

    def test_invalid_cursor_is_rejected(self):
        job = self._completed_job()
        for value in ("abc", "-1", "1.5", "1e3"):
            with self.subTest(value=value):
                status, payload = self.request(
                    "GET", "/api/jobs/{}/events?since={}".format(job["job_id"], value))
                self.assertEqual(status, 400)
                self.assertIn("since", payload["error"])

    def test_empty_cursor_means_full_history(self):
        """`since=` 空值等同未提供，返回全量而不是报错。"""
        job = self._completed_job()
        status, payload = self.request(
            "GET", "/api/jobs/{}/events?since=".format(job["job_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(payload["since"], 0)
        self.assertEqual(len(payload["events"]), payload["total"])

    def test_unknown_job_returns_404(self):
        status, _ = self.request("GET", "/api/jobs/nonexistent/events")
        self.assertEqual(status, 404)

    def test_query_string_does_not_break_static_or_other_routes(self):
        """路由仍按不含查询串的路径匹配。"""
        for path in ("/", "/app.js", "/style.css", "/?v=1"):
            with self.subTest(path=path):
                status, _ = self.request("GET", path)
                self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/health?probe=1")
        self.assertEqual(status, 200)


class WorkspaceFileEndpointTests(unittest.TestCase):
    """工作区文件接口：上传、下载、列表，以及路径穿越防护。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "work"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "seed.txt").write_text("seed", encoding="utf-8")
        (self.base / "outside.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")
        self.environment = patch.dict("os.environ", {
            "AGENTLAB_API_KEY": "", "AGENTLAB_MODEL": "deepseek-flash",
            "AGENTLAB_BASE_URL": "https://api.deepseek.com",
            "AGENTLAB_ALLOW_DEMO": "1", "AGENTLAB_PROVIDER": "demo"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = App(self.base / "data", self.workspace)
        self.addCleanup(self.app.close)
        self.server = self.app.create_server(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.token = self.request("GET", "/api/bootstrap")[1]["csrf_token"]

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": "127.0.0.1:{}".format(self.port)}
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            headers["X-AgentLab-Token"] = self.token
            data = json.dumps(body or {}).encode("utf-8")
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            value = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, value
        finally:
            connection.close()

    def upload(self, name, content):
        import base64 as _base64
        if isinstance(content, str):
            content = content.encode("utf-8")
        return self.request("POST", "/api/files/import", {"files": [
            {"name": name, "content_base64": _base64.b64encode(content).decode("ascii")}]})

    def test_lists_seeded_file(self):
        status, payload = self.request("GET", "/api/files")
        self.assertEqual(status, 200)
        self.assertIn("seed.txt", [row["path"] for row in payload["files"]])

    def test_upload_then_list_and_download(self):
        status, payload = self.upload("note.txt", "你好")
        self.assertEqual(status, 200)
        self.assertEqual(payload["files"][0]["path"], "note.txt")

        status, listing = self.request("GET", "/api/files")
        self.assertIn("note.txt", [row["path"] for row in listing["files"]])

        status, downloaded = self.request("GET", "/api/files/note.txt")
        self.assertEqual(status, 200)
        import base64 as _base64
        self.assertEqual(_base64.b64decode(downloaded["content_base64"]).decode("utf-8"), "你好")

    def test_upload_does_not_overwrite_existing_file(self):
        self.assertEqual(self.upload("note.txt", "first")[0], 200)
        status, payload = self.upload("note.txt", "second")
        self.assertEqual(status, 400)
        self.assertIn("已存在", payload["error"])
        _, downloaded = self.request("GET", "/api/files/note.txt")
        import base64 as _base64
        self.assertEqual(_base64.b64decode(downloaded["content_base64"]).decode("utf-8"), "first")

    def test_path_traversal_attempts_are_rejected(self):
        """任何形式的上跳都不能读到工作区之外的文件。"""
        for path in ("../outside.txt", "..%2Foutside.txt", "%2e%2e%2foutside.txt",
                     "....//outside.txt", "sub/../../outside.txt", "/etc/hosts"):
            with self.subTest(path=path):
                status, payload = self.request("GET", "/api/files/" + path)
                self.assertEqual(status, 400, payload)
                self.assertNotIn("OUTSIDE-SECRET", json.dumps(payload))

    def test_upload_rejects_directory_or_hidden_names(self):
        for name in ("../evil.txt", "sub/a.txt", ".hidden", "a" * 241 + ".txt"):
            with self.subTest(name=name[:20]):
                status, _ = self.upload(name, "x")
                self.assertEqual(status, 400)

    def test_oversized_upload_is_rejected(self):
        status, payload = self.upload("big.bin", b"x" * (1024 * 1024 + 1))
        self.assertEqual(status, 400)
        self.assertIn("1 MiB", payload["error"])

    def test_failed_multi_upload_leaves_no_partial_files(self):
        """第二个文件重名时，第一个必须被回滚。"""
        self.assertEqual(self.upload("taken.txt", "original")[0], 200)
        import base64 as _base64
        status, payload = self.request("POST", "/api/files/import", {"files": [
            {"name": "fresh.txt", "content_base64": _base64.b64encode(b"new").decode()},
            {"name": "taken.txt", "content_base64": _base64.b64encode(b"dup").decode()}]})
        self.assertEqual(status, 400)
        self.assertFalse((self.workspace / "fresh.txt").exists())
        self.assertEqual((self.workspace / "taken.txt").read_text(), "original")

    def test_missing_file_returns_client_error(self):
        status, _ = self.request("GET", "/api/files/nope.txt")
        self.assertEqual(status, 400)


class WebStreamingConfigTests(unittest.TestCase):
    """streaming 配置必须被真正接线，而不只是被校验。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict("os.environ", {
            "AGENTLAB_API_KEY": "", "AGENTLAB_MODEL": "deepseek-flash",
            "AGENTLAB_BASE_URL": "https://api.deepseek.com",
            "AGENTLAB_ALLOW_DEMO": "1", "AGENTLAB_PROVIDER": "demo"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = App(Path(self.temp.name) / "data", Path(self.temp.name) / "work")
        self.addCleanup(self.app.close)

    def test_streaming_defaults_on_and_is_published(self):
        config = self.app.api("GET", "/api/bootstrap", {})["config"]
        self.assertIs(config["streaming"], True)

    def test_delta_handler_follows_configuration(self):
        job = {"events": [], "_session_ids": set(), "_delta": ""}
        self.assertIsNotNone(self.app._delta_handler(job))
        self.app._config["streaming"] = False
        self.assertIsNone(self.app._delta_handler(job))

    def test_delta_handler_accumulates_and_is_bounded(self):
        job = {"events": [], "_session_ids": set(), "_delta": ""}
        handler = self.app._delta_handler(job)
        handler("一")
        handler("二")
        self.assertEqual(job["_delta"], "一二")
        handler("")
        handler(None)
        self.assertEqual(job["_delta"], "一二")
        # 超出上限后不再增长，避免内存被单次任务撑爆。
        handler("x" * 300000)
        self.assertEqual(len(job["_delta"]), DELTA_LIMIT)

    def test_delta_is_exposed_but_internal_fields_are_not(self):
        job_id = self.app.submit(lambda _job: asyncio.sleep(0))
        with self.app._lock:
            self.app._jobs[job_id]["_delta"] = "实时内容"
        payload = self.app.job(job_id)
        self.assertEqual(payload["delta"], "实时内容")
        self.assertNotIn("_delta", payload)
        for key in payload:
            self.assertFalse(key.startswith("_"), key)

    def test_invalid_streaming_value_is_rejected(self):
        for value in ("yes", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(APIError):
                    self.app.configure({"streaming": value})

    def test_streaming_can_be_toggled_off(self):
        result = self.app.configure({"streaming": False})
        self.assertIs(result["config"]["streaming"], False)
        self.assertIs(self.app.api("GET", "/api/bootstrap", {})["config"]["streaming"], False)
