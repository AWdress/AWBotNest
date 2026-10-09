"""Read-only V2 release checks and deduplicated default-Bot notifications."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from packaging.version import InvalidVersion, Version

from . import __version__
from .config import DATA_DIR, Settings
from .rich_delivery import DeliveryUncertain
from .rich_text import sanitize_rich_html, text_to_rich_html
from .services.http import HttpService


RELEASES_URL = "https://api.github.com/repos/AWdress/AWBotNest/releases"
RELEASE_PAGE_URL = "https://github.com/AWdress/AWBotNest/releases/tag/"
CACHE_SECONDS = 3600.0
FAILURE_RETRY_SECONDS = 60.0
MAX_RELEASE_PAGES = 5
MAX_RELEASES = 30
MAX_NOTES_LENGTH = 12_000
MAX_MESSAGE_LENGTH = 3000
_VERSION_PATTERN = re.compile(r"[vV]?(2\.[0-9]{1,9}\.[0-9]{1,9}(?:\.[0-9]{1,9})?)")
_logger = logging.getLogger("awbotnest.system_updates")


def _version(value: object) -> Version | None:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        return None
    try:
        return Version(value.lstrip("vV"))
    except InvalidVersion:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _release(item: object) -> dict[str, str] | None:
    if (not isinstance(item, dict) or item.get("draft") is not False
            or type(item.get("prerelease")) is not bool):
        return None
    # This repository publishes numeric V2 tags with GitHub's prerelease flag.
    # The numeric tag, not that publishing flag, distinguishes a usable release.
    version = _version(item.get("tag_name"))
    if version is None:
        return None
    tag = item["tag_name"]
    # The repository/tag, not an upstream HTML URL, determines the details link.
    return {
        "version": str(version),
        "name": str(item.get("name") or f"v{version}")[:200],
        "notes": str(item.get("body") or "")[:MAX_NOTES_LENGTH],
        "url": RELEASE_PAGE_URL + quote(tag, safe=""),
        "published_at": str(item.get("published_at") or "")[:64],
    }


def _render_notes(notes: str) -> str:
    notes = notes.replace("平台主人", "管理员").replace("平台管理员", "管理员").replace("平台", "系统")
    labels = {
        "added": "新增", "features": "新增", "new features": "新增",
        "fixed": "修复", "fixes": "修复", "bug fixes": "修复",
        "changed": "调整", "changes": "调整", "improvements": "优化",
        "security": "安全", "removed": "移除",
    }
    parts: list[str] = []
    has_heading = False
    for raw in notes.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"[-*_]{3,}", line):
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            label = heading.group(1).strip().strip("*_")
            if (_version(label) is not None or label.lower() in {"changelog", "release notes"}
                    or label in {"更新日志", "版本日志"}):
                continue
            label = labels.get(label.lower(), label)
            if parts:
                parts.append("")
            parts.append("<b>" + text_to_rich_html(label) + "</b>")
            has_heading = True
            continue
        # Release Markdown remains data; never pass its HTML through unsanitized.
        line = re.sub(r"^[-*+]\s+", "• ", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        parts.append(text_to_rich_html(line))
    if not parts:
        return "更新说明见完整更新链接。"
    if not has_heading:
        parts.insert(0, "<b>更新内容</b>")
    return "<br>".join(parts)


def _update_message(current_version: str, release: dict[str, str]) -> str:
    published = release["published_at"]
    try:
        published = datetime.fromisoformat(published.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        published = "未提供"
    intro = (
        f"<b>版本</b> {text_to_rich_html(current_version)} → {text_to_rich_html(release['version'])}"
        f"<br><b>发布日期</b> {text_to_rich_html(published)}<br><br>"
    )
    footer = "<br><br><b>完整更新</b><br>" + text_to_rich_html(release["url"])
    notes = release["notes"]
    limit = min(len(notes), 2400)
    while True:
        shortened = limit < len(notes)
        tail = "<br>… 更多内容见完整更新链接。" if shortened else ""
        message = sanitize_rich_html(intro + _render_notes(notes[:limit]) + tail + footer)
        units = len(message.encode("utf-16-le", errors="replace")) // 2
        if units <= MAX_MESSAGE_LENGTH or limit == 0:
            return message
        limit = max(0, limit - max(1, (units - MAX_MESSAGE_LENGTH + 5) // 4))


class SystemUpdateService:
    def __init__(self, settings: Settings, http: HttpService, notifier: Any,
                 current_version: str = __version__, state_path: str | Path | None = None) -> None:
        try:
            parsed = Version(str(current_version).lstrip("vV"))
            if parsed.major != 2:
                raise InvalidVersion("Not a V2 build")
        except InvalidVersion:
            raise ValueError("当前系统版本格式不正确") from None
        self.settings = settings
        self.http = http
        self.notifier = notifier
        self.current_version = str(parsed)
        self.state_path = Path(state_path) if state_path is not None else DATA_DIR / "system_update_state.json"
        self._cache: dict[str, Any] | None = None
        self._cache_until = 0.0
        self._retry_after = 0.0
        self._check_error = ""
        self._checked_at = ""
        self._fetch_task: asyncio.Task | None = None
        self._notification_lock = asyncio.Lock()
        self.notification_task: asyncio.Task | None = None
        self._notification_status = "not_needed"
        self._notification_error = ""
        self._last_warning = ""
        self._state_error = ""
        self._records: dict[str, dict[str, str]] = {}
        self._closed = False
        self._load_state()

    def _load_state(self) -> None:
        try:
            if self.state_path.stat().st_size > 128 * 1024:
                raise ValueError("更新通知记录过大")
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            records = data.get("notifications") if isinstance(data, dict) else None
            if not isinstance(records, dict):
                raise ValueError("更新通知记录格式不正确")
            for key, record in records.items():
                if (_version(key) is None or not isinstance(record, dict)
                        or record.get("status") not in {"sent", "inflight", "uncertain"}):
                    raise ValueError("更新通知记录格式不正确")
                self._records[str(_version(key))] = {
                    "status": record["status"], "at": str(record.get("at") or "")[:64],
                }
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError):
            # Do not overwrite a damaged record and accidentally resend a prior notice.
            self._state_error = "更新通知记录无法读取，未发送通知"

    def _persist(self, records: dict[str, dict[str, str]]) -> None:
        keys = sorted(records, key=lambda key: Version(key), reverse=True)[:256]
        bounded = {key: records[key] for key in keys}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "notifications": bounded}, ensure_ascii=False),
                             encoding="utf-8")
        temporary.replace(self.state_path)
        self._records = bounded

    def _mark(self, version: str, status: str | None) -> bool:
        records = copy.deepcopy(self._records)
        if status is None:
            records.pop(version, None)
        else:
            records[version] = {"status": status, "at": _now()}
        try:
            self._persist(records)
            return True
        except OSError:
            return False

    async def _refresh(self) -> None:
        try:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "AWBotNest-System-Update",
                       "X-GitHub-Api-Version": "2022-11-28"}
            if self.settings.github_token.strip():
                headers["Authorization"] = "Bearer " + self.settings.github_token.strip()
            versions: dict[str, dict[str, str]] = {}
            for page in range(1, MAX_RELEASE_PAGES + 1):
                response = await self.http.get(RELEASES_URL, headers=headers,
                                               params={"per_page": 100, "page": page}, timeout=30)
                response.raise_for_status()
                values = response.json()
                if not isinstance(values, list) or len(values) > 100:
                    raise ValueError("GitHub 发布数据格式不正确")
                for item in values:
                    release = _release(item)
                    if release is not None:
                        previous = versions.get(release["version"])
                        if previous is None or release["published_at"] > previous["published_at"]:
                            versions[release["version"]] = release
                if len(values) < 100:
                    break
            releases = sorted(versions.values(), key=lambda release: Version(release["version"]), reverse=True)
            releases = releases[:MAX_RELEASES]
            latest = releases[0]["version"] if releases else ""
            self._cache = {
                "current_version": self.current_version, "latest_version": latest,
                "update_available": bool(latest and Version(latest) > Version(self.current_version)),
                "releases": releases,
            }
            self._cache_until = time.monotonic() + CACHE_SECONDS
            self._retry_after = 0.0
            self._check_error = ""
            self._last_warning = ""
        except asyncio.CancelledError:
            raise
        except Exception as error:
            previous_error = self._check_error
            if isinstance(error, httpx.HTTPStatusError):
                if error.response.status_code in {403, 429}:
                    self._check_error = "GitHub 请求受限，请稍后重试"
                elif error.response.status_code == 404:
                    self._check_error = "未找到系统发布信息"
                else:
                    self._check_error = "GitHub 发布接口暂时不可用"
            elif isinstance(error, httpx.HTTPError):
                self._check_error = "检查系统更新失败，网络连接异常"
            elif isinstance(error, ValueError):
                self._check_error = "GitHub 发布数据格式不正确"
            else:
                self._check_error = "检查系统更新失败，请稍后重试"
            self._retry_after = time.monotonic() + FAILURE_RETRY_SECONDS
            if self._check_error != previous_error:
                _logger.warning(self._check_error)
        finally:
            self._checked_at = _now()

    def _notification_result(self, status: str, error: str = "") -> dict[str, str]:
        self._notification_status = status
        self._notification_error = error
        return {"notification_status": status, "notification_error": error}

    def _warning(self, message: str) -> None:
        if message != self._last_warning:
            _logger.warning(message)
            self._last_warning = message

    async def check_scheduled(self) -> dict[str, Any]:
        # An hourly tick can precede cache expiry because fetching takes time.
        # Bypass that cache, but keep the failure backoff for background checks.
        return await self.check(force=time.monotonic() >= self._retry_after)

    async def check(self, force: bool = False, include_history: bool = False,
                    notify: bool = True) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("系统更新检查已停止")
        now = time.monotonic()
        active = self._fetch_task is not None and not self._fetch_task.done()
        refresh = force or now >= max(self._cache_until, self._retry_after)
        if active or refresh:
            if not active:
                self._fetch_task = asyncio.create_task(self._refresh())
            await asyncio.shield(self._fetch_task)
        if notify:
            await self.notify_pending()
        result = copy.deepcopy(self._cache or {
            "current_version": self.current_version, "latest_version": "", "update_available": False, "releases": [],
        })
        if not include_history:
            result["releases"] = result["releases"][:1]
        result.update({
            "ok": not bool(self._check_error), "status": "error" if self._check_error else "ok", "error": self._check_error,
            "checked_at": self._checked_at, "last_checked_at": self._checked_at,
            "stale": bool(self._check_error and self._cache is not None),
            "notification_status": (self._notification_status if self.settings.system_update_notify_enabled is True
                                    else "disabled"),
            "notification_error": (self._notification_error if self.settings.system_update_notify_enabled is True else ""),
        })
        return result

    async def notify_pending(self) -> dict[str, str]:
        task = self.start_notification()
        if task is None:
            return self._notification_result("not_needed")
        return await asyncio.shield(task)

    async def _notify_pending(self) -> dict[str, str]:
        async with self._notification_lock:
            if self.settings.system_update_notify_enabled is not True:
                return self._notification_result("disabled")
            if self._closed or not self._cache or not self._cache["update_available"]:
                return self._notification_result("not_needed")
            if self._state_error:
                self._warning(self._state_error)
                return self._notification_result("state_error", self._state_error)
            release = self._cache["releases"][0]
            version = release["version"]
            record = self._records.get(version)
            if record is not None:
                if record["status"] == "sent":
                    return self._notification_result("already_notified")
                return self._notification_result("uncertain", "更新通知结果待确认，未重复发送")
            if any(Version(key) >= Version(version) and value["status"] == "sent"
                   for key, value in self._records.items()):
                return self._notification_result("already_notified")
            if any(Version(key) >= Version(version) for key in self._records):
                return self._notification_result("uncertain", "更新通知结果待确认，未重复发送")
            if not self._mark(version, "inflight"):
                self._warning("更新通知记录无法保存，未发送通知")
                return self._notification_result("state_error", "更新通知记录无法保存，未发送通知")
            try:
                result = await self.notifier.send_to_default_bot(
                    _update_message(self.current_version, release), category="系统更新", format="rich",
                )
            except asyncio.CancelledError:
                self._mark(version, "uncertain")
                self._notification_result("uncertain", "更新通知结果待确认，未重复发送")
                raise
            except DeliveryUncertain:
                self._mark(version, "uncertain")
                _logger.warning("系统更新通知结果待确认：v%s，未重复发送", version)
                return self._notification_result("uncertain", "更新通知结果待确认，未重复发送")
            except (RuntimeError, ValueError):
                if not self._mark(version, None):
                    self._warning("系统更新通知记录未完成，未重复发送")
                    return self._notification_result("uncertain", "更新通知记录未完成，未重复发送")
                _logger.warning("系统更新通知发送失败：v%s，稍后会重试", version)
                return self._notification_result("failed", "更新通知发送失败，稍后会重试")
            except Exception:
                self._mark(version, "uncertain")
                _logger.warning("系统更新通知结果待确认：v%s，未重复发送", version)
                return self._notification_result("uncertain", "更新通知结果待确认，未重复发送")
            if result is False:
                if not self._mark(version, None):
                    self._warning("系统更新通知记录未完成，未重复发送")
                    return self._notification_result("uncertain", "更新通知记录未完成，未重复发送")
                if self.settings.system_update_notify_enabled is not True:
                    return self._notification_result("disabled")
                return self._notification_result("unconfigured", "未配置默认 Bot 或接收目标")
            if not self._mark(version, "sent"):
                self._warning("系统更新通知已发送，记录未完成；未重复发送")
                return self._notification_result("uncertain", "更新通知已发送，记录未完成；未重复发送")
            _logger.info("系统更新通知已发送：v%s", version)
            return self._notification_result("sent")

    def start_notification(self) -> asyncio.Task | None:
        if self._closed:
            return None
        if self.notification_task is None or self.notification_task.done():
            self.notification_task = asyncio.create_task(self._notify_pending())
        return self.notification_task

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in (self._fetch_task, self.notification_task) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def get_system_update_service(settings: Settings, runtime: Any) -> SystemUpdateService:
    """Lazily share one checker between the scheduler and HTTP endpoints."""
    service = getattr(runtime, "system_updates", None)
    if service is None:
        service = SystemUpdateService(settings, runtime.services.http, runtime.notifier)
        runtime.system_updates = service
    return service
