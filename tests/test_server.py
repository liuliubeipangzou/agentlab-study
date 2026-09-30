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
from agentlab.server import App, MAX_BODY
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
        with patch("agentlab.server.OpenAICompatibleProvider.complete", new_callable=AsyncMock) as complete:
            complete.side_effect = [ModelResponse(tool_calls=[call]), ModelResponse("done")]
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
