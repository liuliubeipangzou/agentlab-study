"""netguard 的安全回归测试。

这些用例是联网工具的安全底线：任何一条失败都意味着模型可以访问内网或云元数据。
"""

import socket
import unittest
from unittest.mock import patch

from agentlab import netguard


class UrlValidationTests(unittest.TestCase):
    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/",
                    "data:text/plain,hi", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.check_url(url)

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(netguard.NetGuardError):
            netguard.check_url("https://user:secret@example.com/")

    def test_rejects_control_characters_and_empty(self):
        for url in ("", "   ", "https://exa\nmple.com/", "https://example.com/\x00"):
            with self.subTest(url=repr(url)):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.check_url(url)

    def test_rejects_invalid_port(self):
        with self.assertRaises(netguard.NetGuardError):
            netguard.check_url("http://example.com:99999/")


class InternalAddressTests(unittest.TestCase):
    """字面量地址：无需 DNS，必须在解析前就被拒绝。"""

    def test_blocks_loopback_and_private_ranges(self):
        blocked = [
            "http://127.0.0.1/", "http://127.0.0.1:8765/api/health",
            "http://10.0.0.1/", "http://10.255.255.254/",
            "http://192.168.0.1/", "http://192.168.255.255/",
            "http://172.16.0.1/", "http://172.31.255.254/",
            "http://0.0.0.0/", "http://100.64.0.1/",      # CGNAT
            "http://198.18.0.1/",                          # 基准测试网段
        ]
        for url in blocked:
            with self.subTest(url=url):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.check_url(url)

    def test_blocks_ipv6_internal_addresses(self):
        for url in ("http://[::1]/", "http://[fd00::1]/", "http://[fe80::1]/",
                    "http://[::ffff:127.0.0.1]/"):
            with self.subTest(url=url):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.check_url(url)

    def test_blocks_cloud_metadata_endpoints(self):
        """云元数据是 SSRF 最有价值的攻击目标。"""
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "http://169.254.169.254/computeMetadata/v1/",
                    "http://[fd00:ec2::254]/"):
            with self.subTest(url=url):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.check_url(url)

    def test_hostname_resolving_to_private_is_blocked(self):
        """DNS 解析到内网同样必须拒绝（防 DNS 重绑定/内网域名）。"""
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 80))]
        with patch("agentlab.netguard.socket.getaddrinfo", return_value=fake):
            with self.assertRaises(netguard.NetGuardError):
                netguard.check_url("http://internal.example.com/")

    def test_any_private_address_in_resolution_blocks_the_host(self):
        """只要有一个解析结果落在内网就整体拒绝，不能"挑一个公网的用"。"""
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch("agentlab.netguard.socket.getaddrinfo", return_value=fake):
            with self.assertRaises(netguard.NetGuardError):
                netguard.check_url("http://mixed.example.com/")

    def test_public_literal_is_allowed(self):
        normalized, addresses = netguard.check_url("https://93.184.216.34/")
        self.assertEqual(normalized, "https://93.184.216.34/")
        self.assertEqual([str(a) for a in addresses], ["93.184.216.34"])

    def test_allow_private_escape_hatch_is_explicit(self):
        normalized, addresses = netguard.check_url("http://127.0.0.1:8000/", allow_private=True)
        self.assertEqual(normalized, "http://127.0.0.1:8000/")
        self.assertEqual(addresses, [])


class RequestGuardTests(unittest.TestCase):
    def test_forbidden_headers_are_rejected(self):
        """Host/Content-Length 等由底层决定，模型不得覆盖。"""
        for header in ({"Host": "evil.com"}, {"Content-Length": "5"},
                       {"Connection": "keep-alive"}, {"Transfer-Encoding": "chunked"}):
            with self.subTest(header=header):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.request("https://example.com/", headers=header, timeout=5)

    def test_header_control_characters_are_rejected(self):
        with self.assertRaises(netguard.NetGuardError):
            netguard.request("https://example.com/",
                             headers={"X-Test": "value\r\nInjected: 1"}, timeout=5)

    def test_parameter_validation(self):
        cases = [
            ({"method": "TRACE"}, "不支持的 HTTP 方法"),
            ({"timeout": 0}, "timeout"),
            ({"timeout": 999}, "timeout"),
            ({"max_bytes": 10}, "max_bytes"),
            ({"max_bytes": 99 * 1024 * 1024}, "max_bytes"),
            ({"headers": ["not", "a", "dict"]}, "headers"),
            ({"body": {"not": "a string"}}, "body"),
        ]
        for kwargs, fragment in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(netguard.NetGuardError) as caught:
                    netguard.request("https://example.com/", **kwargs)
                self.assertIn(fragment, str(caught.exception))

    def test_request_to_internal_target_never_opens_a_socket(self):
        """拦截必须发生在建立连接之前。"""
        with patch("agentlab.netguard.socket.create_connection") as connect:
            with self.assertRaises(netguard.NetGuardError):
                netguard.request("http://169.254.169.254/", timeout=5)
            connect.assert_not_called()


class StrictJsonTests(unittest.TestCase):
    def test_rejects_duplicate_keys(self):
        with self.assertRaises(netguard.NetGuardError):
            netguard.parse_json('{"a": 1, "a": 2}')

    def test_rejects_non_finite_numbers(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e999}'):
            with self.subTest(text=text):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.parse_json(text)

    def test_accepts_valid_json(self):
        self.assertEqual(netguard.parse_json('{"a": [1, 2.5, "x", true, null]}'),
                         {"a": [1, 2.5, "x", True, None]})


class HtmlExtractionTests(unittest.TestCase):
    def test_extracts_title_and_text(self):
        html = ("<html><head><title>标题 A</title><style>p{color:red}</style></head>"
                "<body><h1>Heading</h1><p>First   paragraph</p>"
                "<script>var x = 'should not appear';</script>"
                "<p>Second</p></body></html>")
        result = netguard.html_to_text(html)
        self.assertEqual(result["title"], "标题 A")
        self.assertIn("Heading", result["text"])
        self.assertIn("First paragraph", result["text"])
        self.assertNotIn("should not appear", result["text"])
        self.assertNotIn("color:red", result["text"])

    def test_truncates_long_text(self):
        result = netguard.html_to_text("<p>" + "x" * 5000 + "</p>", limit=100)
        self.assertLess(len(result["text"]), 400)
        self.assertIn("截断", result["text"])

    def test_malformed_html_does_not_raise(self):
        result = netguard.html_to_text("<p>unclosed <b>bold <div><span>")
        self.assertIsInstance(result["text"], str)

    def test_script_only_page_yields_empty_text(self):
        result = netguard.html_to_text("<script>alert(1)</script>")
        self.assertNotIn("alert", result["text"])


if __name__ == "__main__":
    unittest.main()
