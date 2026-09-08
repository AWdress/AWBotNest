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


class HttpService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        timeout = kwargs.pop("timeout", 30)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     proxy=self.settings.proxy_url or None) as client:
            return await client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def download(self, url: str, destination: str | Path, *,
                       max_bytes: int = 100 * 1024 * 1024) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        total = 0
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True,
                                         proxy=self.settings.proxy_url or None) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("下载内容超过插件允许的大小")
                            output.write(chunk)
            temporary.replace(target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
