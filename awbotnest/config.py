from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
SESSIONS_DIR = APP_ROOT / "sessions"
PLUGINS_DIR = APP_ROOT / "plugins"
CONFIG_FILE = DATA_DIR / "config.json"

# 在插件导入 CloakBrowser 前配置缓存；尊重管理员显式指定的目录。
os.environ.setdefault("CLOAKBROWSER_CACHE_DIR", str(DATA_DIR / "cloakbrowser"))
# 浏览器内核更新由平台统一检查和执行，关闭 CloakBrowser 自带的启动时后台更新。
# 首次没有可用内核时，CloakBrowser 仍会下载当前调用所需的内核。
os.environ["CLOAKBROWSER_AUTO_UPDATE"] = "false"
# License Key 只由系统设置管理，忽略容器或宿主机注入的同名环境变量。
os.environ.pop("CLOAKBROWSER_LICENSE_KEY", None)


def validate_config_format(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    if any(key in value for key in ("API_ID", "API_HASH", "BOTS", "AI_SERVICES")):
        raise ValueError("配置文件格式不受支持，请为 V2 使用独立的数据目录")
    # Validate the persisted form before a restore replaces the working file.
    # Optional fields stay optional so backups from earlier V2 releases work.
    def fail(field: str) -> None:
        raise ValueError(f"配置项 {field} 格式不正确")

    numeric = {"api_id": (0, 2**31 - 1), "web_port": (1, 65535),
               "plugin_repo_interval": (1, 525600)}
    for key, (minimum, maximum) in numeric.items():
        if key in value:
            item = value[key]
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                fail(key)
            if isinstance(item, str) and not re.fullmatch(r"[0-9]+", item):
                fail(key)
            if not minimum <= int(item) <= maximum:
                fail(key)
    string_fields = {
        "api_hash", "bot_token", "bot_name", "default_bot_id", "default_bot_chat_id",
        "admin_token", "admin_username", "admin_salt", "admin_password_hash", "web_host",
        "ai_base_url", "ai_api_key", "ai_model", "proxy_url", "webhook_secret", "api_key",
        "pip_index_url", "github_token", "browser_engine", "cloakbrowser_license_key",
    }
    for key in string_fields & value.keys():
        if not isinstance(value[key], str):
            fail(key)
    if value.get("browser_engine") not in (None, "", "chromium", "cloakbrowser"):
        fail("browser_engine")
    if "cloakbrowser_use_free_key" in value and not isinstance(value["cloakbrowser_use_free_key"], bool):
        fail("cloakbrowser_use_free_key")
    salt, password_hash = value.get("admin_salt", ""), value.get("admin_password_hash", "")
    if bool(salt) != bool(password_hash) or (salt and (
            not re.fullmatch(r"[0-9a-fA-F]{32}", salt)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", password_hash))):
        fail("admin_password_hash")
    for key in ("enabled_plugins", "user_sessions", "plugin_order", "plugin_repos"):
        if key not in value:
            continue
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            fail(key)
        if key == "user_sessions" and any(not re.fullmatch(r"[A-Za-z0-9_]+", item) for item in value[key]):
            fail(key)
    bots = value.get("bots", [])
    if not isinstance(bots, list):
        fail("bots")
    bot_ids: set[str] = set()
    for item in bots:
        if not isinstance(item, dict) or any(not isinstance(item.get(key, ""), str)
                                             for key in ("id", "name", "token")):
            fail("bots")
        bot_id = item.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", bot_id) or bot_id in bot_ids or bot_id == "default":
            fail("bots")
        bot_ids.add(bot_id)
    if value.get("default_bot_id", "default") not in {"", "default", *bot_ids}:
        fail("default_bot_id")
    for key in ("plugin_config", "plugin_accounts", "bot_routing", "ai_settings", "cookie_settings", "log_cleaner"):
        if key in value and not isinstance(value[key], dict):
            fail(key)
    for key, item in value.get("plugin_config", {}).items():
        if not isinstance(key, str) or not isinstance(item, dict):
            fail("plugin_config")
    for key, item in value.get("plugin_accounts", {}).items():
        if (not isinstance(key, str) or not isinstance(item, list)
                or any(not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_]+", account)
                       for account in item)):
            fail("plugin_accounts")
    if any(not isinstance(key, str) or not isinstance(item, str)
           for key, item in value.get("bot_routing", {}).items()):
        fail("bot_routing")
    channels = value.get("notification_channels", [])
    if not isinstance(channels, list) or any(not isinstance(item, dict) for item in channels):
        fail("notification_channels")
    for item in channels:
        if any(key in item and not isinstance(item[key], str) for key in ("id", "type", "name")):
            fail("notification_channels")
        if "config" in item and not isinstance(item["config"], dict):
            fail("notification_channels.config")
    cleaner = value.get("log_cleaner", {})
    for key, (minimum, maximum) in {"hour": (0, 23), "minute": (0, 59), "keep_lines": (1, 1000)}.items():
        if key in cleaner and (isinstance(cleaner[key], bool) or not isinstance(cleaner[key], int)
                               or not minimum <= cleaner[key] <= maximum):
            fail(f"log_cleaner.{key}")
    if "enabled" in cleaner and not isinstance(cleaner["enabled"], bool):
        fail("log_cleaner.enabled")
    ai = value.get("ai_settings", {})
    for key in ("providers", "models"):
        if key in ai and (not isinstance(ai[key], list)
                         or any(not isinstance(item, dict) for item in ai[key])):
            fail(f"ai_settings.{key}")
        for item in ai.get(key, []):
            if any(field in item and not isinstance(item[field], str)
                   for field in ("id", "name", "alias", "api_key", "base_url", "model", "provider_id", "api_format")):
                fail(f"ai_settings.{key}")
            if "enabled" in item and not isinstance(item["enabled"], bool):
                fail(f"ai_settings.{key}.enabled")
            if "capabilities" in item and (not isinstance(item["capabilities"], list)
                                            or any(not isinstance(capability, str) for capability in item["capabilities"])):
                fail(f"ai_settings.{key}.capabilities")
    for key in ("capabilities", "plugin_permissions"):
        if key in ai and (not isinstance(ai[key], dict)
                          or any(not isinstance(item, dict) for item in ai[key].values())):
            fail(f"ai_settings.{key}")
    for item in ai.get("plugin_permissions", {}).values():
        if "models" in item and not isinstance(item["models"], dict):
            fail("ai_settings.plugin_permissions.models")
        if "capabilities" in item and (not isinstance(item["capabilities"], list)
                                        or any(not isinstance(capability, str) for capability in item["capabilities"])):
            fail("ai_settings.plugin_permissions.capabilities")
    cookie = value.get("cookie_settings", {})
    for key in ("uuid", "password", "token", "remote_url", "remote_uuid", "remote_password", "crypto_type", "remote_crypto_type"):
        if key in cookie and not isinstance(cookie[key], str):
            fail(f"cookie_settings.{key}")
    if "remote_interval_minutes" in cookie:
        interval = cookie["remote_interval_minutes"]
        if (isinstance(interval, bool) or not isinstance(interval, (int, str))
                or isinstance(interval, str) and not re.fullmatch(r"[0-9]+", interval)
                or not 0 <= int(interval) <= 525600):
            fail("cookie_settings.remote_interval_minutes")
    if "remote_domains" in cookie and (not isinstance(cookie["remote_domains"], list)
                                        or any(not isinstance(domain, str) for domain in cookie["remote_domains"])):
        fail("cookie_settings.remote_domains")


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
    github_token: str = ""
    browser_engine: str = "chromium"
    cloakbrowser_use_free_key: bool = False
    cloakbrowser_license_key: str = ""
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


def load_settings(*, persist_defaults: bool = True) -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    raw: dict[str, object] = {}
    if CONFIG_FILE.exists():
        try:
            value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            validate_config_format(value)
            raw = value
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("配置文件无法读取，已保留原文件，请从备份恢复") from exc
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
    stored_cloak_key = str(raw.get("cloakbrowser_license_key") or "").strip()
    stored_cloak_key_enabled = raw.get(
        "cloakbrowser_use_free_key", bool(stored_cloak_key),
    ) is True
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
            str(item) for item in (raw.get("enabled_plugins", []) or [])
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
        github_token=str(_env("github_token", raw.get("github_token", "")) or "").strip(),
        browser_engine=(str(raw.get("browser_engine") or "chromium").strip()
                        if str(raw.get("browser_engine") or "chromium").strip()
                        in {"cloakbrowser", "chromium"} else "chromium"),
        cloakbrowser_use_free_key=stored_cloak_key_enabled and bool(stored_cloak_key),
        cloakbrowser_license_key=stored_cloak_key,
        log_cleaner=dict(raw.get("log_cleaner") or {
            "enabled": True, "keep_lines": 1000, "hour": 3, "minute": 0,
        }) if isinstance(raw.get("log_cleaner") or {}, dict) else {
            "enabled": True, "keep_lines": 1000, "hour": 3, "minute": 0,
        },
    )
    if persist_defaults and (not CONFIG_FILE.exists() or generated_admin_token):
        save_settings(settings)
    return settings


def save_settings(settings: Settings) -> None:
    import tempfile
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA_DIR,
                                     prefix=".config-", suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(payload)
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(CONFIG_FILE)
    finally:
        temp.unlink(missing_ok=True)
