"""
infra/scheduler.py
定时任务注册与管理

封装 APScheduler，统一管理所有定时任务的注册、启动和停止。
替代根目录下的 schedulers/ 包，作为任务的统一入口。
"""

from __future__ import annotations

from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler


# 全局调度器单例
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

# 任务注册表：job_id -> 初始化函数（用于开关控制）
scheduler_jobs: dict[str, Any] = {}


def register_job(name: str, init_func: Any) -> None:
    """注册一个定时任务到全局注册表"""
    scheduler_jobs[name] = init_func


async def start_scheduler() -> None:
    """启动调度器（在 app.py 的启动流程中调用）"""
    if not scheduler.running:
        scheduler.start()


async def stop_scheduler() -> None:
    """停止调度器（在 app.py 的关闭流程中调用）"""
    if scheduler.running:
        scheduler.shutdown(wait=False)


def get_job_ids() -> list[str]:
    """获取所有已注册的任务 ID"""
    return [job.id for job in scheduler.get_jobs()]
