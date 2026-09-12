from __future__ import annotations

import unittest
from types import SimpleNamespace

import httpx

from awbotnest.api import create_app
from awbotnest.config import Settings
from awbotnest.plugins import PluginRuntime
from awbotnest.routing import PluginRoutes
from awbotnest.scheduler import PluginScheduler


class PluginSecretTests(unittest.IsolatedAsyncioTestCase):
    async def test_secret_is_masked_and_revealed_only_by_declared_field(self):
        plugin = SimpleNamespace(
            id="secret-plugin",
            name="敏感配置测试",
            render_mode="schema",
            config_schema={
                "token": {"type": "password"},
                "endpoint": {"type": "string"},
            },
        )
        runtime = SimpleNamespace(
            scan=lambda: [plugin],
            secret_field=PluginRuntime.secret_field,
            has_frontend=lambda plugin_id: False,
        )
        settings = Settings(
            admin_token="plugin-secret-token",
            plugin_config={
                "secret-plugin": {
                    "token": "real-secret",
                    "endpoint": "https://example.test",
                },
            },
        )
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        app = create_app(
            settings,
            SimpleNamespace(),
            runtime,
            scheduler,
            PluginRoutes(),
            market=SimpleNamespace(clear_cache=lambda: None),
        )
        headers = {"Authorization": "Bearer plugin-secret-token"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test",
        ) as client:
            masked = await client.get("/api/plugins/secret-plugin/config", headers=headers)
            self.assertEqual(masked.status_code, 200, masked.text)
            self.assertEqual(masked.json()["values"]["token"], "********")

            revealed = await client.post(
                "/api/plugins/secret-plugin/config/reveal",
                headers=headers,
                json={"field": "token"},
            )
            self.assertEqual(revealed.status_code, 200, revealed.text)
            self.assertEqual(revealed.json()["value"], "real-secret")
            self.assertEqual(revealed.headers["cache-control"], "no-store")

            ordinary = await client.post(
                "/api/plugins/secret-plugin/config/reveal",
                headers=headers,
                json={"field": "endpoint"},
            )
            self.assertEqual(ordinary.status_code, 400)

    async def test_system_cloakbrowser_key_is_masked_and_revealed_without_cache(self):
        settings = Settings(
            admin_token="plugin-secret-token",
            cloakbrowser_license_key="cb_real_platform_secret",
        )
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        app = create_app(
            settings,
            SimpleNamespace(),
            SimpleNamespace(),
            scheduler,
            PluginRoutes(),
            market=SimpleNamespace(clear_cache=lambda: None),
        )
        headers = {"Authorization": "Bearer plugin-secret-token"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test",
        ) as client:
            masked = await client.get("/api/settings", headers=headers)
            self.assertEqual(
                masked.json()["settings"]["CLOAKBROWSER_LICENSE_KEY"], "********",
            )
            revealed = await client.post(
                "/api/settings/reveal-secret",
                headers=headers,
                json={"kind": "system", "field": "CLOAKBROWSER_LICENSE_KEY"},
            )
            self.assertEqual(revealed.json()["value"], "cb_real_platform_secret")
            self.assertEqual(revealed.headers["cache-control"], "no-store")
            self.assertEqual(revealed.headers["pragma"], "no-cache")
