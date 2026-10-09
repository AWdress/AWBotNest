"""CloakBrowser update checks and conservative post-update cache cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version
import psutil

from .config import APP_ROOT, DATA_DIR, Settings
from .deps import DependencyManager
from .services.http import HttpService


CLOAKBROWSER_REQUIREMENT = SpecifierSet(">=0.5.10,<0.6")
CLOAKBROWSER_PYPI_URL = "https://pypi.org/pypi/cloakbrowser/json"
CLOAKBROWSER_KERNEL_URL = "https://cloakbrowser.dev/api/download/version"
KERNEL_CHANNELS = ("stable", "preview")
logger = logging.getLogger("awbotnest.cloak_updates")
_KERNEL_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){3,4}")
_PRO_KERNEL_DIR_RE = re.compile(r"chromium-([0-9]+(?:\.[0-9]+){3,4})-pro")

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
        raise RuntimeError(f"当前系统不支持 CloakBrowser 内核：{key[0]} {key[1]}") from exc


def kernel_binary_path(version: str, *, pro: bool = True) -> Path:
    suffix = "-pro" if pro else ""
    root = DATA_DIR / "cloakbrowser" / f"chromium-{version}{suffix}"
    if platform.system() == "Darwin":
        return root / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
    return root / ("chrome.exe" if platform.system() == "Windows" else "chrome")


def kernel_binary_installed(version: str, *, pro: bool = True) -> bool:
    if not version:
        return False
    binary = kernel_binary_path(version, pro=pro)
    return binary.is_file() and os.access(binary, os.X_OK)


def kernel_channel_active(channel: str, version: str) -> bool:
    """A cached binary is active only when its channel marker selects it."""
    if channel not in KERNEL_CHANNELS or not _KERNEL_VERSION_RE.fullmatch(version):
        return False
    try:
        prefix = "latest_pro_version_preview" if channel == "preview" else "latest_pro_version"
        marker = DATA_DIR / "cloakbrowser" / f"{prefix}_{_platform_tag()}"
        if not marker.is_file() or marker.stat().st_size > 128:
            return False
        return marker.read_text(encoding="utf-8").strip() == version and kernel_binary_installed(version)
    except (OSError, RuntimeError, UnicodeError):
        return False


def current_kernel_versions(*, key_active: bool) -> list[dict[str, str]]:
    """Return browser kernels that CloakBrowser can currently launch.

    Pro launches are channel-specific, so their marker files are the source of
    truth. Free launches use the SDK's marker/default selection, not the highest
    cached version; an unused cached build must not be reported as active.
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

    try:
        from cloakbrowser.config import (
            get_chromium_version, get_effective_version, get_local_binary_override,
            normalize_requested_version,
        )
        if get_local_binary_override():
            return []  # An arbitrary external binary has no verifiable version.
        pin = normalize_requested_version()
        version = pin or get_effective_version(pro=False)
        if (not pin and not kernel_binary_installed(str(version or ""), pro=False)):
            version = get_chromium_version()
        if (isinstance(version, str) and _KERNEL_VERSION_RE.fullmatch(version)
                and kernel_binary_installed(version, pro=False)):
            return [{"channel": "legacy_free", "version": version}]
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    return []


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


def _cache_path_is_link(path: Path) -> bool:
    """Reject links and Windows directory junctions, including dangling ones."""
    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _running_executable_paths() -> set[Path] | None:
    """Read executable paths only; an inaccessible process makes cleanup unsafe."""
    paths: set[Path] = set()
    try:
        for process in psutil.process_iter():
            try:
                executable = process.exe()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.AccessDenied, OSError):
                return None
            if executable:
                paths.add(Path(executable.removesuffix(" (deleted)")).resolve())
    except (psutil.Error, OSError, ValueError):
        return None
    return paths


def _kernel_tree_size(path: Path) -> int | None:
    """Inspect a plain cache tree without following links or special files."""
    size = 0

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        root = path.resolve(strict=True)
        for directory, directories, files in os.walk(path, followlinks=False, onerror=raise_walk_error):
            folder = Path(directory)
            if _cache_path_is_link(folder) or not folder.resolve(strict=True).is_relative_to(root):
                return None
            for name in directories:
                child = folder / name
                if _cache_path_is_link(child) or not child.resolve(strict=True).is_relative_to(root):
                    return None
            for name in files:
                child = folder / name
                if _cache_path_is_link(child):
                    return None
                details = child.stat(follow_symlinks=False)
                if not stat.S_ISREG(details.st_mode) or details.st_nlink > 1:
                    return None
                # Sparse files occupy fewer blocks than their logical length.
                blocks = getattr(details, "st_blocks", None)
                size += blocks * 512 if blocks is not None else details.st_size
    except (OSError, ValueError):
        return None
    return size


