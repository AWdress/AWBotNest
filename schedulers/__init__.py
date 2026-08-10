# 第三方库
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 自定义模块
from core import logger
from libs.state import state_manager


# 容器启动、插件重载或事件循环短暂繁忙时，任务可能比计划晚几秒。
# APScheduler 默认只容忍 1 秒，容易把本应执行的任务直接跳过。
JOB_MISFIRE_GRACE_SECONDS = 300


scheduler = AsyncIOScheduler(
    timezone="Asia/Shanghai",
    job_defaults={
        "misfire_grace_time": JOB_MISFIRE_GRACE_SECONDS,
        "coalesce": True,
        "max_instances": 1,
    },
)


def _log_job_event(event) -> None:
    """把调度失败写入平台日志，避免任务静默失效。"""
    job_id = str(getattr(event, "job_id", "") or "未知任务")
    scheduled_time = getattr(event, "scheduled_run_time", None)
    if event.code == EVENT_JOB_MISSED:
        logger.warning("定时任务错过执行时间 [%s]：%s", job_id, scheduled_time)
    elif event.code == EVENT_JOB_ERROR:
        error = getattr(event, "exception", None)
        logger.error("定时任务执行失败 [%s]：%r", job_id, error)


scheduler.add_listener(_log_job_event, EVENT_JOB_MISSED | EVENT_JOB_ERROR)


from .universal.log_cleaner import start_log_cleaner

scheduler_jobs = {}

async def start_scheduler():
    # 所有业务定时任务已迁移到插件系统
    # 仅保留平台级的日志清理任务

    # 日志清理由独立配置（SYSTEM.log_cleaner_enabled）管理，始终尝试启动
    try:
        await start_log_cleaner()
    except Exception as e:
        logger.error(f"日志清理任务启动失败: {e}")


