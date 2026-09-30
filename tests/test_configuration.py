"""配置 API 与"真实模型为默认"行为的回归测试。

覆盖：
- 缺少 API Key 时，所有会调用模型的入口都必须同步拒绝（不是提交一个必然失败的任务）；
- 默认 provider 是真实模型，演示模式必须显式开启；
- 检索后端配置能保存并下发到工具上下文；
- 密钥永远不出现在响应里。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agentlab.server import APIError, App


class ConfigurationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = self.temp.name
        self.environment = patch.dict("os.environ", {
            "AGENTLAB_API_KEY": "", "AGENTLAB_MODEL": "deepseek-flash",
            "AGENTLAB_BASE_URL": "https://api.deepseek.com"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ("AGENTLAB_ALLOW_DEMO", "AGENTLAB_PROVIDER",
                     "AGENTLAB_SEARCH_BACKEND", "AGENTLAB_SEARCH_API_KEY", "AGENTLAB_SEARX_URL"):
            os.environ.pop(name, None)

    def make_app(self, **environment):
        for name, value in environment.items():
            os.environ[name] = value
        app = App(data_dir=os.path.join(self.base, "data"), workspace=os.path.join(self.base, "work"))
        self.addCleanup(app.close)
        return app

    def test_default_provider_is_a_real_model(self):
        config = self.make_app().api("GET", "/api/bootstrap", {})["config"]
        self.assertEqual(config["provider"], "openai")
        self.assertFalse(config["has_api_key"])
        self.assertFalse(config["allow_demo"])

    def test_missing_api_key_blocks_every_model_entrypoint(self):
        """提交阶段就拒绝，避免用户拿到一个必然失败的后台任务。"""
        app = self.make_app()
        for path, payload in (("/api/run", {"prompt": "hi"}),
                              ("/api/connection-test", {}),
                              ("/api/workflow", {})):
            with self.subTest(path=path):
                with self.assertRaises(APIError) as caught:
                    app.api("POST", path, payload)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("API Key", caught.exception.message)

    def test_demo_cannot_be_selected_unless_enabled(self):
        app = self.make_app()
        with self.assertRaises(APIError) as caught:
            app.configure({"provider": "demo"})
        self.assertIn("演示模式已停用", caught.exception.message)

    def test_demo_becomes_available_when_explicitly_enabled(self):
        app = self.make_app(AGENTLAB_ALLOW_DEMO="1", AGENTLAB_PROVIDER="demo")
        config = app.api("GET", "/api/bootstrap", {})["config"]
        self.assertEqual(config["provider"], "demo")
        self.assertTrue(app.provider_ready())
        # 演示模式下提交任务不会被凭据检查拦下。
        result = app.api("POST", "/api/run", {"prompt": "/calc 2*3"})
        self.assertIn("job_id", result)

    def test_saving_key_enables_provider_and_is_never_echoed(self):
        app = self.make_app()
        secret = "sk-live-must-not-leak-12345"
        result = app.configure({"provider": "openai", "model": "deepseek-flash",
                                "base_url": "https://api.deepseek.com", "api_key": secret})
        self.assertTrue(result["config"]["has_api_key"])
        self.assertTrue(app.provider_ready())
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        bootstrap = app.api("GET", "/api/bootstrap", {})
        self.assertNotIn(secret, json.dumps(bootstrap, ensure_ascii=False))

    def test_invalid_configuration_is_rejected(self):
        app = self.make_app()
        cases = [
            ({"provider": "bogus"}, "provider"),
            ({"provider": "openai", "base_url": "http://evil.example.com"}, "base_url"),
            ({"provider": "openai", "base_url": "https://api.deepseek.com", "api_key": 123}, "API Key"),
        ]
        for payload, fragment in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(APIError) as caught:
                    app.configure(payload)
                self.assertIn(fragment, caught.exception.message)


class SearchSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict("os.environ", {
            "AGENTLAB_API_KEY": "", "AGENTLAB_MODEL": "deepseek-flash",
            "AGENTLAB_BASE_URL": "https://api.deepseek.com"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ("AGENTLAB_ALLOW_DEMO", "AGENTLAB_PROVIDER",
                     "AGENTLAB_SEARCH_BACKEND", "AGENTLAB_SEARCH_API_KEY", "AGENTLAB_SEARX_URL"):
            os.environ.pop(name, None)
        self.app = App(data_dir=os.path.join(self.temp.name, "data"),
                       workspace=os.path.join(self.temp.name, "work"))
        self.addCleanup(self.app.close)

    def _agent_settings(self):
        return self.app._agent({"events": [], "_session_ids": set(), "run_id": "r"}).tool_settings

    def test_defaults_to_no_explicit_backend(self):
        """留空时由 web 模块自行选择免密钥后端。"""
        config = self.app.api("GET", "/api/bootstrap", {})["config"]
        self.assertEqual(config["search_backend"], "")
        self.assertFalse(config["has_search_key"])

    def test_search_settings_round_trip_and_reach_tools(self):
        secret = "brave-search-secret-key"
        # _agent() 需要可用的 provider，因此先配置模型凭据。
        self.app.configure({"provider": "openai", "model": "deepseek-flash",
                            "base_url": "https://api.deepseek.com", "api_key": "sk-provider-key"})
        result = self.app.configure({"search_backend": "brave",
                                     "search_api_key": secret,
                                     "searx_url": "https://searx.example.com"})
        config = result["config"]
        self.assertEqual(config["search_backend"], "brave")
        self.assertTrue(config["has_search_key"])
        self.assertEqual(config["searx_url"], "https://searx.example.com")
        settings = self._agent_settings()
        self.assertEqual(settings["search"]["backend"], "brave")
        self.assertEqual(settings["search"]["api_key"], secret)
        # 检索 Key 同样必须脱敏。
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))

    def test_search_settings_load_from_environment(self):
        os.environ["AGENTLAB_SEARCH_BACKEND"] = "tavily"
        os.environ["AGENTLAB_SEARCH_API_KEY"] = "env-search-key"
        os.environ["AGENTLAB_API_KEY"] = "sk-provider-key"
        app = App(data_dir=os.path.join(self.temp.name, "data2"),
                  workspace=os.path.join(self.temp.name, "work2"))
        self.addCleanup(app.close)
        settings = app._agent({"events": [], "_session_ids": set(), "run_id": "r"}).tool_settings
        self.assertEqual(settings["search"]["backend"], "tavily")
        self.assertEqual(settings["search"]["api_key"], "env-search-key")

    def test_invalid_search_field_is_rejected(self):
        with self.assertRaises(APIError):
            self.app.configure({"search_backend": "x" * 100})


if __name__ == "__main__":
    unittest.main()
