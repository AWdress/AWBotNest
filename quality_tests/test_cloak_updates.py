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
        response = Mock()
        response.json.return_value = {
            "releases": {
                "0.5.10": [{}],
                "0.5.12": [{}],
                "0.5.13": [{"yanked": True}],
                "0.5.14": [],
                "0.6.0": [{}],
            },
        }
        http = SimpleNamespace(get=AsyncMock(return_value=response))

        with patch("awbotnest.cloak_updates.DependencyManager.target_version", return_value="0.5.10"):
            result = await check_cloakbrowser_update(_settings(), http)

        response.raise_for_status.assert_called_once_with()
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(result["latest_version"], "0.5.12")
        self.assertIs(result["update_available"], True)
        http.get.assert_awaited_once()
