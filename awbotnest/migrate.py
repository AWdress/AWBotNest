from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
import stat
from pathlib import Path

from .config import DATA_DIR, BotSettings, load_settings, save_settings

PENDING_MIGRATION = DATA_DIR / ".v1-migration-pending.zip"


def extract_source(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as package:
        total = 0
        for item in package.infolist():
            target = (destination / item.filename).resolve()
            if (destination.resolve() not in target.parents or "\\" in item.orig_filename
                    or ":" in item.filename or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError("压缩包包含不安全路径")
            total += item.file_size
            if total > 1024 * 1024 * 1024:
                raise ValueError("压缩包解压内容超过 1 GB")
        package.extractall(destination)
    candidates = [destination] + [p for p in destination.iterdir() if p.is_dir()]
    roots = [p for p in candidates if (p / "data" / "config.json").is_file()]
    if len(roots) != 1:
        raise ValueError("数据包必须包含唯一的 data/config.json")
    legacy = _read(roots[0] / "data" / "config.json")
    if not any(key in legacy for key in ("API_ID", "API_HASH", "BOTS", "AI_SERVICES")):
        raise ValueError("未找到有效的 V1 配置")
    return roots[0]


def stage_migration(content: bytes) -> dict[str, object]:
    if len(content) > 512 * 1024 * 1024:
        raise ValueError("V1 数据包超过 512 MB")
    PENDING_MIGRATION.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="awbotnest-v1-") as temporary:
        archive = Path(temporary) / "v1.zip"
        archive.write_bytes(content)
        extract_source(archive, Path(temporary) / "source")
        staged = PENDING_MIGRATION.with_suffix(".tmp")
        try:
            shutil.copyfile(archive, staged)
            staged.replace(PENDING_MIGRATION)
        finally:
            staged.unlink(missing_ok=True)
    return {"restart_required": True, "message": "V1 数据包已校验，重启时先备份再迁移；当前运行数据未修改。"}


def apply_pending_migration() -> bool:
    if not PENDING_MIGRATION.exists():
        return False
    from .backup import BackupManager
    with tempfile.TemporaryDirectory(prefix="awbotnest-v1-") as temporary:
        source = extract_source(PENDING_MIGRATION, Path(temporary) / "source")
        BackupManager.create()
        migrate(source)
    PENDING_MIGRATION.unlink()
    return True


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def migrate(source: Path) -> dict[str, object]:
    # 接受包含 data 目录的项目根目录，拒绝空包/V2 配置。
    if (source / "config.json").is_file() and not (source / "data" / "config.json").is_file():
        raise ValueError("数据包需包含 data/config.json，请压缩整个 data 目录")
    old_config = _read(source / "data" / "config.json")
    if not any(key in old_config for key in ("API_ID", "API_HASH", "BOTS", "AI_SERVICES")):
        raise ValueError("未找到有效的 V1 配置 data/config.json，未修改任何数据")
    old_plugins = _read(source / "data" / "plugins_state.json")
    old_auth = _read(source / "data" / "auth.json")
    settings = load_settings(persist_defaults=False)
    if old_auth.get("username") and old_auth.get("salt") and old_auth.get("pwd_hash"):
        settings.admin_username = str(old_auth["username"])
        settings.admin_salt = str(old_auth["salt"])
        settings.admin_password_hash = str(old_auth["pwd_hash"])
        if old_auth.get("secret"):
            settings.admin_token = str(old_auth["secret"])
    settings.api_id = int(old_config.get("API_ID") or settings.api_id)
    settings.api_hash = str(old_config.get("API_HASH") or settings.api_hash)
    settings.web_port = int(old_config.get("WEB_UI_PORT") or settings.web_port)
    if not 1 <= settings.web_port <= 65535:
        raise ValueError("V1 网页端口不合法")
    settings.bot_token = str(old_config.get("BOT_TOKEN") or settings.bot_token)
    settings.bot_name = str(old_config.get("BOT_NAME") or settings.bot_name)
    settings.default_bot_id = str(old_config.get("DEFAULT_BOT_ID") or settings.default_bot_id)
    settings.default_bot_chat_id = str(old_config.get("DEFAULT_BOT_CHAT_ID") or "")
    settings.webhook_secret = str(old_config.get("WEBHOOK_SECRET") or settings.webhook_secret)
    settings.api_key = str(old_config.get("API_KEY") or settings.api_key)
    settings.pip_index_url = str(old_config.get("PIP_INDEX_URL") or settings.pip_index_url)
    if isinstance(old_config.get("LOG_CLEANER"), dict):
        settings.log_cleaner = dict(old_config["LOG_CLEANER"])
    settings.bots = [
        BotSettings(str(bot.get("id")), str(bot.get("name") or bot.get("id")), str(bot.get("token") or ""))
        for bot in (old_config.get("BOTS") or [])
        if isinstance(bot, dict) and bot.get("id")
    ]
    settings.plugin_config = {
        str(key): dict(value) for key, value in (old_plugins.get("config") or {}).items()
        if isinstance(value, dict)
    }
    settings.plugin_accounts = {
        str(key): [str(item) for item in value]
        for key, value in (old_plugins.get("account_scope") or {}).items()
        if isinstance(value, list)
    }
    settings.bot_routing = {
        str(key): str(value) for key, value in (old_plugins.get("bot_choice") or {}).items()
    }
    try:
        value = json.loads((source / "data" / "webui" / "plugin_order.json").read_text(encoding="utf-8"))
        if isinstance(value, list):
            settings.plugin_order = [str(item) for item in value]
        elif isinstance(value, dict):
            order = value.get("order") or value.get("plugins") or []
            if isinstance(order, list):
                settings.plugin_order = [str(item) for item in order]
    except (OSError, json.JSONDecodeError):
        pass
    settings.ai_base_url = str(old_config.get("AI_BASE_URL") or settings.ai_base_url)
    settings.ai_api_key = str(old_config.get("AI_API_KEY") or settings.ai_api_key)
    settings.ai_model = str(old_config.get("AI_MODEL") or settings.ai_model)
    ai_services = old_config.get("AI_SERVICES") or {}
    if isinstance(ai_services, dict):
        settings.ai_settings = dict(ai_services)
        provider = next((item for item in (ai_services.get("providers") or [])
                         if isinstance(item, dict) and item.get("enabled", True)), None)
        if provider:
            settings.ai_base_url = str(provider.get("base_url") or settings.ai_base_url)
            settings.ai_api_key = str(provider.get("api_key") or settings.ai_api_key)
        capabilities = ai_services.get("capabilities") or {}
        text_capability = capabilities.get("text") if isinstance(capabilities, dict) else {}
        if isinstance(text_capability, dict):
            settings.ai_model = str(text_capability.get("default_model") or settings.ai_model)
    cookie_settings = old_config.get("COOKIE_SERVICE") or old_config.get("COOKIE_SETTINGS") or {}
    if isinstance(cookie_settings, dict):
        settings.cookie_settings = dict(cookie_settings)
    settings.plugin_repos = [
        str(item.get("url") or "") if isinstance(item, dict) else str(item)
        for item in (old_config.get("PLUGIN_REPOS") or settings.plugin_repos)
        if (item.get("url") if isinstance(item, dict) else item)
    ]
    settings.notification_channels = []
    known_bot_ids = {item.id for item in settings.bots}
    for item in (old_config.get("NOTIFICATION_CHANNELS") or []):
        if not isinstance(item, dict):
            continue
        nested = item.get("config") if isinstance(item.get("config"), dict) else {}
        channel = {**nested, **item}
        channel.pop("config", None)
        channel_id = str(channel.get("id") or f"channel{len(settings.notification_channels) + 1}")
        channel["id"] = channel_id
        if channel.get("type") == "wechat":
            channel["type"] = "wecom"
        if channel.get("type") == "telegram" and channel.get("token"):
            if channel_id == "default":
                settings.bot_token = str(channel["token"])
                settings.bot_name = str(channel.get("name") or settings.bot_name)
            elif channel_id not in known_bot_ids:
                settings.bots.append(BotSettings(channel_id, str(channel.get("name") or channel_id), str(channel["token"])))
                known_bot_ids.add(channel_id)
        channel["bot_id"] = channel_id if channel.get("type") == "telegram" else channel.get("bot_id", "")
        channel.pop("token", None)
        settings.notification_channels.append(channel)
    old_proxy = old_config.get("proxy_set") or {}
    if isinstance(old_proxy, dict) and old_proxy.get("proxy_enable"):
        direct = str(old_proxy.get("PROXY_URL") or "").strip()
        details = old_proxy.get("proxy") or {}
        if direct:
            settings.proxy_url = direct
        elif isinstance(details, dict) and details.get("hostname") and details.get("port"):
            scheme = str(details.get("scheme") or "http")
            username = str(details.get("username") or "")
            password = str(details.get("password") or "")
            auth = f"{username}:{password}@" if username else ""
            settings.proxy_url = f"{scheme}://{auth}{details['hostname']}:{details['port']}"
    # 只有清单明确兼容 2.0 的插件才应加入 enabled_plugins；旧启用状态不自动套用。
    settings.user_sessions = []
    copied: list[str] = []
    skipped: list[str] = []

    def copy_tree_files(old_root: Path, new_root: Path, label: str) -> None:
        if not old_root.exists():
            return
        for old_path in old_root.rglob("*"):
            if not old_path.is_file() or old_path.is_symlink():
                continue
            relative = old_path.relative_to(old_root)
            target = new_root / relative
            if target.exists():
                skipped.append(f"{label}/{relative.as_posix()}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, target)
            copied.append(f"{label}/{relative.as_posix()}")

    # v1 的插件 KV 同为逐插件 SQLite；插件私有文件迁移到 2.0 的 data/plugins。
    copy_tree_files(source / "data" / "kv", DATA_DIR / "kv", "kv")
    copy_tree_files(source / "data" / "plugin_data", DATA_DIR / "plugins", "plugin_data")
    # 文件复制成功后才提交配置；失败时旧配置仍可用于再次迁移。
    save_settings(settings)
    return {
        "config_migrated": True,
        "admin_credentials_migrated": bool(old_auth.get("pwd_hash")),
        "plugin_config_count": len(settings.plugin_config),
        "plugin_account_scope_count": len(settings.plugin_accounts),
        "bot_routing_count": len(settings.bot_routing),
        "data_files_copied": copied,
        "existing_files_skipped": skipped,
        "telegram_sessions_migrated": False,
        "incompatible": ["Pyrogram 用户 Session", "Pyrogram 插件源码与运行时状态"],
        "message": "配置、插件作用域、Bot 路由、KV 和插件私有数据已迁移；Telethon 用户账号需要重新登录，兼容插件需重新安装并启用。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移 AWBotNest 现行版配置到 2.0")
    parser.add_argument("--source", type=Path, required=True, help="现行版 AWBotNest 根目录")
    args = parser.parse_args()
    print(json.dumps(migrate(args.source.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
