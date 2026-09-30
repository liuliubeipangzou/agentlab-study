"""联网工具（web_search / fetch_url / http_request）的测试。

分两类：
- **离线用例**使用固定 HTML/JSON fixture，验证解析与错误处理，不依赖网络；
- **联网冒烟**在无法访问外网时自动跳过，避免把网络当成本地契约。
"""

import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from agentlab import netguard, web
from agentlab.tools import Tool, ToolContext, ToolRegistry, create_builtin_tools
from agentlab.types import ToolCall
from agentlab import tools as tools_module


def _network_available(host="html.duckduckgo.com", port=443, timeout=5):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


NETWORK = _network_available()

# 取自 DuckDuckGo HTML 端点真实结构的精简 fixture（含跳转包装与 HTML 实体）。
DDG_FIXTURE = """
<div class="result results_links">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fasync%2Dio%2Dpython%2F&amp;rut=abc">
       Python&#x27;s asyncio &amp; You</a>
  </h2>
  <a class="result__snippet">Write <b>concurrent</b> code with async/await.</a>
</div>
<div class="result results_links">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">
       asyncio documentation</a>
  </h2>
  <a class="result__snippet">Official asyncio reference.</a>
</div>
<div class="result results_links">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="https://example.com/plain">Plain link</a>
  </h2>
  <a class="result__snippet">No redirect wrapper.</a>
</div>
"""


class DuckDuckGoParsingTests(unittest.TestCase):
    def test_decodes_redirect_wrapper(self):
        self.assertEqual(web._decode_ddg_target("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fa"),
                         "https://x.com/a")

    def test_leaves_plain_links_untouched(self):
        self.assertEqual(web._decode_ddg_target("https://example.com/"), "https://example.com/")

    def test_protocol_relative_plain_link_gets_scheme(self):
        self.assertEqual(web._decode_ddg_target("//example.com/x"), "https://example.com/x")

    def test_parses_titles_snippets_and_urls(self):
        results = web._DDGParser.parse(DDG_FIXTURE, limit=10)
        self.assertEqual(len(results), 3)
        first = results[0]
        self.assertEqual(first["url"], "https://realpython.com/async-io-python/")
        self.assertIn("asyncio", first["title"])
        self.assertIn("concurrent", first["snippet"])
        # HTML 实体必须被还原，标签必须被剥离。
        self.assertNotIn("&#x27;", first["title"])
        self.assertNotIn("<b>", first["snippet"])
        self.assertEqual(results[2]["url"], "https://example.com/plain")

    def test_respects_limit(self):
        self.assertEqual(len(web._DDGParser.parse(DDG_FIXTURE, limit=1)), 1)

    def test_structural_change_yields_actionable_error(self):
        """页面改版时必须明确报错，而不是静默返回空列表。"""
        with patch("agentlab.web.netguard.request",
                   return_value={"status": 200, "text": "<html>no results here</html>",
                                 "content_type": "text/html", "bytes": 30,
                                 "truncated": False, "redirect_to": None, "url": "x",
                                 "final_url": "x"}):
            with self.assertRaises(web.SearchError) as caught:
                web.search("anything")
            self.assertIn("AGENTLAB_SEARCH_BACKEND", str(caught.exception))

    def test_internal_result_links_are_dropped(self):
        """返回给模型的链接也不能指向内网。"""
        html = ('<a class="result__a" href="//duckduckgo.com/l/?uddg=http%3A%2F%2F127.0.0.1%2F">'
                'localhost</a><a class="result__snippet">internal</a>'
                '<a class="result__a" href="https://www.python.org/">ok</a>'
                '<a class="result__snippet">public</a>')
        with patch("agentlab.web.netguard.request",
                   return_value={"status": 200, "text": html, "content_type": "text/html",
                                 "bytes": len(html), "truncated": False,
                                 "redirect_to": None, "url": "x", "final_url": "x"}):
            results = web.search("q")
        self.assertEqual([r["url"] for r in results], ["https://www.python.org/"])


class SearchBackendTests(unittest.TestCase):
    def test_config_precedence(self):
        self.assertEqual(web.SearchConfig().describe()["backend"], "duckduckgo")
        self.assertEqual(web.SearchConfig(api_key="k").describe()["backend"], "tavily")
        self.assertEqual(web.SearchConfig(searx_url="https://s.example").describe()["backend"],
                         "searxng")
        self.assertEqual(web.SearchConfig(backend="brave", api_key="k").describe()["backend"],
                         "brave")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(web.SearchError) as caught:
            web.search("q", config=web.SearchConfig(backend="nope"))
        self.assertIn("未知检索后端", str(caught.exception))

    def test_api_backends_require_credentials(self):
        for name in ("tavily", "brave"):
            with self.subTest(backend=name):
                with self.assertRaises(web.SearchError) as caught:
                    web.search("q", config=web.SearchConfig(backend=name))
                self.assertIn("AGENTLAB_SEARCH_API_KEY", str(caught.exception))

    def test_searxng_backend_parses_json(self):
        payload = json.dumps({"results": [
            {"title": "T1", "url": "https://a.example/", "content": "C1"},
            {"title": "T2", "url": "https://b.example/", "content": "C2"},
        ]})
        with patch("agentlab.web.netguard.request",
                   return_value={"status": 200, "text": payload,
                                 "content_type": "application/json", "bytes": len(payload),
                                 "truncated": False, "redirect_to": None, "url": "x",
                                 "final_url": "x"}):
            results = web.search("q", config=web.SearchConfig(backend="searxng",
                                                              searx_url="https://s.example"))
        self.assertEqual([r["url"] for r in results], ["https://a.example/", "https://b.example/"])

    def test_search_parameter_validation(self):
        for query, limit in [("", 5), ("   ", 5), ("ok", 0), ("ok", 21), ("ok", "5"),
                             ("x" * 1001, 5)]:
            with self.subTest(query=query[:10], limit=limit):
                with self.assertRaises(web.SearchError):
                    web.search(query, limit=limit)


