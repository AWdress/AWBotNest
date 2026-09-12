from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from awbotnest.cloak_updates import check_cloakbrowser_update


def _settings(**overrides):
    values = {
        "browser_engine": "cloakbrowser",
        "cloakbrowser_use_free_key": True,
        "cloakbrowser_license_key": "cb_test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("settings", [
    _settings(browser_engine="chromium"),
    _settings(cloakbrowser_use_free_key=False),
    _settings(cloakbrowser_license_key=""),
])
def test_scheduled_update_check_never_connects_when_key_mode_is_inactive(settings):
    http = SimpleNamespace(get=AsyncMock())

    result = asyncio.run(check_cloakbrowser_update(settings, http))

    assert result["status"] == "disabled"
    http.get.assert_not_awaited()


def test_scheduled_update_check_selects_latest_compatible_release():
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
        result = asyncio.run(check_cloakbrowser_update(_settings(), http))

    response.raise_for_status.assert_called_once_with()
    assert result["status"] == "update_available"
    assert result["latest_version"] == "0.5.12"
    assert result["update_available"] is True
    http.get.assert_awaited_once()
