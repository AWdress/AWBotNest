"""Read-only checks for CloakBrowser component and browser-kernel updates."""

from __future__ import annotations

import asyncio
import platform
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .config import DATA_DIR, Settings
from .deps import DependencyManager
from .services.http import HttpService


CLOAKBROWSER_REQUIREMENT = SpecifierSet(">=0.5.10,<0.6")
CLOAKBROWSER_PYPI_URL = "https://pypi.org/pypi/cloakbrowser/json"
CLOAKBROWSER_KERNEL_URL = "https://cloakbrowser.dev/api/download/version"
KERNEL_CHANNELS = ("stable", "preview")

_PLATFORM_TAGS = {
    ("Linux", "x86_64"): "linux-x64",
    ("Linux", "aarch64"): "linux-arm64",
    ("Darwin", "arm64"): "darwin-arm64",
    ("Darwin", "x86_64"): "darwin-x64",
    ("Windows", "AMD64"): "windows-x64",
    ("Windows", "x86_64"): "windows-x64",
}

_lock = asyncio.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "current_version": "",
    "latest_version": "",
    "component_update_available": False,
    "kernel_update_available": False,
    "kernel_channels": [],
    "required_kernel_channels": [],
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


def _platform_tag() -> str:
    key = (platform.system(), platform.machine())
    try:
        return _PLATFORM_TAGS[key]
    except KeyError as exc:
        raise RuntimeError(f"当前平台不支持 CloakBrowser 内核：{key[0]} {key[1]}") from exc


def kernel_binary_path(version: str, *, pro: bool = True) -> Path:
    suffix = "-pro" if pro else ""
    root = DATA_DIR / "cloakbrowser" / f"chromium-{version}{suffix}"
    if platform.system() == "Darwin":
        return root / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
    return root / ("chrome.exe" if platform.system() == "Windows" else "chrome")


def kernel_binary_installed(version: str, *, pro: bool = True) -> bool:
    return bool(version and kernel_binary_path(version, pro=pro).is_file())


def current_kernel_versions(*, key_active: bool) -> list[dict[str, str]]:
    """Return browser kernels that CloakBrowser can currently launch.

    Pro launches are channel-specific, so their marker files are the source of
    truth.  Legacy free launches have no reliable active marker on a fresh
    install; in that mode the newest complete non-Pro binary is the effective
    local choice.
    """
    cache_dir = DATA_DIR / "cloakbrowser"
    if key_active:
        try:
            platform_tag = _platform_tag()
        except RuntimeError:
            return []
        kernels: list[dict[str, str]] = []
        for channel in KERNEL_CHANNELS:
            prefix = "latest_pro_version_preview" if channel == "preview" else "latest_pro_version"
            marker = cache_dir / f"{prefix}_{platform_tag}"
            try:
                version = marker.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if version and kernel_binary_installed(version):
                kernels.append({"channel": channel, "version": version})
        return kernels

    versions: list[Version] = []
    for root in cache_dir.glob("chromium-*"):
        if not root.is_dir() or root.name.endswith("-pro"):
            continue
        raw_version = root.name.removeprefix("chromium-")
        try:
            version = Version(raw_version)
        except InvalidVersion:
            continue
        if kernel_binary_installed(raw_version, pro=False):
            versions.append(version)
    if not versions:
        return []
    return [{"channel": "legacy_free", "version": str(max(versions))}]


def required_kernel_channels() -> tuple[str, ...]:
    """Return channels proven to have been launched by an installed plugin."""
    cache_dir = DATA_DIR / "cloakbrowser"
    try:
        platform_tag = _platform_tag()
    except RuntimeError:
        return ()
    result: list[str] = []
    for channel in KERNEL_CHANNELS:
        suffix = f"preview_{platform_tag}" if channel == "preview" else platform_tag
        # CloakBrowser creates these channel-specific markers only while resolving
        # a real launch. Merely finding another Pro binary directory is not enough:
        # it may have been downloaded by a plugin that was later removed.
        used = any(
            (cache_dir / f"{prefix}{suffix}").is_file()
            for prefix in ("latest_pro_version_", ".last_pro_version_check_")
        )
        if used:
            result.append(channel)
    return tuple(result)


