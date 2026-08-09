"""全局插件热度客户端的数据校验与中心地址规范化。"""
from __future__ import annotations

import re
import uuid
from typing import Any

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_EVENT_TYPES = {"install", "update"}


class HeatEventError(ValueError):
    """本地待上报事件格式无效。"""


def normalize_server_url(value: Any) -> str:
    """只接受不含用户凭据的 HTTP(S) 中心服务根地址。"""
    from urllib.parse import urlsplit, urlunsplit

    source = str(value or "").strip()
    if not source:
        return ""
    if len(source) > 512:
        raise ValueError("全局热度服务器地址过长")
    try:
        parsed = urlsplit(source)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("全局热度服务器地址无效") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("全局热度服务器地址必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("全局热度服务器地址不能包含凭据、查询参数或片段")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _canonical_uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value or "")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise HeatEventError(f"{field} 必须是 UUID") from exc


def validate_event(raw: Any) -> dict[str, str]:
    """清洗持久化的待上报事件，避免发送畸形本地状态。"""
    if not isinstance(raw, dict):
        raise HeatEventError("事件必须是对象")
    plugin_id = str(raw.get("plugin_id") or "").strip()
    if not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise HeatEventError("plugin_id 格式无效")
    event_type = str(raw.get("event_type") or "").strip().lower()
    if event_type not in _EVENT_TYPES:
        raise HeatEventError("event_type 只允许 install 或 update")
    version = str(raw.get("version") or "").strip()
    if len(version) > 64:
        raise HeatEventError("version 不能超过 64 个字符")
    app_version = str(raw.get("app_version") or "").strip()
    if len(app_version) > 64:
        raise HeatEventError("app_version 不能超过 64 个字符")
    return {
        "event_id": _canonical_uuid(raw.get("event_id"), "event_id"),
        "installation_id": _canonical_uuid(raw.get("installation_id"), "installation_id"),
        "plugin_id": plugin_id,
        "event_type": event_type,
        "version": version,
        "app_version": app_version,
    }
