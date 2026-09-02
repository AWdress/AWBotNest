"""AWBotNest 主启动入口。"""

from __future__ import annotations

import asyncio
import logging
import platform
import json

import uvicorn

from . import __version__
from .app import create_app
from .config import load_settings
from .plugins import PluginRuntime
from .telegram import TelegramAccounts
from .scheduler import PluginScheduler
from .services import PlatformServices
from .logs import memory_logs
from .routing import PluginRoutes
from .notifier import NotificationService
from .backup import BackupManager
from .activity import activity
from .market import PluginMarket
from .migrate import migrate as migrate_v1, apply_pending_migration
from .config import APP_ROOT, DATA_DIR


async def run_once() -> bool:
    logging.getLogger().addHandler(memory_logs)
    logger = logging.getLogger("awbotnest.main")
    logger.info("  AWBotNest v%s (Python %s)", __version__, platform.python_version())

    restored = BackupManager.apply_pending()
    if restored:
        logging.getLogger("awbotnest.backup").info("已应用待恢复备份")
    if apply_pending_migration():
        logger.info("已备份原数据并应用 V1 迁移包")
    # Docker 直升：同一数据卷中检测 V1 配置并只自动迁移一次。
    migration_marker = DATA_DIR / ".v1-migrated"
    legacy_config = DATA_DIR / "config.json"
    if not migration_marker.exists() and legacy_config.exists():
        try:
            legacy = json.loads(legacy_config.read_text(encoding="utf-8"))
            if isinstance(legacy, dict) and any(key in legacy for key in ("API_ID", "API_HASH", "BOTS", "AI_SERVICES")):
                logger.info("检测到 V1 数据，开始自动迁移到 V2…")
                backup = BackupManager.create()
                logger.info("V1 迁移前备份已保存：%s", backup.name)
                result = migrate_v1(APP_ROOT)
                migration_marker.write_text("v2\n", encoding="utf-8")
                logger.info("V1 数据自动迁移完成：插件配置 %d 项，复制文件 %d 个",
                            result.get("plugin_config_count", 0), len(result.get("data_files_copied", [])))
        except Exception as exc:
            logger.exception("V1 数据自动迁移失败，停止启动以保护原始数据：%s", exc)
            raise
    settings = load_settings()
    accounts = TelegramAccounts(settings)
    scheduler = PluginScheduler()
    scheduler.start()
    cleaner = settings.log_cleaner
    if cleaner.get("enabled", True):
        cleaner_hour = max(0, min(int(cleaner.get("hour", 3)), 23))
        cleaner_minute = max(0, min(int(cleaner.get("minute", 0)), 59))
        scheduler.add_cron(
            "__platform__", "log-cleaner",
            lambda: memory_logs.trim(int(cleaner.get("keep_lines", 1000))),
            hour=cleaner_hour, minute=cleaner_minute,
        )
        logger.info("日志清理定时任务已启动（每天 %d:%02d 执行）", cleaner_hour, cleaner_minute)
    services = PlatformServices(settings)
    routes = PluginRoutes()
    notifier = NotificationService(settings, accounts, services.http)
    runtime = PluginRuntime(settings, accounts, scheduler, services, routes, notifier)
    market = PluginMarket(settings)

    await accounts.start()
    bot_spec_map = {spec.id: spec.name for spec in settings.bot_specs()}
    bot_names = [bot_spec_map.get(bot_id, bot_id) for bot_id in accounts.bots.keys()]
    user_names = list(accounts.users.keys())
    logger.info(
        "Telegram 账号初始化完成：Bot %d 个%s，User 账号 %d 个%s",
        len(bot_names), f" {bot_names}" if bot_names else "",
        len(user_names), f" {user_names}" if user_names else "",
    )
    logger.info("账号连接自动恢复已启用")

    scanned_plugins = runtime.scan()
    await runtime.restore()
    logger.info("插件恢复完成，已加载 %d 个（扫描到 %d 个）", len(runtime.loaded), len(scanned_plugins))

    async def poll_plugin_market():
        return await market.poll_updates(runtime)

    scheduler.add_interval(
        "__platform__", "插件市场轮询", poll_plugin_market,
        seconds=max(1, int(settings.plugin_repo_interval)) * 60,
    )
    logger.info("插件仓库轮询已注册：每 %d 分钟，%d 个仓库（含官方）",
                max(1, int(settings.plugin_repo_interval)), len(settings.plugin_repos))
    scheduler.add_cron(
        "__platform__", "插件仓库自动发现", market.discover_repositories,
        hour=0, minute=0,
    )
    logger.info("插件仓库自动发现已注册：每天 0:00 执行")
    cookie_settings = settings.cookie_settings
    if cookie_settings.get("remote_enabled"):
        async def sync_remote_cookiecloud() -> None:
            from .cookiecloud import pull, record_sync
            try:
                values = await pull(
                    str(cookie_settings.get("remote_url") or ""),
                    str(cookie_settings.get("remote_uuid") or ""),
                    str(cookie_settings.get("remote_password") or ""),
                    str(cookie_settings.get("remote_crypto_type") or "auto"),
                    settings.proxy_url or None,
                )
                await services.cookies.replace(values)
                count = sum(len(item) for item in values.values())
                record_sync("remote", "success", "远程 CookieCloud 自动同步完成", len(values), count)
                logger.info("远程 CookieCloud 同步完成：%d 个 Cookie，%d 个域名", count, len(values))
            except Exception as exc:
                record_sync("remote", "error", f"自动同步失败：{exc}")
                raise
        scheduler.add_interval(
            "__platform__", "远程 CookieCloud 同步", sync_remote_cookiecloud,
            seconds=max(5, int(cookie_settings.get("remote_interval_minutes") or 60)) * 60,
        )
        logger.info("远程 CookieCloud 定时同步已启动（每 %d 分钟）",
                    max(5, int(cookie_settings.get("remote_interval_minutes") or 60)))
        try:
            await sync_remote_cookiecloud()
        except Exception as exc:
            logger.warning("远程 CookieCloud 首次同步失败：%s", exc)
    logger.info("定时任务调度器已就绪：已注册 %d 个后台任务", len(scheduler.jobs()))

    display_host = "127.0.0.1" if settings.web_host in {"0.0.0.0", "::"} else settings.web_host
    logger.info("Web 控制台已启动，访问地址: http://%s:%s", display_host, settings.web_port)
    logger.info("AWBotNest 启动完成")

    restart_event = asyncio.Event()
    app = create_app(settings, accounts, runtime, scheduler, routes, restart_event, market)
    server = uvicorn.Server(uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    ))
    async def stop_for_restart() -> None:
        await restart_event.wait()
        server.should_exit = True
        # Open WebSocket clients may keep Uvicorn waiting indefinitely. Give
        # them a brief graceful-close window, then finish the in-process
        # restart so configuration changes (including the listen port) apply.
        await asyncio.sleep(2)
        if not server.force_exit:
            server.force_exit = True

    restart_watcher = asyncio.create_task(stop_for_restart())
    try:
        await server.serve()
    finally:
        restart_watcher.cancel()
        await runtime.stop()
        await accounts.stop()
        scheduler.stop()
        activity.flush()
        logging.getLogger().removeHandler(memory_logs)
    return restart_event.is_set()


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Keep the user-facing stream focused on platform activity. Third-party
    # clients still report warnings and errors, without flooding it at INFO.
    for logger_name in ("apscheduler", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    # Telegram reconnects automatically; its transient disconnect warnings are
    # implementation noise in the admin UI. Actual failures remain visible.
    logging.getLogger("telethon").setLevel(logging.ERROR)
    while await run_once():
        logging.getLogger("awbotnest.main").info("平台正在重新加载配置并启动")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