def update_cloakbrowser_kernel(license_key: str, channel: str) -> str | None:
    """Force the public CloakBrowser updater to refresh and install one channel.

    CloakBrowser caches its own version lookup for an hour.  The platform's
    explicit "check and update" action must not reuse that stale lookup after
    it has already discovered a newer kernel, so only the channel-specific
    lookup markers are invalidated before invoking the package updater.
    """
    if channel not in KERNEL_CHANNELS:
        raise ValueError(f"不支持的 CloakBrowser 内核通道：{channel}")
    from cloakbrowser.config import get_cache_dir, get_platform_tag
    from cloakbrowser.download import check_for_pro_update

    platform_tag = get_platform_tag()
    suffix = f"preview_{platform_tag}" if channel == "preview" else platform_tag
    cache_dir = get_cache_dir()
    for prefix in (".last_pro_version_check_", ".last_pro_version_resolution_"):
        (cache_dir / f"{prefix}{suffix}").unlink(missing_ok=True)
    return check_for_pro_update(license_key, channel)


def _latest_compatible(payload: dict[str, Any]) -> str:
    candidates: list[Version] = []
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        raise ValueError("组件更新服务返回的数据格式不正确")
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
        raise ValueError("没有找到平台兼容的 CloakBrowser 组件版本")
    return str(max(candidates))


