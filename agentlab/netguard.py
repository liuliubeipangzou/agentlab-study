"""面向模型可调用网络工具的安全出口（SSRF 防护、禁重定向、响应限额）。

模型可以构造任意 URL，因此这一层是联网工具的安全边界。它做四件事：

1. **解析并校验 IP**：拒绝回环、私网、链路本地、保留网段、组播，以及云厂商
   元数据地址（169.254.169.254 / fd00:ec2::254）。DNS 解析出的**每一个**地址都
   必须通过检查，否则整体拒绝。
2. **连到已校验的 IP**：校验过的地址直接作为连接目标，避免"先检查后连接"之间
   的 DNS 重绑定窗口。Host 头仍使用原主机名。
3. **禁止重定向**：重定向目标必须重新走完整校验，且默认不跟随，避免借 302 绕过。
4. **限额**：响应体大小、超时、解压后大小都有上限，防止内存耗尽。

不使用系统代理：代理会让流量离开本机，使地址校验失去意义。

本模块只使用标准库。已知边界见 `docs/tools.md`：它是教学级的应用层防线，
不是操作系统沙箱；能执行任意本地代码的工具（如 run_python）在子进程内仍可
绕过它直接发起连接。
"""

import ipaddress
import http.client
import json
import math
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
MAX_REDIRECTS = 3

# 显式列出的额外封锁网段：ipaddress 的 is_private 在各 Python 版本上口径不完全一致，
# 且这些网段在生产环境里确实不该被模型访问。
_EXTRA_BLOCKED_V4 = (
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT / 运营商级 NAT
    ipaddress.ip_network("192.0.0.0/24"),     # IETF 协议分配
    ipaddress.ip_network("198.18.0.0/15"),    # 基准测试网段
    ipaddress.ip_network("169.254.169.254/32"),  # 云元数据（AWS/GCP/Azure 兼容）
)
_EXTRA_BLOCKED_V6 = (
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS over IPv6
)

_ALLOWED_SCHEMES = ("http", "https")


class NetGuardError(ValueError):
    """URL 或远端响应未通过安全策略；消息可直接展示给用户和模型。"""


def _blocked(address):
    """判断单个 IP 是否属于禁止访问的范围。"""
    if address.version == 6 and address.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 这类映射地址按 IPv4 规则判断。
        return _blocked(address.ipv4_mapped)
    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified):
        return True
    pool = _EXTRA_BLOCKED_V4 if address.version == 4 else _EXTRA_BLOCKED_V6
    return any(address in network for network in pool)


