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


class BrowserService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._serial = asyncio.Lock()

    @staticmethod
    def _proxy(value):
        if not value:
            return None
        if isinstance(value, dict):
            return value
        from urllib.parse import unquote
        parsed = urlsplit(str(value))
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        result = {"server": f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    def _run_sync(self, url, action, *, headless, timeout, cookies, user_agent, proxy):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            options = {"headless": headless}
            if proxy:
                options["proxy"] = proxy
            browser = playwright.chromium.launch(**options)
            try:
                context = browser.new_context(**({"user_agent": user_agent} if user_agent else {}))
                try:
                    if isinstance(cookies, str):
                        context.set_extra_http_headers({"Cookie": cookies})
                    elif cookies:
                        context.add_cookies(cookies)
                    page = context.new_page()
                    page.set_default_timeout(timeout * 1000)
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                    return action(page)
                finally:
                    context.close()
            finally:
                browser.close()

    async def run(self, url: str, action: Callable[[Any], Any], *, headless: bool = True,
                  timeout: int = 60, cookies: list[dict[str, object]] | None = None,
                  user_agent: str = "", ua: str | None = None, proxy=None) -> Any:
        user_agent = user_agent or ua or ""
        resolved_proxy = self._proxy(self.settings.proxy_url if proxy is None else proxy)
        if not (inspect.iscoroutinefunction(action) or inspect.iscoroutinefunction(getattr(action, "__call__", None))):
            # V1 的同步 action 必须收到同步 Page，否则 click 等调用不会执行。
            async with self._serial:
                task = asyncio.create_task(asyncio.to_thread(self._run_sync, url, action,
                    headless=headless, timeout=timeout, cookies=cookies, user_agent=user_agent,
                    proxy=resolved_proxy))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    # 等待工作线程释放浏览器，避免停用返回后仍在执行浏览器操作。
                    await asyncio.gather(task, return_exceptions=True)
                    raise
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("浏览器能力未安装，请安装 playwright") from exc
        async with async_playwright() as playwright:
            launch_args: dict[str, object] = {"headless": headless}
            if resolved_proxy:
                launch_args["proxy"] = resolved_proxy
            browser = await playwright.chromium.launch(**launch_args)
            context_args = {"user_agent": user_agent} if user_agent else {}
            context = None
            try:
                context = await browser.new_context(**context_args)
                if isinstance(cookies, str):
                    await context.set_extra_http_headers({"Cookie": cookies})
                elif cookies:
                    await context.add_cookies(cookies)
                page = await context.new_page()
                page.set_default_timeout(timeout * 1000)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                value = action(page)
                return await asyncio.wait_for(value, timeout=timeout) if isinstance(value, Awaitable) else value
            finally:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    await browser.close()

    async def page_source(self, url: str, **kwargs: Any) -> str:
        def source(page):
            try:
                page.wait_for_load_state("networkidle", timeout=min(int(kwargs.get("timeout", 60)), 15) * 1000)
            except Exception:
                pass  # V1 treats network-idle timeout as best effort, not a failed page.
            return page.content()
        return await self.run(url, source, **kwargs)
