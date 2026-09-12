from __future__ import annotations

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from awbotnest.cloak_updates import check_cloakbrowser_update


def _settings(**overrides):
    values = {
        "browser_engine": "cloakbrowser",
        "cloakbrowser_use_free_key": True,
        "cloakbrowser_license_key": "cb_test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CloakUpdateTests(IsolatedAsyncioTestCase):
    async def test_scheduled_check_never_connects_when_key_mode_is_inactive(self):
        cases = [
            _settings(browser_engine="chromium"),
            _settings(cloakbrowser_use_free_key=False),
            _settings(cloakbrowser_license_key=""),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                http = SimpleNamespace(get=AsyncMock())

                result = await check_cloakbrowser_update(settings, http)

                self.assertEqual(result["status"], "disabled")
                http.get.assert_not_awaited()

    async def test_scheduled_check_selects_latest_compatible_release(self):
        component_response = Mock()
        component_response.json.return_value = {
            "releases": {
                "0.5.10": [{}],
                "0.5.12": [{}],
                "0.5.13": [{"yanked": True}],
                "0.5.14": [],
                "0.6.0": [{}],
            },
        }
        stable_response = Mock()
        stable_response.json.return_value = {
            "version": "152.0.7977.82.1",
            "resolved_channel": "stable",
        }
        preview_response = Mock()
        preview_response.json.return_value = {
            "version": "153.0.8000.1.2",
            "resolved_channel": "preview",
        }
        http = SimpleNamespace(get=AsyncMock(side_effect=[
            component_response, stable_response, preview_response,
        ]))

        with patch("awbotnest.cloak_updates.DependencyManager.target_version", return_value="0.5.10"), \
             patch("awbotnest.cloak_updates._platform_tag", return_value="linux-x64"), \
             patch("awbotnest.cloak_updates.kernel_binary_installed",
                   side_effect=lambda version: version == "152.0.7977.82.1"):
            result = await check_cloakbrowser_update(
                _settings(), http, ("stable", "preview"),
            )

        component_response.raise_for_status.assert_called_once_with()
        stable_response.raise_for_status.assert_called_once_with()
        preview_response.raise_for_status.assert_called_once_with()
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(result["latest_version"], "0.5.12")
        self.assertTrue(result["component_update_available"])
        self.assertTrue(result["kernel_update_available"])
        self.assertEqual(
            [(item["channel"], item["latest_version"], item["update_available"])
             for item in result["kernel_channels"]],
            [
                ("stable", "152.0.7977.82.1", False),
                ("preview", "153.0.8000.1.2", True),
            ],
        )
        self.assertIs(result["update_available"], True)
        self.assertEqual(http.get.await_count, 3)

    async def test_check_requests_only_plugin_required_kernel_channels(self):
        component_response = Mock()
        component_response.json.return_value = {"releases": {"0.5.10": [{}]}}
        preview_response = Mock()
        preview_response.json.return_value = {
            "version": "152.0.7977.82.1", "resolved_channel": "preview",
        }
        http = SimpleNamespace(get=AsyncMock(side_effect=[
            component_response, preview_response,
        ]))

        with patch("awbotnest.cloak_updates.DependencyManager.target_version",
                   return_value="0.5.10"), \
             patch("awbotnest.cloak_updates._platform_tag", return_value="linux-x64"), \
             patch("awbotnest.cloak_updates.kernel_binary_installed", return_value=False):
            result = await check_cloakbrowser_update(
                _settings(), http, ("preview", "unsupported", "preview"),
            )

        self.assertEqual(http.get.await_count, 2)
        self.assertEqual(result["required_kernel_channels"], ["preview"])
        self.assertEqual(
            [item["channel"] for item in result["kernel_channels"]], ["preview"],
        )
