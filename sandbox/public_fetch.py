"""Fetch browser resources outside a network-less execution container.

Every connection uses a validated IP, retaining the original Host and TLS name.
No environment proxy, redirect following, internal destination or decompression
is permitted. The broker never forwards its own credentials to this function.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx


MAX_BODY = 1024 * 1024
MAX_UPLOAD = 64 * 1024
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}\Z")
_HOP = {"host", "connection", "proxy-connection", "proxy-authorization", "proxy-authenticate",
        "transfer-encoding", "upgrade", "te", "trailer", "keep-alive", "content-length", "accept-encoding"}


def _public(address):
    value = ipaddress.ip_address(address)
    return (value.is_global and not value.is_multicast and not value.is_reserved
            and not value.is_loopback and not value.is_link_local
            and not (isinstance(value, ipaddress.IPv6Address) and
                     (value.ipv4_mapped is not None or value.sixtofour is not None or value.teredo is not None)))


async def public_target(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ValueError("浏览器网址格式不正确")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("浏览器网址格式不正确") from None
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or "\\" in url or "%" in host or host.endswith(".")
            or port not in {None, 80, 443}):
        raise ValueError("浏览器只允许公网 HTTP(S) 网址和标准端口")
    port = port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise ValueError("浏览器主机名不正确") from None
        async with asyncio.timeout(3):
            records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    else:
        addresses = [str(literal)]
    if not addresses or len(addresses) > 32 or any(not _public(address) for address in addresses):
        raise ValueError("浏览器不能访问本机、内网或保留地址")
    ip = addresses[0]
    ip_authority = f"[{ip}]" if ":" in ip else ip
    host_authority = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port != default_port:
        ip_authority += f":{port}"
        host_authority += f":{port}"
    return urlunsplit((parsed.scheme, ip_authority, parsed.path or "/", parsed.query, "")), host_authority, host


def _headers(value):
    if not isinstance(value, dict) or len(value) > 80:
        raise ValueError("浏览器请求头过多")
    output, size = {}, 0
    for key, item in value.items():
        if (not isinstance(key, str) or not _HEADER.fullmatch(key) or not isinstance(item, str)
                or len(item) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in item)):
            raise ValueError("浏览器请求头格式不正确")
        size += len(key) + len(item)
        if size > 32 * 1024:
            raise ValueError("浏览器请求头过长")
        if key.lower() not in _HOP:
            output[key] = item
    return output


async def fetch_public(request):
    """Return one bounded resource, or a deliberately non-diagnostic error."""
    try:
        if not isinstance(request, dict) or request.keys() - {"url", "method", "headers", "body_base64"}:
            raise ValueError("浏览器资源请求不正确")
        method = request.get("method", "GET")
        if method not in _METHODS:
            raise ValueError("浏览器请求方法不支持")
        encoded = request.get("body_base64", "")
        if not isinstance(encoded, str) or len(encoded) > (MAX_UPLOAD + 2) // 3 * 4:
            raise ValueError("浏览器提交内容过大")
        body = base64.b64decode(encoded, validate=True)
        if len(body) > MAX_UPLOAD or (body and method in {"GET", "HEAD"}):
            raise ValueError("浏览器提交内容过大或格式不正确")
        headers = _headers(request.get("headers", {}))
        async with asyncio.timeout(8):
            pinned, authority, hostname = await public_target(request.get("url"))
            # A fresh client prevents connection reuse across different original
            # TLS names which happen to resolve to the same IP.
            headers = {key: value for key, value in headers.items() if key.lower() != "host"}
            headers["Host"] = authority
            headers["Accept-Encoding"] = "identity"
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                         timeout=httpx.Timeout(5, connect=3)) as client:
                async with client.stream(method, pinned, headers=headers, content=body,
                                         extensions={"sni_hostname": hostname}) as response:
                    if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                        raise ValueError("浏览器资源返回了不支持的压缩内容")
                    if "attachment" in response.headers.get("content-disposition", "").lower():
                        raise ValueError("浏览器不接受附件下载")
                    if len(response.headers) > 100:
                        raise ValueError("浏览器资源响应头过多")
                    declared = response.headers.get("content-length")
                    if declared and (not declared.isdecimal() or int(declared) > MAX_BODY):
                        raise ValueError("浏览器资源过大")
                    chunks = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(chunks) + len(chunk) > MAX_BODY:
                            raise ValueError("浏览器资源过大")
                        chunks.extend(chunk)
                    returned = {key: value for key, value in response.headers.items()
                                if key.lower() not in _HOP | {"content-encoding"}}
                    # Playwright represents repeated Set-Cookie fields with
                    # newlines. A comma would corrupt Expires and session state.
                    cookies = response.headers.get_list("set-cookie")
                    if cookies:
                        returned["set-cookie"] = "\n".join(cookies)
                    if sum(len(k) + len(v) for k, v in returned.items()) > 32 * 1024:
                        raise ValueError("浏览器资源响应头过长")
                    return {"status": response.status_code, "headers": returned,
                            "body_base64": base64.b64encode(chunks).decode("ascii")}
    except (ValueError, binascii.Error, httpx.HTTPError, OSError, TimeoutError, UnicodeError):
        # Never include upstream bodies, URLs, headers or browser credentials.
        return {"error": "公网资源不可用、被隔离规则阻止或超过大小限制"}
