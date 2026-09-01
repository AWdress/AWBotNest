"""AWBotNest 主启动入口。"""

from __future__ import annotations

import asyncio
import logging
import platform

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


async def run_once() -> bool:
    logging.getLogger().addHandler(memory_logs)
    logger = logging.getLogger("awbotnest.main")
    logger.info("==================================================")
    logger.info("  AWBotNest v%s (Python %s)", __version__, platform.python_version())
    logger.info("==================================================")

    restored = BackupManager.apply_pending()
    if restored:
        logging.getLogger("awbotnest.backup").info("已应用待恢复备份")
    settings = load_settings()
    accounts = TelegramAccounts(settings)
    scheduler = PluginScheduler()
    scheduler.start()
    cleaner = settings.log_cleaner
    if cleaner.get("enabled", True):
        scheduler.add_cron(
            "__platform__", "log-cleaner",
            lambda: memory_logs.trim(int(cleaner.get("keep_lines", 1000))),
            hour=max(0, min(int(cleaner.get("hour", 3)), 23)),
            minute=max(0, min(int(cleaner.get("minute", 0)), 59)),
        )
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

    scanned_plugins = runtime.scan()
    await runtime.restore()
    loaded_names = [p.meta.name for p in runtime.loaded.values()]
    logger.info(
        "插件系统初始化完成：扫描到 %d 个插件，已启用 %d 个%s",
        len(scanned_plugins),
        len(runtime.loaded),
        f" ({', '.join(loaded_names)})" if loaded_names else "",
    )

    scheduler.add_interval(
        "__platform__", "插件市场轮询", market.refresh,
        seconds=max(1, int(settings.plugin_repo_interval)) * 60,
    )
    scheduler.add_cron(
        "__platform__", "插件仓库自动发现", market.discover_repositories,
        hour=0, minute=0,
    )
    logger.info("定时任务调度器已就绪：已注册 %d 个后台任务", len(scheduler.jobs()))

    display_host = "127.0.0.1" if settings.web_host in {"0.0.0.0", "::"} else settings.web_host
    logger.info("Web 控制台已启动，访问地址: http://%s:%s", display_host, settings.web_port)
    logger.info("==================================================")

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
