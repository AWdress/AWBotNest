# 第三方库
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 自定义模块
from core import logger
from libs.state import state_manager


scheduler = AsyncIOScheduler()


from .universal.auto_changename import auto_changename_temp
from .universal.auto_avatar import auto_avatar_temp
from .universal.log_cleaner import start_log_cleaner

scheduler_jobs = {
    "autochangename": auto_changename_temp,
    "autoavatar": auto_avatar_temp,
}

async def start_scheduler():
    for job in (schedulers := state_manager.get_section("SCHEDULER", {})):
        logger.debug(f"Checking scheduler job: {job}")

        # 处理标准调度任务
        if schedulers[job] == "on" and job in scheduler_jobs:
            logger.debug(f"Starting scheduler job: {job}")
            try:
                job_func = scheduler_jobs[job]
                await job_func()  # 异步执行调度任务
            except Exception as e:
                logger.error(f"Failed to start job '{job}': {e}")

    # 日志清理由独立配置（SYSTEM.log_cleaner_enabled）管理，始终尝试启动
    try:
        await start_log_cleaner()
    except Exception as e:
        logger.error(f"Failed to start log_cleaner: {e}")


