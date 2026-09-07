"""Plugin-scoped, read-only Cookie access with V1 domain permissions."""
from __future__ import annotations

import time
from urllib.parse import urlsplit


class PluginCookies:
    def __init__(self, service, settings, domains, notifier):
        self._service = service
        self._settings = settings
        self._domains = tuple(str(item).lower().strip() for item in domains)
        self._notifier = notifier
        self._requested = {}

    @property
    def domains(self):
        return list(self._domains)

    @property
    def available(self):
        return bool(self._settings.cookie_settings.get("enabled") and
                    self._domains and self._service.path.exists())

    def _authorize(self, domain, *, require_enabled=True):
        value = str(domain).strip()
        requested = (urlsplit(value if "://" in value else "//" + value).hostname or "").lower().lstrip(".")
        if not requested:
            raise ValueError("Cookie 域名格式无效")
        if not any(requested == pattern or
                   (pattern.startswith("*.") and
                    (requested == pattern[2:] or requested.endswith("." + pattern[2:])))
                   for pattern in self._domains):
            raise PermissionError(f"插件未声明 Cookie 域名权限: {requested}")
        if require_enabled and not self._settings.cookie_settings.get("enabled"):
            raise RuntimeError("平台 Cookie 同步尚未启用")
        return requested

    async def get(self, domain, *, path="/", names=None):
        return await self._service.get(self._authorize(domain), path=path, names=names)

    async def header(self, domain, *, path="/", names=None):
        return await self._service.header(self._authorize(domain), path=path, names=names)

    async def playwright(self, domain, *, path="/"):
        return await self._service.playwright(self._authorize(domain), path=path)

    async def request_sync(self, domain):
        requested = self._authorize(domain, require_enabled=False)
        if self.available:
            # A valid non-root Cookie also means synchronization is already available.
            paths = {"/"}
            for entries in self._service._read().values():
                if isinstance(entries, list):
                    paths.update(str(item.get("path") or "/") for item in entries if isinstance(item, dict))
            for path in paths:
                if await self._service.get(requested, path=path):
                    return True
        now = time.monotonic()
        if requested in self._requested and now - self._requested[requested] < 1800:
            return False
        self._requested[requested] = now
        try:
            await self._notifier(f"请同步 {requested} 的 Cookie 后重试")
        except BaseException:
            self._requested.pop(requested, None)
            raise
        return False