class FetchTests(unittest.TestCase):
    def test_fetch_rejects_internal_target(self):
        for url in ("http://127.0.0.1/", "http://169.254.169.254/", "http://10.0.0.1/"):
            with self.subTest(url=url):
                with self.assertRaises(web.SearchError) as caught:
                    web.fetch(url)
                self.assertIn("安全策略", str(caught.exception))

    def test_fetch_reports_redirect_instead_of_following(self):
        with patch("agentlab.web.netguard.request",
                   return_value={"status": 301, "text": "", "content_type": "", "bytes": 0,
                                 "truncated": False, "redirect_to": "https://final.example/",
                                 "url": "http://start.example/", "final_url": "http://start.example/"}):
            with self.assertRaises(web.SearchError) as caught:
                web.fetch("http://start.example/")
            self.assertIn("https://final.example/", str(caught.exception))

    def test_fetch_reports_non_text_content(self):
        with patch("agentlab.web.netguard.request",
                   return_value={"status": 200, "text": "", "content_type": "application/pdf",
                                 "bytes": 1000, "truncated": False, "redirect_to": None,
                                 "url": "x", "final_url": "x"}):
            with self.assertRaises(web.SearchError) as caught:
                web.fetch("https://x.example/file.pdf")
            self.assertIn("非文本内容", str(caught.exception))

    def test_fetch_adds_scheme_when_missing(self):
        captured = {}

        def fake(url, **kwargs):
            captured["url"] = url
            return {"status": 200, "text": "<title>T</title><p>body</p>",
                    "content_type": "text/html", "bytes": 30, "truncated": False,
                    "redirect_to": None, "url": url, "final_url": url}

        with patch("agentlab.web.netguard.request", side_effect=fake):
            result = web.fetch("example.com/page")
        self.assertEqual(captured["url"], "https://example.com/page")
        self.assertEqual(result["title"], "T")

    def test_fetch_parameter_validation(self):
        with self.assertRaises(web.SearchError):
            web.fetch("https://example.com/", limit=10)
        with self.assertRaises(web.SearchError):
            web.fetch("")


class ToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """新工具必须能被注册表校验并执行，且失败要作为结构化结果返回。"""

    async def _execute(self, name, arguments, settings=None):
        registry = create_builtin_tools()
        context = ToolContext(workspace=tempfile.gettempdir(), max_output_chars=8000,
                              settings=settings)
        return await registry.execute(ToolCall(name, arguments), context, approved=True)

    async def test_run_python_success_and_failure_shapes(self):
        ok = await self._execute("run_python", {"code": "result = 21 * 2"})
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["value"]["result"], 42)

        bad = await self._execute("run_python", {"code": "raise ValueError('boom')"})
        # 失败必须在工具层表现为 ok=false，否则模型会误判为成功。
        self.assertFalse(bad["ok"])
        self.assertIn("ValueError", bad["error"])

    async def test_run_python_timeout_surfaces_as_tool_failure(self):
        result = await self._execute("run_python",
                                    {"code": "import time\ntime.sleep(30)", "timeout": 1.5})
        self.assertFalse(result["ok"])
        self.assertIn("超过", result["error"])

    async def test_http_request_blocks_internal_address(self):
        result = await self._execute("http_request", {"url": "http://169.254.169.254/"})
        self.assertFalse(result["ok"])
        self.assertIn("受保护网段", result["error"])

    async def test_http_request_rejects_get_with_body(self):
        result = await self._execute("http_request",
                                    {"url": "https://example.com/", "body": "{}"})
        self.assertFalse(result["ok"])
        self.assertIn("body", result["error"])

    async def test_fetch_url_blocks_internal_address(self):
        result = await self._execute("fetch_url", {"url": "http://127.0.0.1:8765/"})
        self.assertFalse(result["ok"])
        self.assertIn("安全策略", result["error"])

    async def test_schema_validation_rejects_bad_arguments(self):
        result = await self._execute("web_search", {"query": "", "limit": 99})
        self.assertFalse(result["ok"])

    async def test_search_settings_are_forwarded(self):
        captured = {}

        def fake_search(query, limit, config):
            captured["backend"] = config.backend
            captured["api_key"] = config.api_key
            return [{"title": "t", "url": "https://x.example/", "snippet": "s",
                     "source": "test"}]

        with patch.object(tools_module.web, "search", side_effect=fake_search):
            result = await self._execute(
                "web_search", {"query": "q"},
                settings={"search": {"backend": "brave", "api_key": "secret-key"}})
        self.assertTrue(result["ok"])
        self.assertEqual(captured["backend"], "brave")
        self.assertEqual(captured["api_key"], "secret-key")


@unittest.skipUnless(NETWORK, "当前环境无法访问外网")
class LiveNetworkSmokeTests(unittest.TestCase):
    """真实联网冒烟；网络不可用时自动跳过。"""

    def test_live_search_returns_results(self):
        results = web.search("python asyncio", limit=3)
        self.assertGreaterEqual(len(results), 1)
        for item in results:
            self.assertTrue(item["url"].startswith("http"))
            self.assertTrue(item["title"])

    def test_live_fetch_extracts_text(self):
        result = web.fetch("https://example.com/", limit=500)
        self.assertIn("Example Domain", result["title"])
        self.assertTrue(result["text"].strip())

    def test_live_request_reports_status(self):
        response = netguard.request("https://example.com/", timeout=20)
        self.assertEqual(response["status"], 200)
        self.assertGreater(response["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