def cleanup_cloakbrowser_kernels(protected_versions: Iterable[str]) -> dict[str, int]:
    """Remove only unreferenced Pro kernels after a verified explicit update.

    Callers must hold the browser maintenance gate and pass the successfully
    updated versions. Legacy no-key binaries, metadata, GeoIP data, pins, both
    channels and running processes remain untouched. Any uncertainty skips
    cleanup rather than making an otherwise successful update fail.
    """
    result = {"removed": 0, "freed_bytes": 0, "skipped": 0}
    try:
        protected = set(protected_versions)
        if not protected or any(
            not isinstance(version, str) or not _KERNEL_VERSION_RE.fullmatch(version)
            for version in protected
        ):
            return result

        from cloakbrowser.config import (
            get_binary_path, get_cache_dir, get_local_binary_override,
            normalize_requested_version,
        )

        cache = Path(get_cache_dir()).absolute()
        if any(_cache_path_is_link(path) for path in (cache, *cache.parents)):
            return result
        cache = cache.resolve(strict=True)
        broad_roots = {Path(cache.anchor), Path.home().resolve(), APP_ROOT.resolve(), DATA_DIR.resolve(),
                       Path.cwd().resolve()}
        if not cache.is_dir() or cache in broad_roots:
            return result

        candidates = [path for path in cache.iterdir() if _PRO_KERNEL_DIR_RE.fullmatch(path.name)]
        candidates.sort(key=lambda path: path.name)
        result["skipped"] = len(candidates)
        # A new marker alone is not enough: the caller's verified binaries must
        # still be complete executable files in their expected Pro directories.
        for version in protected:
            binary = Path(get_binary_path(version, pro=True))
            if (not binary.is_file() or not os.access(binary, os.X_OK)
                    or not binary.resolve(strict=True).is_relative_to(cache / f"chromium-{version}-pro")
                    or any(_cache_path_is_link(path) for path in (binary, *binary.parents))):
                return result

        marker_names = {
            f"{prefix}_{tag}"
            for prefix in ("latest_pro_version", "latest_pro_version_preview")
            for tag in set(_PLATFORM_TAGS.values())
        }
        for marker in cache.iterdir():
            if not marker.name.startswith("latest_pro_version_"):
                continue
            if (marker.name not in marker_names or _cache_path_is_link(marker)
                    or not marker.is_file() or marker.stat().st_size > 128):
                return result
            version = marker.read_text(encoding="utf-8").strip()
            if not _KERNEL_VERSION_RE.fullmatch(version):
                return result
            protected.add(version)

        pin = normalize_requested_version()
        if pin:
            if not _KERNEL_VERSION_RE.fullmatch(pin):
                return result
            protected.add(pin)
        override = get_local_binary_override()
        override_path = Path(override).resolve(strict=True) if override else None
        running = _running_executable_paths()
        if running is None:
            return result

        for candidate in candidates:
            version = _PRO_KERNEL_DIR_RE.fullmatch(candidate.name).group(1)
            if version in protected or _cache_path_is_link(candidate) or not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.parent != cache or resolved != candidate:
                continue
            if override_path and override_path.is_relative_to(resolved):
                continue
            if any(path.is_relative_to(resolved) for path in running):
                continue
            size = _kernel_tree_size(candidate)
            if size is None:
                continue
            identity = candidate.stat(follow_symlinks=False)
            # Recheck occupancy immediately before removal. Only executable
            # paths are read; command lines may contain credentials.
            running = _running_executable_paths()
            if running is None:
                break
            if any(path.is_relative_to(resolved) for path in running):
                continue
            current = candidate.stat(follow_symlinks=False)
            if (_cache_path_is_link(candidate) or candidate.resolve(strict=True) != resolved
                    or (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino)):
                continue
            try:
                shutil.rmtree(candidate)
            except OSError:
                logger.debug("CloakBrowser 旧内核清理已跳过", exc_info=True)
                continue
            result["removed"] += 1
            result["freed_bytes"] += size
            result["skipped"] -= 1
    except Exception:
        logger.debug("CloakBrowser 旧内核清理已跳过", exc_info=True)
    if result["removed"]:
        logger.info("CloakBrowser 已清理 %d 个旧内核，释放 %.1f MB",
                    result["removed"], result["freed_bytes"] / (1024 * 1024))
    return result


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
        raise ValueError("没有找到系统兼容的 CloakBrowser 组件版本")
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
        installed = kernel_channel_active(channel, latest)
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
            installed = kernel_channel_active(str(channel.get("channel") or ""), latest)
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
        previous_state = dict(_state)
        _state.update({"status": "checking", "current_version": current, "error": ""})

        try:
            component_result, *kernel_results = await asyncio.gather(
                _check_component(http, current),
                *(_check_kernel(http, channel) for channel in required_channels),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            # The lock excludes a newer check; restore its predecessor so the
            # UI does not remain permanently stuck on a cancelled request.
            _state.clear()
            _state.update(previous_state)
            raise
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


def mark_cloakbrowser_updated(settings: Settings, version: str, *,
                             kernel_versions: dict[str, str] | None = None) -> None:
    """Refresh the snapshot after an explicit component and kernel update."""
    if not _enabled(settings):
        return
    channels = []
    for source in _state.get("kernel_channels", []):
        item = dict(source)
        latest = str(item.get("latest_version") or "")
        channel = str(item.get("channel") or "")
        activated = str((kernel_versions or {}).get(channel) or "")
        if activated and kernel_channel_active(channel, activated):
            try:
                if not latest or Version(activated) >= Version(latest):
                    latest = activated
                    item["latest_version"] = latest
            except InvalidVersion:
                pass
        if latest:
            installed = kernel_channel_active(channel, latest)
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
