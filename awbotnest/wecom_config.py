"""Shared validation for enterprise application callbacks."""
from __future__ import annotations

import base64
import re


CALLBACK_SECRETS = ("callback_token", "callback_aes_key")


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


def validate_callback_config(config: dict) -> None:
    if "callback_enabled" in config and not isinstance(config["callback_enabled"], bool):
        raise ValueError("企业微信消息回调开关格式不正确")
    for key in (*CALLBACK_SECRETS, "callback_users"):
        if key in config and not isinstance(config[key], str):
            raise ValueError("企业微信消息回调配置格式不正确")
    if not config.get("callback_enabled", False):
        return
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
    if (not users or len(users) > 64 or len(config["callback_users"]) > 8192
            or any(user.lower() == "@all" for user in users)
            or any(not re.fullmatch(r"[A-Za-z0-9_@.-]{1,64}", user) for user in users)):
        raise ValueError("请填写允许操作的成员 UserID，多个成员用 | 分隔（最多 64 个）")
    from .wecom_crypto import WeComCrypto
    WeComCrypto(token, key, config["corpid"])
