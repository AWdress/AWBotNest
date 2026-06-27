# 第三方库
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 自定义模块
from core import logger
from libs.state import state_manager


scheduler = AsyncIOScheduler()


from .zhuque.fireGenshinCharacterMagic import zhuque_autofire_firsttimeget
from .universal.auto_changename import auto_changename_temp
from .universal.auto_avatar import auto_avatar_temp
from .universal.custom_auto_reply import custom_auto_reply_init
from .universal.log_cleaner import start_log_cleaner

scheduler_jobs = {
    "autofire": zhuque_autofire_firsttimeget,
    "autochangename": auto_changename_temp,
    "autoavatar": auto_avatar_temp,
    "custom_auto_reply": custom_auto_reply_init,
}

async def start_scheduler():    
    for job in (schedulers := state_manager.get_section("SCHEDULER", {})):
        logger.debug(f"Checking scheduler job: {job}")
        
        # 处理自定义定时回复任务（格式：custom_auto_reply_任务ID）
        if job.startswith("custom_auto_reply_") and schedulers[job] == "on":
            task_id = job.replace("custom_auto_reply_", "")
            logger.debug(f"Starting custom auto reply task: {task_id}")
            try:
                from .universal.custom_auto_reply import init_custom_auto_reply_task
                await init_custom_auto_reply_task(task_id)
            except Exception as e:
                logger.error(f"Failed to start custom auto reply task '{task_id}': {e}")
        
        # 处理其他标准调度任务
        elif schedulers[job] == "on" and job in scheduler_jobs:
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


