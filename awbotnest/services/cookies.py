from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..config import DATA_DIR, Settings


class CookieService:
    def __init__(self) -> None:
        self.path = DATA_DIR / "cookies.json"
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def get(self, domain: str, *, path: str = "/", names=None) -> list[dict[str, Any]]:
        requested = (urlsplit(domain if "://" in domain else "//" + domain).hostname or "").lower().lstrip(".")
        path = "/" + path.lstrip("/")
        wanted = None if names is None else {names} if isinstance(names, str) else set(names)
        result, seen = [], set()
        async with self._lock:
            data = self._read()
        for source, cookies in data.items():
            # 旧 V2 键值数据无路径信息，只能按根路径读取；重新同步可恢复完整属性。
            if isinstance(cookies, dict):
                cookies = [{"name": k, "value": v, "domain": source, "path": "/", "hostOnly": True}
                           for k, v in cookies.items()]
            if not isinstance(cookies, list):
                continue
            for raw in cookies:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                host = str(item.get("domain") or source).lower().strip().lstrip(".")
                if not host or (requested != host and (item.get("hostOnly") or not requested.endswith("." + host))):
                    continue
                cookie_path = str(item.get("path") or "/")
                prefix = cookie_path.rstrip("/") or "/"
                if not path.startswith(prefix) or (prefix != "/" and len(path) > len(prefix) and path[len(prefix)] != "/"):
                    continue
                expires = item.get("expirationDate", item.get("expires"))
                try:
                    if expires is not None and 0 < float(expires) <= time.time():
                        continue
                except (TypeError, ValueError):
                    pass
                name = str(item.get("name") or "")
                identity = (name, host, cookie_path)
                if not name or identity in seen or (wanted is not None and name not in wanted):
                    continue
                seen.add(identity)
                item.setdefault("domain", source)
                item["path"] = cookie_path
                item["name"] = name
                item["value"] = str(item.get("value") or "")
                result.append(item)
        result.sort(key=lambda item: len(item["path"]), reverse=True)
        return result

    async def set(self, domain: str, values: dict[str, str]) -> None:
        async with self._lock:
            data = self._read()
            data[domain.lower().strip()] = {str(key): str(value) for key, value in values.items()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    async def replace(self, values: dict[str, Any]) -> None:
        async with self._lock:
            clean = {
                str(domain).lower().strip(): ([dict(item) for item in cookies if isinstance(item, dict)]
                    if isinstance(cookies, list) else {str(key): str(value) for key, value in cookies.items()})
                for domain, cookies in values.items() if str(domain).strip() and isinstance(cookies, (dict, list))
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    async def domains(self) -> list[dict[str, object]]:
        async with self._lock:
            return [{"domain": key, "count": len(value)} for key, value in sorted(self._read().items())]

    async def delete(self, domain: str) -> bool:
        async with self._lock:
            data = self._read()
            removed = data.pop(domain.lower().strip(), None) is not None
            if removed:
                temp = self.path.with_suffix(".tmp")
                temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(self.path)
            return removed

    async def header(self, domain: str, *, path: str = "/", names=None) -> str:
        cookies = await self.get(domain, path=path, names=names)
        values = {}
        for cookie in cookies:
            values.setdefault(cookie["name"], cookie["value"])
        return "; ".join(f"{key}={value}" for key, value in values.items())

    async def playwright(self, domain: str, *, path: str = "/") -> list[dict[str, object]]:
        cookies = await self.get(domain, path=path)
        result = []
        allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
        for cookie in cookies:
            item = {key: value for key, value in cookie.items() if key in allowed}
            if "expirationDate" in cookie and "expires" not in item:
                item["expires"] = cookie["expirationDate"]
            same_site = str(item.get("sameSite") or "").lower()
            if same_site in {"strict", "lax", "none"}:
                item["sameSite"] = same_site.title()
            else:
                item.pop("sameSite", None)
            result.append(item)
        return result
