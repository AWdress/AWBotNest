"""Shared validation for enterprise application callbacks."""
from __future__ import annotations

import base64
import re


CALLBACK_SECRETS = ("callback_token", "callback_aes_key")
CALLBACK_CHANNEL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CALLBACK_MEMBER_ID = re.compile(r"[A-Za-z0-9_@.-]{1,64}")


def channel_config(settings, channel_id: str) -> dict | None:
    raw = next((item for item in settings.notification_channels
                if str(item.get("id") or "") == channel_id), None)
    if raw is None:
        return None
    nested = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    return {**nested, **raw}


def callback_members(config: dict) -> set[str]:
    value = config.get("callback_users", "")
    return {item.strip() for item in value.split("|") if item.strip()} if isinstance(value, str) else set()


def callback_member_allowed(config: dict, user_id: str) -> bool:
    """WeCom UserIDs are case-insensitive; retain the original ID for replies."""
    return (isinstance(user_id, str) and bool(CALLBACK_MEMBER_ID.fullmatch(user_id))
            and user_id.lower() in {member.lower() for member in callback_members(config)})


def validate_callback_channels(channels: list[dict]) -> None:
    """An enabled callback must be addressable by an unambiguous top-level ID."""
    for source in channels:
        nested = source.get("config") if isinstance(source.get("config"), dict) else {}
        effective = {**nested, **source}
        if effective.get("callback_enabled") is not True:
            continue
        channel_id = source.get("id")
        if not isinstance(channel_id, str) or not CALLBACK_CHANNEL_ID.fullmatch(channel_id):
            raise ValueError("启用消息回调的通知渠道 ID 只能包含英文字母、数字、横线和下划线，最多 128 位")
        if sum(item.get("id") == channel_id for item in channels) != 1:
            raise ValueError("启用消息回调的通知渠道 ID 必须唯一")


def validate_callback_config(config: dict) -> None:
    if "callback_enabled" in config and not isinstance(config["callback_enabled"], bool):
        raise ValueError("企业微信消息回调开关格式不正确")
    for key in (*CALLBACK_SECRETS, "callback_users"):
        if key in config and not isinstance(config[key], str):
            raise ValueError("企业微信消息回调配置格式不正确")
    if not config.get("callback_enabled", False):
        return
    if not isinstance(config.get("id"), str) or not CALLBACK_CHANNEL_ID.fullmatch(config["id"]):
        raise ValueError("启用消息回调的通知渠道 ID 只能包含英文字母、数字、横线和下划线，最多 128 位")
    if "enabled" in config and not isinstance(config["enabled"], bool):
        raise ValueError("企业微信通知渠道开关格式不正确")
    if config.get("type") not in {"wecom", "wechat"} or config.get("url") or config.get("webhook"):
        raise ValueError("消息回调仅支持企业微信自建应用")
    if not isinstance(config.get("corpid"), str) or not config["corpid"].strip():
        raise ValueError("请填写企业 ID")
    if not isinstance(config.get("secret"), str) or not config["secret"].strip() or config["secret"] == "********":
        raise ValueError("请填写企业微信应用 Secret")
    agentid = config.get("agentid", "")
    if (isinstance(agentid, bool) or not isinstance(agentid, (str, int))
            or not re.fullmatch(r"[0-9]{1,10}", str(agentid)) or not 0 < int(agentid) < 2**31):
        raise ValueError("请填写有效的应用 AgentID")
    token = config.get("callback_token", "")
    if not token or len(token) > 128 or any(char.isspace() for char in token) or token == "********":
        raise ValueError("请填写有效的回调 Token")
    key = config.get("callback_aes_key", "")
    if not re.fullmatch(r"[A-Za-z0-9+/]{43}", key):
        raise ValueError("EncodingAESKey 必须是 43 位 Base64 字符")
    try:
        decoded = base64.b64decode(key + "=", validate=True)
    except ValueError as exc:
        raise ValueError("EncodingAESKey 格式不正确") from exc
    if len(decoded) != 32:
        raise ValueError("EncodingAESKey 格式不正确")
    users = callback_members(config)
    if (not users or len({user.lower() for user in users}) > 64 or len(config["callback_users"]) > 8192
            or any(user.lower() == "@all" for user in users)
            or any(not CALLBACK_MEMBER_ID.fullmatch(user) for user in users)):
        raise ValueError("请填写允许操作的成员 UserID，多个成员用 | 分隔（最多 64 个）")
    from .wecom_crypto import WeComCrypto
    WeComCrypto(token, key, config["corpid"])
