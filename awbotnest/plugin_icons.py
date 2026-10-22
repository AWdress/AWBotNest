"""Lazy, bounded local caching for icons from known plugin metadata."""
from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from uuid import uuid4

from .assistant_mcp import MCPHttpTransport, public_endpoint, validate_endpoint
from .config import DATA_DIR
from .logs import redact_secrets
from .services.http import HttpService

MAX_ICON_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_CACHE_FILES = 1024
ICON_TTL = 7 * 86400
_KINDS = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
          ".gif": "image/gif", ".ico": "image/x-icon", ".svg": "image/svg+xml"}
_SVG_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
             "defs", "clipPath", "mask", "linearGradient", "radialGradient", "stop", "title", "desc"}
_SVG_ATTRS = {"id", "viewBox", "width", "height", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy",
              "r", "rx", "ry", "d", "points", "fill", "fill-rule", "fill-opacity", "stroke", "stroke-width",
              "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset",
              "stroke-opacity", "opacity", "transform", "clip-path", "clip-rule", "mask", "offset",
              "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod",
              "preserveAspectRatio", "color", "vector-effect", "xmlns"}


def _icon_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("插件图标地址不正确")
    parsed = urlsplit(value)
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    validate_endpoint(base)
    if (parsed.fragment or "\\" in value or any(ord(char) < 33 or ord(char) > 126 for char in value)
            or redact_secrets(value) != value
            or any(re.search(r"token|key|secret|auth|credential|signature|password|session|cookie", name, re.I)
                   for name, _ in parse_qsl(parsed.query))):
        raise ValueError("插件图标地址不能包含凭据")
    return value


def _image_bytes(body: bytes, content_type: str) -> tuple[bytes, str]:
    if not body or len(body) > MAX_ICON_BYTES or not content_type.startswith("image/"):
        raise ValueError("插件图标不是可用图片")
    signatures = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
                  (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"\x00\x00\x01\x00", ".ico"))
    for signature, suffix in signatures:
        if body.startswith(signature):
            return body, suffix
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return body, ".webp"
    if content_type != "image/svg+xml":
        raise ValueError("插件图标格式不受支持")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("SVG 图标须使用 UTF-8") from None
    if "\0" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("SVG 图标包含不支持的声明")
    root = ET.fromstring(text)
    if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        raise ValueError("插件图标不是 SVG")
    elements = list(root.iter())
    if len(elements) > 10000:
        raise ValueError("插件图标过于复杂")
    for element in elements:
        for child in list(element):
            if child.tag.rsplit("}", 1)[-1] not in _SVG_TAGS:
                element.remove(child)
        for name, value in list(element.attrib.items()):
            local = name.rsplit("}", 1)[-1]
            if (name != local or local not in _SVG_ATTRS or "\\" in value or "javascript:" in value.lower()
                    or "url(" in value.lower() and not re.fullmatch(r"url\(#[A-Za-z0-9_.:-]+\)", value)):
                del element.attrib[name]
    content = ET.tostring(root, encoding="utf-8")
    if len(content) > MAX_ICON_BYTES:
        raise ValueError("插件图标过大")
    return content, ".svg"


