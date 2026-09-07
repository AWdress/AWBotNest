"""平台托管的插件调度器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
import asyncio
import inspect
import time
import logging
import contextvars
from datetime import datetime, timedelta
from apscheduler.triggers.cron import CronTrigger

from apscheduler.schedulers.asyncio import AsyncIOScheduler

_current_state = contextvars.ContextVar("scheduler_state", default=None)


class PluginScheduler:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai", job_defaults={
            "misfire_grace_time": 300, "coalesce": True, "max_instances": 1,
        })
        self._states = {}
        self._manual = {}
        self._running = {}
        self._stopping = False

    def _tracked(self, job_id, callback):
        async def execute():
            state = self._states.get(job_id, {})
            if state.get("status") == "running":
                return
            state = {"status": "running", "step": "执行中", "started": time.monotonic()}
            self._states[job_id] = state
            self._running[job_id] = asyncio.current_task()
            token = _current_state.set(state)
            try:
                result = callback()
                if inspect.isawaitable(result):
                    result = await result
                failed = isinstance(result, dict) and result.get("ok") is False
                state.update(status="failed" if failed else "success",
                             step="执行失败，请查看运行日志" if failed else "执行完成")
                return result
            except asyncio.CancelledError:
                state.update(status="cancelled", step="执行已取消")
                raise
            except Exception:
                state.update(status="failed", step="执行失败，请查看运行日志")
                logging.getLogger("awbotnest.scheduler").exception("定时任务执行失败：%s", job_id.split("::", 1)[-1])
            finally:
                _current_state.reset(token)
                self._running.pop(job_id, None)
                state["duration_seconds"] = int(time.monotonic() - state["started"])
        return execute

    def report_progress(self, *, percent=None, step=None):
        state = _current_state.get()
        if state is None or state.get("status") != "running":
            return False
        if percent is not None:
            state["percent"] = max(0, min(100, float(percent)))
        if step is not None:
            state["step"] = str(step)
        return True

    def run_now(self, job_id):
        job = self.scheduler.get_job(job_id)
        if job is None:
            raise LookupError("定时任务不存在")
        if self._states.get(job_id, {}).get("status") in {"pending", "running"}:
            raise ValueError("任务正在执行，请勿重复提交")
        self._states[job_id] = {"status": "pending", "step": "等待执行", "duration_seconds": 0}
        task = asyncio.create_task(job.func(*job.args, **job.kwargs))
        self._manual[job_id] = task
        task.add_done_callback(lambda done: self._manual.pop(job_id, None) if self._manual.get(job_id) is done else None)

    def snapshot(self, job_id):
        state = dict(self._states.get(job_id, {}))
        started = state.pop("started", None)
        if state.get("status") == "running" and started is not None:
            state["duration_seconds"] = int(time.monotonic() - started)
        return {"running": state.get("status") in {"pending", "running"}, "progress": state}

    def start(self) -> None:
        if not self.scheduler.running:
            self._stopping = False
            self.scheduler.start()

    def _check_limit(self, plugin_id: str) -> None:
        prefix = f"{plugin_id}::"
        if sum(job.id.startswith(prefix) for job in self.scheduler.get_jobs()) >= 64:
            raise RuntimeError("单个插件最多注册 64 个定时任务")

    def add(self, plugin_id, name, callback, trigger, **fields):
        self._check_limit(plugin_id)
        base = f"{plugin_id}::{name}"
        job_id, suffix = base, 0
        while self.scheduler.get_job(job_id):
            suffix += 1
            job_id = f"{base}#{suffix}"
        explicit_next = "next_run_time" in fields
        args, kwargs = fields.pop("args", ()), fields.pop("kwargs", {})
        async def invoke():
            if inspect.iscoroutinefunction(callback):
                return await callback(*args, **kwargs)
            value = await asyncio.to_thread(callback, *args, **kwargs)
            return await value if inspect.isawaitable(value) else value
        job = self.scheduler.add_job(self._tracked(job_id, invoke), trigger, id=job_id, **fields)
        if isinstance(job.trigger, CronTrigger) and not explicit_next:
            now = datetime.now(self.scheduler.timezone)
            missed = job.trigger.get_next_fire_time(None, now - timedelta(seconds=300))
            if missed is not None and missed <= now:
                self.scheduler.modify_job(job_id, next_run_time=now)
        return job_id

    def add_interval(self, plugin_id: str, name: str, callback: Callable[..., Any],
                     *, seconds: int, replace_existing: bool = True) -> str:
        job_id = f"{plugin_id}::{name}"
        if not replace_existing or self.scheduler.get_job(job_id) is None:
            self._check_limit(plugin_id)
        self.scheduler.add_job(
            self._tracked(job_id, callback),
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
        if not any(fields.get(key) is not None for key in
                   ("year", "month", "day", "week", "day_of_week", "hour", "minute", "second")):
            raise ValueError("Cron 必须提供至少一个有效时间字段")
        job_id = f"{plugin_id}::{name}"
        if not replace_existing or self.scheduler.get_job(job_id) is None:
            self._check_limit(plugin_id)
        self.scheduler.add_job(
            self._tracked(job_id, callback),
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
        for job_id, task in [*self._manual.items(), *self._running.items()]:
            if job_id.startswith(prefix) and task is not asyncio.current_task():
                task.cancel()

    def jobs(self) -> list[dict[str, object]]:
        return [
            {
                "id": job.id,
                **self.snapshot(job.id),
                "next_run": job.next_run_time.isoformat() if getattr(job, "next_run_time", None) else None,
                "trigger": str(job.trigger),
            }
            for job in self.scheduler.get_jobs()
        ]

    def stop(self) -> None:
        for task in {*self._manual.values(), *self._running.values()}:
            task.cancel()
        if self.scheduler.running and not self._stopping:
            self._stopping = True
            self.scheduler.shutdown(wait=False)

    async def close(self):
        tasks = {*self._manual.values(), *self._running.values()} - {asyncio.current_task()}
        self.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
