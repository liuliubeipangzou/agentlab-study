"""桌面入口的隔离测试：所有端口探测、浏览器和服务启动都使用模拟对象。"""
import errno
import io
import json
import socket
import types
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from agentlab import cli, launcher


class ServeCLITests(unittest.TestCase):
    def test_serve_parser_defaults_and_custom_settings(self):
        default = cli.parser().parse_args(["serve"])
        self.assertEqual(default.port, 8765)
        self.assertFalse(default.open)
        selected = cli.parser().parse_args([
            "--data-dir", "state folder", "--workspace", "work folder",
            "serve", "--port", "9876", "--open",
        ])
        self.assertEqual(selected.command, "serve")
        self.assertEqual(selected.port, 9876)
        self.assertTrue(selected.open)
        self.assertEqual(selected.data_dir, "state folder")
        self.assertEqual(selected.workspace, "work folder")

    def test_serve_dispatch_is_synchronous_and_loopback_only(self):
        fake_server = types.ModuleType("agentlab.server")
        fake_server.serve = MagicMock()
        with patch.dict("sys.modules", {"agentlab.server": fake_server}), \
                patch.object(cli.asyncio, "run") as async_run:
            code = cli.main(["--data-dir", "custom-data", "--workspace", "custom-work",
                             "serve", "--port", "9876", "--open"])
        self.assertEqual(code, 0)
        async_run.assert_not_called()
        fake_server.serve.assert_called_once_with(data_dir="custom-data", workspace="custom-work",
                                                  host="127.0.0.1", port=9876, open_browser=True)


class LauncherTests(unittest.TestCase):
    def fake_server(self):
        server = types.ModuleType("agentlab.server")
        server.serve = MagicMock()
        return server

    def opener(self, payload):
        response = MagicMock()
        response.status = 200
        response.read.return_value = payload
        response.__enter__.return_value = response
        result = MagicMock()
        result.open.return_value = response
        return result

    def test_health_requires_exact_application_identity(self):
        valid = self.opener(b'{"app":"agentlab","status":"ok"}')
        with patch.object(launcher.urllib.request, "build_opener", return_value=valid):
            self.assertTrue(launcher._service_running())
        request = valid.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/health")
        self.assertEqual(valid.open.call_args.kwargs["timeout"], 1.5)
        for body in (b"not JSON", b'{"app":"other","status":"ok"}',
                     b'{"app":"agentlab","status":"starting"}', b"x" * 1025,
                     json.dumps({"app": "agentlab", "status": "ok", "extra": True}).encode()):
            with self.subTest(body=body[:100]), \
                    patch.object(launcher.urllib.request, "build_opener", return_value=self.opener(body)), \
                    self.assertRaises(launcher.LauncherError):
                launcher._service_running()

    def test_connection_refused_means_not_running_but_timeout_is_ambiguous(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
        with patch.object(launcher.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(launcher._service_running())
        opener.open.side_effect = urllib.error.URLError(socket.timeout("timed out"))
        with patch.object(launcher.urllib.request, "build_opener", return_value=opener), \
                self.assertRaises(launcher.LauncherError):
            launcher._service_running()

    def test_redirect_cannot_impersonate_local_health(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:8765/api/health", 302, "redirect", {}, io.BytesIO())
        with patch.object(launcher.urllib.request, "build_opener", return_value=opener), \
                self.assertRaises(launcher.LauncherError):
            launcher._service_running()
        self.assertIsNone(launcher._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_existing_service_opens_browser_without_starting_another(self):
        server = self.fake_server()
        with patch.dict("sys.modules", {"agentlab.server": server}), \
                patch.object(launcher, "_service_running", return_value=True), \
                patch.object(launcher.webbrowser, "open", return_value=True) as browser, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(launcher.launch(), 0)
        server.serve.assert_not_called()
        browser.assert_called_once_with("http://127.0.0.1:8765/")

    def test_new_service_receives_custom_directories_and_opens_browser(self):
        server = self.fake_server()
        with patch.dict("sys.modules", {"agentlab.server": server}), \
                patch.object(launcher, "_service_running", return_value=False):
            self.assertEqual(launcher.launch(data_dir="state", workspace="files", port=9876), 0)
        server.serve.assert_called_once_with(data_dir="state", workspace="files", host="127.0.0.1",
                                             port=9876, open_browser=True)

    def test_concurrent_double_click_reuses_newly_started_service(self):
        server = self.fake_server()
        server.serve.side_effect = OSError(errno.EADDRINUSE, "in use")
        with patch.dict("sys.modules", {"agentlab.server": server}), \
                patch.object(launcher, "_service_running", side_effect=[False, True]), \
                patch.object(launcher.webbrowser, "open", return_value=True) as browser, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(launcher.launch(), 0)
        self.assertEqual(server.serve.call_count, 1)
        browser.assert_called_once_with("http://127.0.0.1:8765/")

    def test_unknown_occupant_is_neither_started_over_nor_opened(self):
        server = self.fake_server()
        with patch.dict("sys.modules", {"agentlab.server": server}), \
                patch.object(launcher, "_service_running", side_effect=launcher.LauncherError("其他服务占用端口")), \
                patch.object(launcher.webbrowser, "open") as browser, \
                self.assertRaises(launcher.LauncherError):
            launcher.launch()
        server.serve.assert_not_called()
        browser.assert_not_called()

    def test_invalid_port_fails_before_any_network(self):
        with patch.object(launcher.urllib.request, "build_opener") as opener:
            for port in (0, -1, 65536, True, "8765"):
                with self.subTest(port=port), self.assertRaises(launcher.LauncherError):
                    launcher._service_running(port)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
