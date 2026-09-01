from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
SESSIONS_DIR = APP_ROOT / "sessions"
PLUGINS_DIR = APP_ROOT / "plugins"
CONFIG_FILE = DATA_DIR / "config.json"


@dataclass(slots=True)
class BotSettings:
    id: str
    name: str
    token: str


@dataclass(slots=True)
class Settings:
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    bot_name: str = "主要 Bot"
    bots: list[BotSettings] = field(default_factory=list)
    default_bot_id: str = "default"
    default_bot_chat_id: str = ""
    admin_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    admin_username: str = "admin"
    admin_salt: str = ""
    admin_password_hash: str = ""
    web_host: str = "0.0.0.0"
    web_port: int = 18001
    enabled_plugins: list[str] = field(default_factory=lambda: ["hello"])
    user_sessions: list[str] = field(default_factory=list)
    plugin_config: dict[str, dict[str, object]] = field(default_factory=dict)
    plugin_accounts: dict[str, list[str]] = field(default_factory=dict)
    bot_routing: dict[str, str] = field(default_factory=dict)
    plugin_order: list[str] = field(default_factory=list)
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4.1-mini"
    ai_settings: dict[str, object] = field(default_factory=dict)
    cookie_settings: dict[str, object] = field(default_factory=dict)
    plugin_repos: list[str] = field(default_factory=lambda: ["AWdress/AWBotNest-Plugins"])
    plugin_repo_interval: int = 20
    notification_channels: list[dict[str, object]] = field(default_factory=list)
    proxy_url: str = ""
    webhook_secret: str = ""
    api_key: str = ""
    pip_index_url: str = ""
    log_cleaner: dict[str, object] = field(default_factory=lambda: {
        "enabled": True, "keep_lines": 1000, "hour": 3, "minute": 0,
    })

    @property
    def telegram_configured(self) -> bool:
        return self.api_id > 0 and bool(self.api_hash.strip())

    def bot_specs(self) -> list[BotSettings]:
        result = [BotSettings("default", self.bot_name or "主要 Bot", self.bot_token)]
        result.extend(bot for bot in self.bots if bot.id != "default")
        return result


def _env(name: str, default: object) -> object:
    value = os.getenv(f"AWBOTNEST_{name.upper()}")
    return default if value is None else value


def load_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    raw: dict[str, object] = {}
    if CONFIG_FILE.exists():
        try:
            value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                raw = value
        except (OSError, json.JSONDecodeError):
            raw = {}
    raw_bots = raw.get("bots") or []
    bots = [
        BotSettings(
            id=str(item.get("id") or "").strip(),
            name=str(item.get("name") or item.get("id") or "Bot").strip(),
            token=str(item.get("token") or "").strip(),
        )
        for item in raw_bots if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    generated_admin_token = not bool(str(raw.get("admin_token") or "").strip())
    settings = Settings(
        api_id=int(_env("api_id", raw.get("api_id", 0)) or 0),
        api_hash=str(_env("api_hash", raw.get("api_hash", "")) or ""),
        bot_token=str(_env("bot_token", raw.get("bot_token", "")) or ""),
        bot_name=str(raw.get("bot_name") or "主要 Bot"),
        bots=bots,
        default_bot_id=str(raw.get("default_bot_id") or "default"),
        default_bot_chat_id=str(raw.get("default_bot_chat_id") or ""),
        admin_token=str(_env("admin_token", raw.get("admin_token", "")) or secrets.token_urlsafe(32)),
        admin_username=str(raw.get("admin_username") or "admin"),
        admin_salt=str(raw.get("admin_salt") or ""),
        admin_password_hash=str(raw.get("admin_password_hash") or ""),
        web_host=str(_env("web_host", raw.get("web_host", "0.0.0.0")) or "0.0.0.0"),
        web_port=int(_env("web_port", raw.get("web_port", 18001)) or 18001),
        enabled_plugins=[
            str(item) for item in (raw.get("enabled_plugins") or ["hello"])
            if str(item).strip()
        ],
        user_sessions=[
            str(item) for item in (raw.get("user_sessions") or [])
            if str(item).strip()
        ],
        plugin_config={
            str(key): dict(value)
            for key, value in (
                (raw.get("plugin_config") or {}).items()
                if isinstance(raw.get("plugin_config") or {}, dict) else []
            )
            if isinstance(value, dict)
        },
        plugin_accounts={
            str(key): [str(item) for item in value if str(item).strip()]
            for key, value in (raw.get("plugin_accounts") or {}).items()
            if isinstance(value, list)
        } if isinstance(raw.get("plugin_accounts") or {}, dict) else {},
        bot_routing={str(key): str(value) for key, value in (raw.get("bot_routing") or {}).items()}
        if isinstance(raw.get("bot_routing") or {}, dict) else {},
        plugin_order=[str(item) for item in (raw.get("plugin_order") or []) if str(item).strip()],
        ai_base_url=str(raw.get("ai_base_url") or "https://api.openai.com/v1"),
        ai_api_key=str(_env("ai_api_key", raw.get("ai_api_key", "")) or ""),
        ai_model=str(raw.get("ai_model") or "gpt-4.1-mini"),
        ai_settings=dict(raw.get("ai_settings") or {}) if isinstance(raw.get("ai_settings") or {}, dict) else {},
        cookie_settings=dict(raw.get("cookie_settings") or {}) if isinstance(raw.get("cookie_settings") or {}, dict) else {},
        plugin_repos=[
            str(item).strip() for item in (raw.get("plugin_repos") or ["AWdress/AWBotNest-Plugins"])
            if str(item).strip()
        ],
        plugin_repo_interval=max(1, int(raw.get("plugin_repo_interval", 20) or 20)),
        notification_channels=[dict(item) for item in (raw.get("notification_channels") or [])
                               if isinstance(item, dict)],
        proxy_url=str(_env("proxy_url", raw.get("proxy_url", "")) or "").strip(),
        webhook_secret=str(_env("webhook_secret", raw.get("webhook_secret", "")) or "").strip(),
        api_key=str(_env("api_key", raw.get("api_key", "")) or "").strip(),
        pip_index_url=str(_env("pip_index_url", raw.get("pip_index_url", "")) or "").strip(),
        log_cleaner=dict(raw.get("log_cleaner") or {
            "enabled": True, "keep_lines": 1000, "hour": 3, "minute": 0,
        }) if isinstance(raw.get("log_cleaner") or {}, dict) else {
            "enabled": True, "keep_lines": 1000, "hour": 3, "minute": 0,
        },
    )
    if not CONFIG_FILE.exists() or generated_admin_token:
        save_settings(settings)
    return settings


def save_settings(settings: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_FILE)
