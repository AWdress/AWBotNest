# 第三方库
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 自定义模块
from core import logger
from libs.state import state_manager


scheduler = AsyncIOScheduler()


from .universal.log_cleaner import start_log_cleaner

scheduler_jobs = {}

async def start_scheduler():
    # 所有业务定时任务已迁移到插件系统
    # 仅保留平台级的日志清理任务

    # 日志清理由独立配置（SYSTEM.log_cleaner_enabled）管理，始终尝试启动
    try:
        await start_log_cleaner()
    except Exception as e:
        logger.error(f"Failed to start log_cleaner: {e}")