def check_url(url, allow_private=False):
    """校验 URL 形式与目标地址；返回 (规范化URL, 解析出的IP列表)。

    `allow_private` 仅供本机自测（例如访问 127.0.0.1 上的本地服务）使用，产品路径
    不应打开它。
    """
    if not isinstance(url, str) or not url.strip():
        raise NetGuardError("URL 不能为空")
    if len(url) > 4096:
        raise NetGuardError("URL 过长")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise NetGuardError("URL 不能包含控制字符")
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        raise NetGuardError("URL 格式无效") from None
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise NetGuardError("仅允许 http 与 https，收到：" + (parts.scheme or "(无)"))
    if parts.username is not None or parts.password is not None:
        raise NetGuardError("URL 不能内嵌用户名或密码")
    host = parts.hostname
    if not host:
        raise NetGuardError("URL 缺少主机名")
    try:
        port = parts.port
    except ValueError:
        raise NetGuardError("URL 端口无效") from None
    if port is not None and not 1 <= port <= 65535:
        raise NetGuardError("URL 端口超出范围")

    if allow_private:
        return url.strip(), []

    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        # 字面量地址：无需 DNS，直接判定。
        if _blocked(literal):
            raise NetGuardError("目标地址属于受保护网段，已拒绝访问：" + host)
        return url.strip(), [literal]

    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise NetGuardError("无法解析主机名：" + host) from None
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise NetGuardError("主机名没有可用的 IP 地址：" + host)
    for address in addresses:
        if _blocked(address):
            raise NetGuardError("主机名 {} 解析到受保护网段 {}，已拒绝访问".format(host, address))
    return url.strip(), addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """把连接目标固定为已校验的 IP，同时在 TLS 握手与 Host 头中使用原主机名。

    这样证书校验仍然针对真实主机名，而实际 TCP 连接不会再去解析一次 DNS。
    """

    def __init__(self, host, port=None, *, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, **kwargs)

    def connect(self):
        if self._pinned_ip is None:
            return super().connect()
        self.sock = socket.create_connection(
            (str(self._pinned_ip), self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            server_hostname = self._tunnel_host
            self._tunnel()
        else:
            server_hostname = self.host
        context = self._context if self._context is not None else ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname=server_hostname)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP 同样固定 IP；Host 头由 urllib 依据原 URL 生成。"""

    def __init__(self, host, port=None, *, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, **kwargs)

    def connect(self):
        if self._pinned_ip is None:
            return super().connect()
        self.sock = socket.create_connection(
            (str(self._pinned_ip), self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned):
        super().__init__()
        self._pinned = pinned

    def http_open(self, req):
        pinned = self._pinned
        def factory(host, **kwargs):
            return _PinnedHTTPConnection(host, pinned_ip=pinned, **kwargs)
        return self.do_open(factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned, context):
        super().__init__(context=context)
        self._pinned = pinned

    def https_open(self, req):
        pinned = self._pinned
        context = self._context
        def factory(host, **kwargs):
            return _PinnedHTTPSConnection(host, pinned_ip=pinned, context=context, **kwargs)
        return self.do_open(factory, req)


def _pick_address(addresses, prefer_ipv4=True):
    """优先使用 IPv4，避免在无 IPv6 出口的环境中连接失败。"""
    if not addresses:
        return None
    for address in addresses:
        if address.version == 4:
            return address
    return addresses[0]


def build_opener(pinned_ip, timeout, allow_private=False):
    """构造不使用代理、禁止自动重定向、连接目标固定的 opener。"""
    context = ssl.create_default_context()
    # 重定向必须由我们逐跳校验，因此返回 None 直接阻断自动跟随。
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    handlers = [
        urllib.request.ProxyHandler({}),      # 明确禁用环境代理
        _NoRedirect(),
        _PinnedHTTPHandler(pinned_ip),
        _PinnedHTTPSHandler(pinned_ip, context),
    ]
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = []
    return opener


def _decode_body(raw, charset_hint=None):
    meta_charset = None
    if charset_hint:
        meta_charset = charset_hint.strip().strip('"').lower() or None
    for encoding in (meta_charset, "utf-8", "gb18030", "latin-1"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def request(url, method="GET", headers=None, body=None, timeout=DEFAULT_TIMEOUT,
            max_bytes=DEFAULT_MAX_BYTES, allow_private=False, max_redirects=0):
    """发起一次受控的 HTTP 请求。

    返回 dict：status / url / final_url / content_type / text / bytes / truncated /
    redirect_to。默认不跟随重定向，而是把 3xx 目标作为 `redirect_to` 返回，由调用方
    （或模型）显式决定是否继续，从而让每一跳都重新经过校验。
    """
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise NetGuardError("不支持的 HTTP 方法：" + str(method))
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
        raise NetGuardError("timeout 必须是 0 到 120 之间的有限正数")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 16 * 1024 * 1024:
        raise NetGuardError("max_bytes 必须在 1024 到 16 MiB 之间")
    if headers is not None and not isinstance(headers, dict):
        raise NetGuardError("headers 必须是对象")
    if body is not None and not isinstance(body, (str, bytes)):
        raise NetGuardError("body 必须是字符串")

    current = url
    hops = 0
    while True:
        normalized, addresses = check_url(current, allow_private=allow_private)
        pinned = _pick_address(addresses)
        opener = build_opener(pinned, timeout, allow_private)
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(normalized, data=data, method=method)
        req.add_header("User-Agent", "AgentLab/0.2 (+local agent)")
        req.add_header("Accept", "*/*")
        for name, value in (headers or {}).items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise NetGuardError("header 名和值都必须是字符串")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in name + value):
                raise NetGuardError("header 不能包含控制字符")
            lowered = name.lower()
            if lowered in ("host", "content-length", "connection", "transfer-encoding"):
                # 这些由底层实现决定，模型不得覆盖，避免请求走私与主机头欺骗。
                raise NetGuardError("不允许自定义 header：" + name)
            req.add_header(name, value)
        if data is not None and not any(k.lower() == "content-type" for k in (headers or {})):
            req.add_header("Content-Type", "application/json")

        try:
            with opener.open(req, timeout=timeout) as response:
                return _read_response(response, normalized, max_bytes)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code <= 399:
                location = exc.headers.get("Location") if exc.headers else None
                exc.close()
                if not location:
                    raise NetGuardError("服务端返回 {} 但没有 Location 头".format(exc.code)) from None
                target = urllib.parse.urljoin(normalized, location)
                if hops >= min(max_redirects, MAX_REDIRECTS):
                    # 不自动跟随：把目标交回调用方，下一跳会重新校验地址。
                    return {"status": exc.code, "url": normalized, "final_url": normalized,
                            "content_type": "", "text": "", "bytes": 0, "truncated": False,
                            "redirect_to": target}
                hops += 1
                current = target
                continue
            detail = ""
            try:
                raw = exc.read(2048)
                detail = raw.decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            code = exc.code
            exc.close()
            raise NetGuardError("远端返回 HTTP {}：{}".format(code, detail[:200] if detail else "(无正文)")) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise NetGuardError("请求失败：{}".format(type(reason).__name__)) from None
        except (socket.timeout, TimeoutError):
            raise NetGuardError("请求超时（{} 秒）".format(timeout)) from None
        except ssl.SSLError:
            raise NetGuardError("TLS 握手失败，目标证书可能无效") from None


def _read_response(response, requested_url, max_bytes):
    raw = response.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    content_type = response.headers.get("Content-Type", "") or ""
    charset = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("charset="):
            charset = piece.split("=", 1)[1]
    base_type = content_type.split(";")[0].strip().lower()
    textual = (base_type.startswith("text/") or base_type in (
        "application/json", "application/xml", "application/javascript",
        "application/x-ndjson", "application/rss+xml", "application/atom+xml")
        or base_type.endswith("+json") or base_type.endswith("+xml"))
    text = _decode_body(raw, charset) if textual else ""
    return {"status": response.status, "url": requested_url,
            "final_url": response.geturl(), "content_type": base_type,
            "text": text, "bytes": len(raw), "truncated": truncated,
            "redirect_to": None}


def parse_json(text):
    """严格解析 JSON：拒绝重复键、NaN/Infinity 与非有限浮点。"""
    def reject(value):
        raise NetGuardError("JSON 含非有限数值：" + str(value))
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise NetGuardError("JSON 含重复键：" + str(key))
            result[key] = item
        return result
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise NetGuardError("JSON 数值超出有限范围")
        return number
    try:
        return json.loads(text, parse_constant=reject, parse_float=finite, object_pairs_hook=pairs)
    except NetGuardError:
        raise
    except (ValueError, RecursionError) as exc:
        raise NetGuardError("响应不是有效 JSON：" + type(exc).__name__) from None


class _TextExtractor(HTMLParser):
    """把 HTML 转成可读文本；丢弃脚本、样式与注释。"""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "header", "footer",
              "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip_depth:
            return
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped + " ")

    def text(self):
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)


def html_to_text(html, limit=20000):
    """HTML → 纯文本。解析失败时退化为粗略去标签，绝不抛异常。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    text = parser.text()
    if not text:
        # 退化路径：去掉标签与 script/style 内容。
        import re
        without_blocks = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", without_blocks)
        text = " ".join(text.split())
    title = " ".join(parser.title.split())
    if len(text) > limit:
        text = text[:limit] + "\n…（内容已截断）"
    return {"title": title, "text": text}
