from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from awbotnest.api import create_app
from awbotnest.config import Settings
from awbotnest.cookiecloud import remote_configured, service_configured
from awbotnest.routing import PluginRoutes
from awbotnest.scheduler import PluginScheduler


class CookieCloudConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scheduler = PluginScheduler()
        self.addCleanup(self.scheduler.stop)

    @staticmethod
    def runtime():
        return SimpleNamespace(services=SimpleNamespace(
            cookies=SimpleNamespace(domains=AsyncMock(return_value=[]), replace=AsyncMock()),
        ))

    def test_credentials_replace_legacy_enable_switches(self):
        browser = {"enabled": False, "uuid": "browser-id", "password": "browser-password"}
        remote = {
            "remote_enabled": False,
            "remote_url": "https://cookie.example.test/cookiecloud",
            "remote_uuid": "remote-id",
            "remote_password": "remote-password",
        }

        self.assertTrue(service_configured(browser))
        self.assertTrue(remote_configured(remote))
        self.assertFalse(service_configured({"uuid": "browser-id"}))
        self.assertFalse(remote_configured({"remote_url": "https://cookie.example.test"}))

    async def test_saving_removes_legacy_switches_and_starts_remote_sync(self):
        settings = Settings(admin_token="cookie-token", cookie_settings={
            "enabled": False,
            "remote_enabled": False,
            "uuid": "browser-id",
            "password": "browser-password",
        })
        app = create_app(
            settings, SimpleNamespace(), self.runtime(), self.scheduler, PluginRoutes(),
            market=SimpleNamespace(clear_cache=lambda: None),
        )
        headers = {"Authorization": "Bearer cookie-token"}
        payload = {"settings": {
            "remote_url": "https://cookie.example.test/cookiecloud",
            "remote_uuid": "remote-id",
            "remote_password": "remote-password",
            "remote_interval_minutes": 15,
        }}

        with patch("awbotnest.api.settings.save_settings"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test",
            ) as client:
                response = await client.put("/api/cookies/settings", headers=headers, json=payload)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("enabled", settings.cookie_settings)
        self.assertNotIn("remote_enabled", settings.cookie_settings)
        self.assertIsNotNone(self.scheduler.scheduler.get_job("__platform__::远程 CookieCloud 同步"))

    async def test_manual_remote_sync_does_not_need_a_switch(self):
        settings = Settings(admin_token="cookie-token", cookie_settings={
            "remote_enabled": False,
            "remote_url": "https://cookie.example.test/cookiecloud",
            "remote_uuid": "remote-id",
            "remote_password": "remote-password",
        })
        runtime = self.runtime()
        app = create_app(
            settings, SimpleNamespace(), runtime, self.scheduler, PluginRoutes(),
            market=SimpleNamespace(clear_cache=lambda: None),
        )
        headers = {"Authorization": "Bearer cookie-token"}

        with patch("awbotnest.cookiecloud.pull", AsyncMock(return_value={
            "example.test": [{
                "name": "session", "value": "value", "domain": "example.test",
            }],
        })):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test",
            ) as client:
                response = await client.post("/api/cookies/remote-sync", headers=headers)

        self.assertEqual(response.status_code, 200, response.text)
        runtime.services.cookies.replace.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
