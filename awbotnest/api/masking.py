"""Presentation-safe views of sensitive settings used by API responses."""
from urllib.parse import urlparse, urlunparse


def masked_proxy(settings) -> str:
    if not settings.proxy_url:
        return ""
    value = urlparse(settings.proxy_url)
    if not value.password:
        return settings.proxy_url
    host = value.hostname or ""
    if value.port:
        host += f":{value.port}"
    auth = f"{value.username or ''}:********@"
    return urlunparse((value.scheme, auth + host, value.path, "", value.query, ""))


def masked_channels(settings) -> list[dict[str, object]]:
    result = []
    bot_tokens = {item.id: item.token for item in settings.bot_specs()}
    metadata = {"id", "name", "type", "enabled", "is_default", "plugins", "config"}
    for source in settings.notification_channels:
        item = dict(source)
        nested = dict(item.get("config") or {}) if isinstance(item.get("config"), dict) else {}
        config = {**{key: value for key, value in item.items() if key not in metadata}, **nested}
        channel_type = "wechat" if item.get("type") == "wecom" else str(item.get("type") or "telegram")
        channel_id = str(item.get("id") or "")
        if channel_type == "telegram" and bot_tokens.get(channel_id):
            config["token"] = "********"
        for key in ("url", "webhook", "server", "token", "password", "secret", "device_key"):
            if config.get(key):
                config[key] = "********"
        result.append({
            "id": channel_id, "name": str(item.get("name") or channel_id), "type": channel_type,
            "enabled": item.get("enabled", True), "is_default": bool(item.get("is_default")),
            "config": config,
        })
    return result