class PluginIconCache:
    def __init__(self, settings, *, root: Path | None = None, stream=None, resolver=public_endpoint):
        self.root = root or DATA_DIR / "plugin_icons"
        self._sources: OrderedDict[str, str] = OrderedDict()
        self._pending: dict[str, asyncio.Task] = {}
        self._failed: OrderedDict[str, float] = OrderedDict()
        self._slots = asyncio.Semaphore(4)
        self._stream = stream or MCPHttpTransport(HttpService(settings))._pinned_stream
        self._resolver = resolver
        self._disk_lock = threading.Lock()
        self._accessed: dict[str, float] = {}

    def present(self, plugin: dict) -> dict:
        item = dict(plugin)
        source = str(item.get("icon") or "")
        if "\\" in source or any(ord(char) < 33 or ord(char) > 126 for char in source):
            item["icon"] = ""
            return item
        if not source or source.startswith("/api/plugins/icons/"):
            return item
        # Existing same-origin resources are already local; never fetch them server-side.
        if source.startswith("/") and not source.startswith("//"):
            return item
        try:
            source = _icon_url(source)
        except (ValueError, UnicodeError):
            item["icon"] = ""
            return item
        key = hashlib.sha256((source + "\0" + str(item.get("version") or "")).encode()).hexdigest()
        self._sources[key] = source
        self._sources.move_to_end(key)
        while len(self._sources) > 8192:
            self._sources.popitem(last=False)
        item["icon"] = f"/api/plugins/icons/{key}"
        return item

    def _cached(self, key: str) -> tuple[Path, str] | None:
        candidates = []
        for suffix, kind in _KINDS.items():
            path = self.root / (key + suffix)
            try:
                info = path.stat()
                if path.is_file() and not path.is_symlink() and 0 < info.st_size <= MAX_ICON_BYTES:
                    candidates.append((info.st_mtime_ns, path, kind))
            except OSError:
                continue
        if candidates:
            _, path, kind = max(candidates)
            self._accessed[key] = time.monotonic()
            return path, kind
        return None

    async def get(self, key: str) -> tuple[Path, str]:
        if not re.fullmatch(r"[0-9a-f]{64}", key) or key not in self._sources:
            raise LookupError("插件图标不存在")
        cached = self._cached(key)
        try:
            fresh = cached and time.time() - cached[0].stat().st_mtime < ICON_TTL
        except OSError:
            cached, fresh = None, False
        if fresh:
            return cached
        if self._failed.get(key, 0) > time.monotonic():
            if cached:
                return cached
            raise LookupError("插件图标暂不可用")
        task = self._pending.get(key)
        if task is None:
            if len(self._pending) >= 256:
                if cached:
                    return cached
                raise LookupError("插件图标正在加载")
            task = asyncio.create_task(self._download(key, self._sources[key]))
            self._pending[key] = task
            task.add_done_callback(lambda done: self._finish(key, done))
        if cached:
            return cached
        try:
            return await asyncio.shield(task)
        except Exception:
            raise LookupError("插件图标暂不可用") from None

    def _finish(self, key: str, task: asyncio.Task) -> None:
        self._pending.pop(key, None)
        if task.cancelled():
            return
        if task.exception() is not None:
            self._failed[key] = time.monotonic() + 300
            self._failed.move_to_end(key)
            while len(self._failed) > 2048:
                self._failed.popitem(last=False)

    async def _download(self, key: str, source: str) -> tuple[Path, str]:
        async with asyncio.timeout(45), self._slots:
            async with asyncio.timeout(15):
                for hop in range(4):
                    source = _icon_url(source)
                    parsed = urlsplit(source)
                    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                    pinned, host, sni = await self._resolver(base)
                    target = urlsplit(pinned)
                    pinned = urlunsplit((target.scheme, target.netloc, target.path, parsed.query, ""))
                    async with self._stream("GET", pinned, hostname=sni, timeout=10, follow_redirects=False,
                            headers={"Host": host, "Accept": "image/png,image/jpeg,image/webp,image/svg+xml,image/gif,image/x-icon",
                                     "Accept-Encoding": "identity", "User-Agent": "AWBotNest PluginIcons"}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location", "")
                            if not location or len(location) > 2000 or hop == 3:
                                raise ValueError("插件图标重定向不正确")
                            source = _icon_url(urljoin(source, location))
                            continue
                        if response.status_code != 200:
                            raise ValueError("插件图标下载失败")
                        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                            raise ValueError("插件图标响应格式不受支持")
                        length = response.headers.get("content-length", "")
                        if length and (not length.isdecimal() or int(length) > MAX_ICON_BYTES):
                            raise ValueError("插件图标过大")
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            if len(body) + len(chunk) > MAX_ICON_BYTES:
                                raise ValueError("插件图标过大")
                            body.extend(chunk)
                        kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        content, suffix = await asyncio.to_thread(_image_bytes, bytes(body), kind)
                        path = await asyncio.to_thread(self._write, key, content, suffix)
                        return path, _KINDS[suffix]
        raise ValueError("插件图标下载失败")

    def _write(self, key: str, content: bytes, suffix: str) -> Path:
        with self._disk_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.root / f".{key}.{uuid4().hex}.tmp"
            path = self.root / (key + suffix)
            try:
                temporary.write_bytes(content)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            # Cleanup is best-effort: an inaccessible old file must not hide a good icon.
            self._prune(path)
            return path

    def _prune(self, current: Path) -> None:
        now, accessed_now = time.time(), time.monotonic()
        entries = []
        try:
            for old in self.root.iterdir():
                try:
                    if (old.suffix in _KINDS and re.fullmatch(r"[0-9a-f]{64}", old.stem)
                            and old.is_file() and not old.is_symlink()):
                        info = old.stat()
                        entries.append((info.st_mtime, info.st_size, old))
                    elif old.name.startswith(".") and old.suffix == ".tmp" and old.stat().st_mtime < now - 3600:
                        old.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            return
        total, count = sum(size for _, size, _ in entries), len(entries)
        for modified, size, old in sorted(entries):
            over_budget = total > MAX_CACHE_BYTES or count > MAX_CACHE_FILES
            expired = modified < now - 30 * 86400 and old.stem not in self._sources
            if not (over_budget or expired):
                continue
            if (old == current or old.stem in self._pending
                    or self._accessed.get(old.stem, 0) > accessed_now - 60):
                continue
            try:
                old.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            count -= 1
            self._accessed.pop(old.stem, None)
        # An all-active working set may briefly exceed the budget by in-flight icons.
        # The next write removes cold entries; active responses are never evicted.
        if len(self._accessed) > 8192:
            for key, stamp in list(self._accessed.items()):
                if stamp <= accessed_now - 60 and self._accessed.get(key) == stamp:
                    self._accessed.pop(key, None)


def present_plugin_icon(deps, plugin: dict) -> dict:
    cache = getattr(deps, "plugin_icons", None)
    return cache.present(plugin) if cache is not None else plugin


def set_icon_resource_cookie(deps, response) -> None:
    if getattr(deps, "plugin_icons", None) is not None and response is not None:
        response.set_cookie("awbotnest_resource", deps.settings.admin_token, httponly=True,
                            samesite="lax", path="/api/plugins")
