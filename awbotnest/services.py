from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .config import DATA_DIR, Settings


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


class CookieService:
    def __init__(self) -> None:
        self.path = DATA_DIR / "cookies.json"
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def get(self, domain: str) -> dict[str, str]:
        async with self._lock:
            return dict(self._read().get(domain.lower().strip(), {}))

    async def set(self, domain: str, values: dict[str, str]) -> None:
        async with self._lock:
            data = self._read()
            data[domain.lower().strip()] = {str(key): str(value) for key, value in values.items()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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

    async def header(self, domain: str) -> str:
        values = await self.get(domain)
        return "; ".join(f"{key}={value}" for key, value in values.items())

    async def playwright(self, domain: str) -> list[dict[str, object]]:
        values = await self.get(domain)
        return [{"name": key, "value": value, "domain": domain, "path": "/"}
                for key, value in values.items()]


class BrowserService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, url: str, action: Callable[[Any], Any], *, headless: bool = True,
                  timeout: int = 60, cookies: list[dict[str, object]] | None = None,
                  user_agent: str = "") -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("浏览器能力未安装，请安装 playwright") from exc
        async with async_playwright() as playwright:
            launch_args: dict[str, object] = {"headless": headless}
            if self.settings.proxy_url:
                launch_args["proxy"] = {"server": self.settings.proxy_url}
            browser = await playwright.chromium.launch(**launch_args)
            context_args = {"user_agent": user_agent} if user_agent else {}
            context = await browser.new_context(**context_args)
            try:
                if cookies:
                    await context.add_cookies(cookies)
                page = await context.new_page()
                page.set_default_timeout(timeout * 1000)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                value = action(page)
                return await asyncio.wait_for(value, timeout=timeout) if isinstance(value, Awaitable) else value
            finally:
                await context.close()
                await browser.close()

    async def page_source(self, url: str, **kwargs: Any) -> str:
        return await self.run(url, lambda page: page.content(), **kwargs)


class AIService:
    def __init__(self, settings: Settings, http: HttpService) -> None:
        self.settings = settings
        self.http = http

    async def chat(self, messages: list[dict[str, str]], *, model: str = "",
                   temperature: float | None = None) -> str:
        if not self.settings.ai_api_key:
            raise RuntimeError("尚未配置 AI 服务")
        payload: dict[str, object] = {
            "model": model or self.settings.ai_model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        response = await self.http.post(
            f"{self.settings.ai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.ai_api_key}"},
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"])


class PlatformServices:
    def __init__(self, settings: Settings) -> None:
        self.http = HttpService(settings)
        self.cookies = CookieService()
        self.browser = BrowserService(settings)
        self.ai = AIService(settings, self.http)