async def _check_component(http: HttpService, current: str) -> dict[str, Any]:
    response = await http.get(
        CLOAKBROWSER_PYPI_URL,
        headers={"Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    latest = _latest_compatible(response.json())
    available = not current or Version(latest) > Version(current)
    return {"latest_version": latest, "update_available": available}


async def _check_kernel(http: HttpService, channel: str) -> dict[str, Any]:
    url = CLOAKBROWSER_KERNEL_URL + ("?channel=preview" if channel == "preview" else "")
    try:
        response = await http.get(
            url,
            headers={"Accept": "application/json", "X-Platform": _platform_tag()},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        latest = str(payload.get("version") or "").strip()
        if not latest:
            raise ValueError("内核更新服务没有返回版本号")
        installed = kernel_binary_installed(latest)
        return {
            "channel": channel,
            "resolved_channel": str(payload.get("resolved_channel") or channel),
            "latest_version": latest,
            "installed": installed,
            "update_available": not installed,
            "status": "current" if installed else "update_available",
            "error": "",
        }
    except Exception as exc:
        return {
            "channel": channel,
            "resolved_channel": channel,
            "latest_version": "",
            "installed": False,
            "update_available": False,
            "status": "error",
            "error": str(exc)[:200] or type(exc).__name__,
        }


def _disabled_status(settings: Settings) -> dict[str, Any]:
    return {
        "status": "disabled",
        "current_version": DependencyManager(settings).target_version("cloakbrowser"),
        "latest_version": "",
        "component_update_available": False,
        "kernel_update_available": False,
        "kernel_channels": [],
        "required_kernel_channels": [],
        "update_available": False,
        "last_checked_at": "",
        "error": "",
    }


def cloak_update_status(settings: Settings) -> dict[str, Any]:
    """Return a UI-safe snapshot, hiding stale results while checks are off."""
    if not _enabled(settings):
        return _disabled_status(settings)
    snapshot = dict(_state)
    current = DependencyManager(settings).target_version("cloakbrowser")
    snapshot["current_version"] = current
    try:
        component_available = bool(
            snapshot["latest_version"]
            and (not current or Version(snapshot["latest_version"]) > Version(current))
        )
    except InvalidVersion:
        component_available = False
    channels = []
    for item in snapshot.get("kernel_channels", []):
        channel = dict(item)
        latest = str(channel.get("latest_version") or "")
        if latest:
            installed = kernel_binary_installed(latest)
            channel.update({
                "installed": installed,
                "update_available": not installed,
                "status": "current" if installed else "update_available",
            })
        channels.append(channel)
    kernel_available = any(item.get("update_available") for item in channels)
    snapshot.update({
        "component_update_available": component_available,
        "kernel_update_available": kernel_available,
        "kernel_channels": channels,
        "update_available": component_available or kernel_available,
    })
    if snapshot.get("status") != "checking":
        snapshot["status"] = (
            "update_available" if snapshot["update_available"]
            else "error" if snapshot.get("error")
            else "current" if snapshot.get("last_checked_at")
            else "idle"
        )
    return snapshot


async def check_cloakbrowser_update(settings: Settings, http: HttpService,
                                    channels: Iterable[str] | None = None) -> dict[str, Any]:
    """Check component plus Stable/Preview kernels without downloading anything."""
    if not _enabled(settings):
        return _disabled_status(settings)

    async with _lock:
        if not _enabled(settings):
            return _disabled_status(settings)
        current = DependencyManager(settings).target_version("cloakbrowser")
        required_channels = tuple(dict.fromkeys(
            channel for channel in (channels if channels is not None else required_kernel_channels())
            if channel in KERNEL_CHANNELS
        ))
        _state.update({"status": "checking", "current_version": current, "error": ""})

        component_result, *kernel_results = await asyncio.gather(
            _check_component(http, current),
            *(_check_kernel(http, channel) for channel in required_channels),
            return_exceptions=True,
        )
        errors: list[str] = []
        if isinstance(component_result, BaseException):
            latest = ""
            component_available = False
            errors.append(f"组件：{str(component_result)[:160] or type(component_result).__name__}")
        else:
            latest = component_result["latest_version"]
            component_available = component_result["update_available"]

        channels: list[dict[str, Any]] = []
        for channel, result in zip(required_channels, kernel_results):
            if isinstance(result, BaseException):
                result = {
                    "channel": channel, "resolved_channel": channel,
                    "latest_version": "", "installed": False,
                    "update_available": False, "status": "error",
                    "error": str(result)[:200] or type(result).__name__,
                }
            channels.append(result)
            if result.get("error"):
                errors.append(f"{channel.title()} 内核：{result['error']}")

        kernel_available = any(item.get("update_available") for item in channels)
        update_available = component_available or kernel_available
        error = "；".join(errors)[:500]
        _state.update({
            "status": "update_available" if update_available else "error" if error else "current",
            "current_version": current,
            "latest_version": latest,
            "component_update_available": component_available,
            "kernel_update_available": kernel_available,
            "kernel_channels": channels,
            "required_kernel_channels": list(required_channels),
            "update_available": update_available,
            "last_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": error,
        })
        return dict(_state)


def mark_cloakbrowser_updated(settings: Settings, version: str) -> None:
    """Refresh the snapshot after an explicit component and kernel update."""
    if not _enabled(settings):
        return
    channels = []
    for source in _state.get("kernel_channels", []):
        item = dict(source)
        latest = str(item.get("latest_version") or "")
        if latest:
            installed = kernel_binary_installed(latest)
            item.update({
                "installed": installed,
                "update_available": not installed,
                "status": "current" if installed else "update_available",
                "error": "",
            })
        channels.append(item)
    kernel_available = any(item.get("update_available") for item in channels)
    latest = str(_state.get("latest_version") or "")
    try:
        component_available = bool(
            latest and (not version or Version(latest) > Version(version))
        )
    except InvalidVersion:
        component_available = False
    update_available = component_available or kernel_available
    _state.update({
        "status": "update_available" if update_available else "current",
        "current_version": version,
        "latest_version": latest or version,
        "component_update_available": component_available,
        "kernel_update_available": kernel_available,
        "kernel_channels": channels,
        "required_kernel_channels": list(_state.get("required_kernel_channels", [])),
        "update_available": update_available,
        "last_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": "",
    })
