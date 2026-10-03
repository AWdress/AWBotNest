import asyncio
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from awbotnest import cloak_proxy
from awbotnest.config import Settings
from awbotnest.services.browser import BrowserService


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_launch_failure_stops_driver(self):
        driver = SimpleNamespace(
            chromium=SimpleNamespace(launch=Mock(side_effect=RuntimeError("launch failed"))),
            stop=Mock(),
        )
        manager = SimpleNamespace(start=Mock(return_value=driver))
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                BrowserService(Settings())._run_sync(
                    "https://example.test", lambda page: None, headless=True,
                    timeout=1, cookies=None, user_agent="", proxy=None,
                )
        driver.stop.assert_called_once()

    async def test_async_launch_failure_stops_driver(self):
        driver = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(side_effect=RuntimeError("launch failed"))),
            stop=AsyncMock(),
        )
        manager = SimpleNamespace(start=AsyncMock(return_value=driver))
        async def action(page):
            return None
        with patch("playwright.async_api.async_playwright", return_value=manager):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                await BrowserService(Settings()).run("https://example.test", action)
        driver.stop.assert_awaited_once()

    async def test_cancelling_browser_launch_stops_driver(self):
        launching = asyncio.Event()
        async def launch(**kwargs):
            launching.set()
            await asyncio.Event().wait()
        driver = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=AsyncMock())
        manager = SimpleNamespace(start=AsyncMock(return_value=driver))
        async def action(page):
            return None
        with patch("playwright.async_api.async_playwright", return_value=manager):
            task = asyncio.create_task(BrowserService(Settings()).run("https://example.test", action))
            await asyncio.wait_for(launching.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        driver.stop.assert_awaited_once()

    async def test_context_creation_failure_closes_browser_and_driver(self):
        browser = SimpleNamespace(
            new_context=AsyncMock(side_effect=RuntimeError("context failed")), close=AsyncMock(),
        )
        driver = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock(),
        )
        manager = SimpleNamespace(start=AsyncMock(return_value=driver))
        async def action(page):
            return None
        with patch("playwright.async_api.async_playwright", return_value=manager):
            with self.assertRaisesRegex(RuntimeError, "context failed"):
                await BrowserService(Settings()).run("https://example.test", action)
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    async def test_explicit_direct_mode_reaches_cloak_sync_and_async_launch(self):
        calls = []
        sync_page = SimpleNamespace(set_default_timeout=Mock(), goto=Mock())
        sync_context = SimpleNamespace(new_page=Mock(return_value=sync_page), close=Mock())
        sync_browser = SimpleNamespace(new_context=Mock(return_value=sync_context), close=Mock())
        async_page = SimpleNamespace(set_default_timeout=Mock(), goto=AsyncMock())
        async_context = SimpleNamespace(new_page=AsyncMock(return_value=async_page), close=AsyncMock())
        async_browser = SimpleNamespace(new_context=AsyncMock(return_value=async_context), close=AsyncMock())

        def launch(**kwargs):
            calls.append(kwargs)
            return sync_browser
        async def launch_async(**kwargs):
            calls.append(kwargs)
            return async_browser
        async def action(page):
            return "async result"

        package = ModuleType("cloakbrowser")
        package.launch = cloak_proxy._wrap_launch(launch)
        package.launch_async = cloak_proxy._wrap_launch(launch_async)
        settings = Settings(browser_engine="cloakbrowser", proxy_url="http://system-proxy.test:8080")
        with patch.dict(sys.modules, {"cloakbrowser": package}), \
                patch.object(cloak_proxy, "configure_cloakbrowser"), \
                patch.object(cloak_proxy, "_settings", settings):
            service = BrowserService(settings)
            self.assertEqual(await service.run("https://example.test", lambda page: "sync result", proxy=False),
                             "sync result")
            self.assertEqual(await service.run("https://example.test", action, proxy=False), "async result")
        self.assertEqual([call["proxy"] for call in calls], [None, None])
        sync_browser.close.assert_called_once()
        async_browser.close.assert_awaited_once()
