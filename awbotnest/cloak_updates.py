"""CloakBrowser component update checks.

The scheduled check is intentionally read-only.  Installing a new Python
component while plugins are running would leave the process with a mixture of
old imported modules and new files on disk, so installation remains an
explicit administrator action.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .config import Settings
from .deps import DependencyManager
from .services.http import HttpService


CLOAKBROWSER_REQUIREMENT = SpecifierSet(">=0.5.10,<0.6")
CLOAKBROWSER_PYPI_URL = "https://pypi.org/pypi/cloakbrowser/json"

_lock = asyncio.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "current_version": "",
    "latest_version": "",
    "update_available": False,
    "last_checked_at": "",
    "error": "",
}


def _enabled(settings: Settings) -> bool:
    return bool(
        settings.browser_engine == "cloakbrowser"
        and settings.cloakbrowser_use_free_key
        and str(settings.cloakbrowser_license_key or "").strip()
    )


def _latest_compatible(payload: dict[str, Any]) -> str:
    candidates: list[Version] = []
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        raise ValueError("更新服务返回的数据格式不正确")
    for raw_version, files in releases.items():
        try:
            version = Version(str(raw_version))
        except InvalidVersion:
            continue
        if version.is_prerelease or version not in CLOAKBROWSER_REQUIREMENT:
            continue
        if not isinstance(files, list) or not files or all(
            isinstance(item, dict) and item.get("yanked") for item in files
        ):
            continue
        candidates.append(version)
    if not candidates:
        raise ValueError("没有找到平台兼容的 CloakBrowser 版本")
    return str(max(candidates))


def cloak_update_status(settings: Settings) -> dict[str, Any]:
    """Return a UI-safe snapshot, hiding stale results while checks are off."""
    current = DependencyManager(settings).target_version("cloakbrowser")
    if not _enabled(settings):
        return {
            "status": "disabled",
            "current_version": current,
            "latest_version": "",
            "update_available": False,
            "last_checked_at": "",
            "error": "",
        }
    snapshot = dict(_state)
    snapshot["current_version"] = current
    if snapshot["latest_version"]:
        try:
            snapshot["update_available"] = (
                not current or Version(snapshot["latest_version"]) > Version(current)
            )
            snapshot["status"] = "update_available" if snapshot["update_available"] else "current"
        except InvalidVersion:
            pass
    return snapshot


async def check_cloakbrowser_update(settings: Settings, http: HttpService) -> dict[str, Any]:
    """Check PyPI only when the saved CloakBrowser free-key mode is active."""
    if not _enabled(settings):
        return cloak_update_status(settings)

    async with _lock:
        # Settings are mutable and may have changed while this task waited.
        if not _enabled(settings):
            return cloak_update_status(settings)
        current = DependencyManager(settings).target_version("cloakbrowser")
        _state.update({
            "status": "checking",
            "current_version": current,
            "error": "",
        })
        try:
            response = await http.get(
                CLOAKBROWSER_PYPI_URL,
                headers={"Accept": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            latest = _latest_compatible(response.json())
            update_available = not current or Version(latest) > Version(current)
            _state.update({
                "status": "update_available" if update_available else "current",
                "current_version": current,
                "latest_version": latest,
                "update_available": update_available,
                "last_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "error": "",
            })
        except Exception as exc:
            _state.update({
                "status": "error",
                "current_version": current,
                "latest_version": "",
                "update_available": False,
                "last_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "error": str(exc)[:300] or type(exc).__name__,
            })
        return dict(_state)


def mark_cloakbrowser_updated(settings: Settings, version: str) -> None:
    """Refresh the in-memory result after an explicit component update."""
    if not _enabled(settings):
        return
    _state.update({
        "status": "current",
        "current_version": version,
        "latest_version": version,
        "update_available": False,
        "last_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": "",
    })
