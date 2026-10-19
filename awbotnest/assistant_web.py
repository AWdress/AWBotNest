"""Opt-in public web search and text reading, without a browser or credentials."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .assistant_mcp import MCPHttpTransport, public_endpoint, validate_endpoint
from .logs import redact_secrets

MAX_BYTES = 256 * 1024
MAX_TEXT = 16000
PAGE_CHARS = 2400
_STOP = {"login", "signin", "sign-in", "oauth", "authorize", "download"}
_IGNORED = {"script", "style", "noscript", "svg", "template", "iframe", "object"}


def _web_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("网页地址格式不正确")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("网页地址格式不正确") from None
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    validate_endpoint(base)
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("只读取公网地址，不能访问本机或内网")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (not address.is_global or address.is_multicast):
        raise ValueError("只读取公网地址，不能访问本机或内网")
    if (parsed.fragment or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)
            or any(part.lower() in _STOP for part in unquote(unquote(parsed.path)).split("/"))):
        raise ValueError("只读取公开网页，不进入登录或下载页面")
    if redact_secrets(value) != value:
        raise ValueError("网页地址不能包含凭据")
    return value


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.anchors, self.title = [], [], []
        self._skip, self._title, self._anchor = 0, False, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _IGNORED:
            self._skip += 1
        if self._skip:
            return
        if tag == "title":
            self._title = True
        elif tag == "a" and len(self.anchors) < 200:
            self._anchor = {"url": attrs.get("href", ""), "class": attrs.get("class", ""), "parts": []}
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "section", "article"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in _IGNORED and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag == "title":
            self._title = False
        elif tag == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(" ".join(self._anchor.pop("parts")).split())
            self.anchors.append(self._anchor)
            self._anchor = None
        elif tag in {"p", "div", "li", "tr", "section", "article"}:
            self.text.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._title:
            self.title.append(data)
        else:
            self.text.append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)

    def body(self):
        lines = [" ".join(line.split()) for line in "".join(self.text).splitlines()]
        return "\n".join(line for line in lines if line)


class PublicWeb:
    def __init__(self, deps, *, settings, clean, stream=None, resolver=public_endpoint):
        self.deps, self.settings, self.clean = deps, settings, clean
        self.stream, self.resolver = stream, resolver
        self._slots, self._cache = asyncio.Semaphore(2), OrderedDict()

    def _enabled(self):
        config = self.settings()
        if not isinstance(config, dict) or config.get("enabled") is not True or config.get("web_enabled") is not True:
            raise PermissionError("公开网页查询已关闭")

    def _clean(self, text, limit):
        return self.clean(redact_secrets(str(text)), limit=limit)

    def _validate(self, url):
        url = _web_url(url)
        decoded = url
        for _ in range(3):
            if self._clean(decoded, 3000) != decoded:
                raise ValueError("网页地址不能包含敏感信息")
            decoded = unquote(decoded)
        return url

    async def _fetch(self, url):
        self._enabled()
        url = self._validate(url)
        http = getattr(getattr(getattr(self.deps, "runtime", None), "services", None), "http", None)
        if self.stream is None and (http is None or not callable(getattr(http, "stream", None))):
            raise RuntimeError("系统 HTTP 服务不可用")
        transport = MCPHttpTransport(http)
        stream = self.stream or transport._pinned_stream
        async with asyncio.timeout(20), self._slots:
            for hop in range(4):
                self._enabled()
                parsed = urlsplit(url)
                base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                pinned, host, sni = await self.resolver(base)
                self._enabled()  # Revocation while DNS or the queue was pending.
                target = urlsplit(pinned)
                pinned = urlunsplit((target.scheme, target.netloc, target.path, parsed.query, ""))
                async with stream("GET", pinned, hostname=sni, timeout=15, follow_redirects=False,
                        headers={"Host": host, "User-Agent": "AWBotNest/1 PublicReader",
                                 "Accept": "text/html, text/plain, application/json", "Accept-Encoding": "identity"}) as response:
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location or len(location) > 2000 or hop == 3:
                            raise ValueError("网页重定向过多或目标不正确")
                        url = self._validate(urljoin(url, location))
                        continue
                    if status != 200:
                        raise ValueError("网页无法公开读取，未自动登录或重试")
                    kind = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                    if kind not in {"text/html", "text/plain", "application/json", "application/xhtml+xml"}:
                        raise ValueError("只读取公开文字网页，不下载图片、文件或程序")
                    if "attachment" in response.headers.get("content-disposition", "").lower():
                        raise ValueError("目标是文件下载，已停止读取")
                    if response.headers.get("content-encoding", "identity").strip().lower() not in {"identity", ""}:
                        raise ValueError("网页未提供可安全读取的未压缩响应")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        if len(body) + len(chunk) > MAX_BYTES:
                            raise ValueError("网页内容过大，已停止读取")
                        body.extend(chunk)
                    self._enabled()
                    content_type = response.headers.get("content-type", "")
                    charset = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_-]{1,30})", content_type, re.I)
                    encoding = charset.group(1) if charset else "utf-8"
                    try:
                        text = bytes(body).decode(encoding, errors="replace")
                    except LookupError:
                        text = bytes(body).decode("utf-8", errors="replace")
                    return url, kind, text
        raise ValueError("网页无法读取")

    def _link(self, base, value):
        if not isinstance(value, str) or not value or value.startswith("#"):
            return None
        try:
            parsed = urlsplit(urljoin(base, value))
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
            return self._validate(url)
        except ValueError:
            return None

    async def read(self, url, offset=0):
        self._enabled()
        url = self._validate(url)
        if type(offset) is not int or not 0 <= offset <= MAX_TEXT:
            raise ValueError("网页读取位置不正确")
        cached = self._cache.get(url)
        if not cached or cached["expires"] <= time.time():
            try:
                final, kind, raw = await self._fetch(url)
            except (ValueError, PermissionError):
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RuntimeError("网页读取未完成，请稍后重试") from None
            title, links = "", []
            if kind in {"text/html", "application/xhtml+xml"}:
                page = _Page()
                page.feed(raw)
                text = page.body()
                title = self._clean(" ".join(" ".join(page.title).split()), 200)
                seen = set()
                for anchor in page.anchors:
                    target = self._link(final, anchor["url"])
                    if target and target not in seen and len(links) < 12:
                        seen.add(target)
                        links.append({"title": self._clean(anchor["text"], 120), "url": target})
            else:
                text = raw
            if not text.strip():
                raise ValueError("网页没有可读取的公开文字，可能需要登录或运行脚本")
            text = self._clean(text, MAX_TEXT + 1)
            cached = {"url": final, "title": title, "text": text[:MAX_TEXT], "links": links,
                      "truncated": len(text) > MAX_TEXT, "expires": time.time() + 300}
            self._cache[url] = cached
            self._cache.move_to_end(url)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
        if offset > len(cached["text"]):
            raise ValueError("网页读取位置超出可用正文")
        end = min(len(cached["text"]), offset + PAGE_CHARS)
        return {"url": cached["url"], "title": cached["title"], "content": cached["text"][offset:end],
                "links": cached["links"], "offset": offset, "next_offset": end if end < len(cached["text"]) else None,
                "complete": end == len(cached["text"]), "total": len(cached["text"]), "truncated": cached["truncated"],
                "snapshot": hashlib.sha256(cached["text"].encode()).hexdigest()[:20], "untrusted": True}

    async def search(self, query, limit=5):
        self._enabled()
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 300 or type(limit) is not int
                or not 1 <= limit <= 5 or self._clean(query, 1000) != query):
            raise ValueError("搜索内容不能为空、不能包含敏感信息，且不超过 300 字；最多返回 5 项")
        endpoint = "https://html.duckduckgo.com/html/?" + urlencode({"q": query.strip()})
        try:
            final, kind, text = await self._fetch(endpoint)
        except (ValueError, PermissionError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("公开搜索暂不可用，请稍后重试") from None
        if kind not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("公开搜索没有返回可用结果")
        page = _Page()
        page.feed(text)
        results, snippets, seen = [], {}, set()
        for anchor in page.anchors:
            target = self._link(final, anchor["url"])
            if not target:
                continue
            parsed = urlsplit(target)
            if parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"}:
                values = parse_qs(parsed.query).get("uddg") or []
                target = self._link(final, values[0]) if len(values) == 1 else None
            if not target:
                continue
            classes = anchor["class"].split()
            if "result__snippet" in classes:
                snippets[target] = self._clean(anchor["text"], 300)
            if "result__a" in classes and target not in seen and len(results) < limit:
                seen.add(target)
                results.append({"title": self._clean(anchor["text"], 180), "url": target})
        if not results:
            raise ValueError("公开搜索没有返回可用结果，可能受到访问限制；未编造搜索结果")
        for result in results:
            if result["url"] in snippets:
                result["snippet"] = snippets[result["url"]]
        return {"query": self._clean(query, 300), "results": results, "untrusted": True,
                "source": "DuckDuckGo", "count": len(results)}
