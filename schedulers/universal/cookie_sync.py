"""远程 CookieCloud 定时同步。"""
from datetime import datetime

from core import logger
from kernel import cookies as cookie_kernel


JOB_ID = "cookiecloud_remote_sync"


async def remote_cookie_sync_action() -> None:
    try:
        status = await cookie_kernel.pull_remote_snapshot()
        logger.info(
            "远程 CookieCloud 同步完成：%d 个 Cookie，%d 个域名",
            status["cookie_count"],
            status["domain_count"],
        )
    except cookie_kernel.CookieServiceError as exc:
        logger.warning("远程 CookieCloud 同步失败：%s", exc)


async def start_remote_cookie_sync(*, run_now: bool = True) -> None:
    from schedulers import scheduler

    if scheduler.get_job(JOB_ID):
        scheduler.remove_job(JOB_ID)

    settings = cookie_kernel.load_settings()
    if not settings["enabled"] or not settings["remote_enabled"]:
        logger.info("远程 CookieCloud 定时同步已停止")
        return

    interval = settings["remote_interval_minutes"]
    job_options = {}
    if run_now:
        job_options["next_run_time"] = datetime.now().astimezone()
    scheduler.add_job(
        remote_cookie_sync_action,
        "interval",
        minutes=interval,
        id=JOB_ID,
        name="远程 Cookie 同步",
        replace_existing=True,
        **job_options,
    )
    logger.info("远程 CookieCloud 定时同步已启动（每 %d 分钟）", interval)
