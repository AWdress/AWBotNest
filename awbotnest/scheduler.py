"""平台托管的插件调度器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler


class PluginScheduler:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def _check_limit(self, plugin_id: str) -> None:
        prefix = f"{plugin_id}::"
        if sum(job.id.startswith(prefix) for job in self.scheduler.get_jobs()) >= 64:
            raise RuntimeError("单个插件最多注册 64 个定时任务")

    def add_interval(self, plugin_id: str, name: str, callback: Callable[..., Any],
                     *, seconds: int, replace_existing: bool = True) -> str:
        job_id = f"{plugin_id}::{name}"
        if not replace_existing or self.scheduler.get_job(job_id) is None:
            self._check_limit(plugin_id)
        self.scheduler.add_job(
            callback,
            "interval",
            seconds=max(1, int(seconds)),
            id=job_id,
            replace_existing=replace_existing,
            coalesce=True,
            max_instances=1,
        )
        return job_id

    def add_cron(self, plugin_id: str, name: str, callback: Callable[..., Any],
                 *, replace_existing: bool = True, **fields: Any) -> str:
        job_id = f"{plugin_id}::{name}"
        if not replace_existing or self.scheduler.get_job(job_id) is None:
            self._check_limit(plugin_id)
        self.scheduler.add_job(
            callback,
            "cron",
            id=job_id,
            replace_existing=replace_existing,
            coalesce=True,
            max_instances=1,
            **fields,
        )
        return job_id

    def remove_plugin(self, plugin_id: str) -> None:
        prefix = f"{plugin_id}::"
        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                self.scheduler.remove_job(job.id)

    def jobs(self) -> list[dict[str, object]]:
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self.scheduler.get_jobs()
        ]

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
