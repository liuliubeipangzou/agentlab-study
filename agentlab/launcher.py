"""供 macOS 双击入口使用：验证已有服务身份，再复用或启动本地界面。"""
import errno
import json
import socket
import sys
import urllib.error
import urllib.request
import webbrowser


HOST = "127.0.0.1"
PORT = 8765


class LauncherError(RuntimeError):
    """启动失败时可直接显示给用户的说明。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """健康检查必须由当前端口直接响应，不能跳转到其他服务。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _service_running(port=PORT):
    """只有健康响应精确匹配时返回 True；拒绝连接返回 False，其他占用报错。"""
    if type(port) is not int or not 1 <= port <= 65535:
        raise LauncherError("端口必须是 1 到 65535 之间的整数。")
    url = "http://{}:{}/api/health".format(HOST, port)
    # 回环请求不使用系统代理，防止健康检查离开本机。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=1.5) as response:
            if response.status != 200:
                raise LauncherError("端口 {} 已被其他服务占用，未启动 Agent Lab。".format(port))
            body = response.read(1025)
        if len(body) > 1024 or json.loads(body.decode("utf-8")) != {"app": "agentlab", "status": "ok"}:
            raise ValueError("unexpected service identity")
        return True
    except urllib.error.HTTPError as exc:
        exc.close()
        raise LauncherError("端口 {} 已被其他服务占用，未启动 Agent Lab。".format(port)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, OSError) and exc.reason.errno == errno.ECONNREFUSED:
            return False
        raise LauncherError("无法确认端口 {} 上的服务身份，请检查已有终端或稍后重试。".format(port)) from None
    except (socket.timeout, TimeoutError, OSError):
        raise LauncherError("端口 {} 未响应健康检查，请检查已有终端或稍后重试。".format(port)) from None
    except (ValueError, UnicodeError):
        raise LauncherError("端口 {} 已被其他服务占用，未启动 Agent Lab。".format(port)) from None


def _open_existing(port):
    """复用已验证的进程；浏览器打开失败时仍给出可访问地址。"""
    url = "http://{}:{}/".format(HOST, port)
    print("Agent Lab 已在运行，打开 {}".format(url))
    if not webbrowser.open(url):
        print("浏览器未自动打开，请访问上面的本机地址。")


def launch(data_dir=".agentlab", workspace="workspace", port=PORT):
    """重复双击复用同一端口的 Agent Lab，不杀进程、不读取或保存密钥。"""
    if _service_running(port):
        _open_existing(port)
        return 0
    from .server import serve
    try:
        serve(data_dir=data_dir, workspace=workspace, host=HOST, port=port, open_browser=True)
    except OSError as exc:
        # 两次双击可能同时通过探测；另一个实例先启动时安全复用。
        if exc.errno == errno.EADDRINUSE and _service_running(port):
            _open_existing(port)
            return 0
        raise
    return 0


def main():
    """保留终端中的简洁启动信息，Ctrl+C 结束本地服务。"""
    try:
        if sys.version_info < (3, 9):
            raise LauncherError("Agent Lab 需要 Python 3.9 或更新版本。")
        return launch()
    except (LauncherError, OSError, ValueError) as exc:
        print("启动失败：" + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAgent Lab 已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
