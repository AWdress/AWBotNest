"""系统托管的插件调度器。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
import asyncio
import inspect
import time
import logging
import contextvars
import weakref
from datetime import datetime, timedelta
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.base import MaxInstancesReachedError
from apscheduler.events import EVENT_JOB_MISSED

from apscheduler.schedulers.asyncio import AsyncIOScheduler

_current_state = contextvars.ContextVar("scheduler_state", default=None)
TASK_DRAIN_TIMEOUT_SECONDS = 10.0
logger = logging.getLogger("awbotnest.scheduler")


@contextmanager
def waiting_phase(step: str):
    """Expose admission/resource wait without releasing the job's execution slot."""
    state = _current_state.get()
    if state is None or state.get("status") not in {"pending", "running"}:
        yield
        return
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task_ref = state.get("_task_ref")
        task = task_ref() if task_ref is not None else None
    marker = object()
    phases = state.setdefault("_waiting_phases", {})
    if not phases:
        state["_waiting_previous"] = (state.get("status"), state.get("step"))
        state["_waiting_started"] = time.monotonic()
    phases[marker] = str(step)
    state.update(status="pending", step=str(step))
    try:
        yield
    finally:
        phases.pop(marker, None)
        if phases:
            if state.get("status") == "pending":
                state["step"] = next(reversed(phases.values()))
        else:
            state.pop("_waiting_phases", None)
            started = state.pop("_waiting_started", None)
            previous = state.pop("_waiting_previous", ("running", "执行中"))
            if started is not None:
                state["waiting_seconds"] = state.get("waiting_seconds", 0.0) + time.monotonic() - started
            scheduler_ref = state.get("_scheduler_ref")
            scheduler = scheduler_ref() if scheduler_ref is not None else None
            same_state = scheduler_ref is None or (scheduler is not None
                and scheduler._states.get(state.get("_job_id")) is state)
            if (_current_state.get() is state and same_state and state.get("status") == "pending"
                    and task is not None and not task.done()):
                state.update(status=previous[0], step=previous[1])


class _PluginExecutor(AsyncIOExecutor):
    """Keep manual and scheduled invocations in the same execution slot."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def start(self, scheduler, alias):
        super().start(scheduler, alias)
        self._instances.clear()

    def submit_job(self, job, run_times):
        try:
            if self.owner._live_tasks(job.id):
                raise MaxInstancesReachedError(job)
            super().submit_job(job, run_times)
        except MaxInstancesReachedError:
            self.owner._defer(job, run_times)
            raise

    def _do_submit_job(self, job, run_times):
        previous = self._pending_futures.copy()
        super()._do_submit_job(job, run_times)
        generation = self.owner._generations.get(job.id)
        for task in self._pending_futures - previous:
            self.owner._aps_tasks.setdefault(job.id, {})[task] = generation

            def finished(done, job_id=job.id):
                if not done.cancelled():
                    try:
                        events = done.result()
                    except BaseException:
                        pass
                    else:
                        for event in events:
                            if event.code == EVENT_JOB_MISSED:
                                self.owner._missed(event, generation)
                tasks = self.owner._aps_tasks.get(job_id, {})
                tasks.pop(done, None)
                if not tasks:
                    self.owner._aps_tasks.pop(job_id, None)
                self.owner._finish_stop()

            # APS's earlier callback releases its instance count first.
            task.add_done_callback(finished)


class PluginScheduler:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai", job_defaults={
            "misfire_grace_time": 300, "coalesce": True, "max_instances": 1,
        }, executors={"default": _PluginExecutor(self)})
        self._states = {}
        self._manual = {}
        self._running = {}
        self._generations = {}
        self._manual_generations = {}
        self._running_generations = {}
        self._aps_tasks = {}
        self._deferred = {}
        self._draining: set[asyncio.Task] = set()
        self._stopping = False
        self._shutdown_scheduled = False

    def _live_tasks(self, job_id, generation=None, *, exclude=None):
        generation = self._generations.get(job_id) if generation is None else generation
        tasks = set()
        for mapping, generations in ((self._manual, self._manual_generations),
                                     (self._running, self._running_generations)):
            task = mapping.get(job_id)
            if task is not None and not task.done() and generations.get(job_id) is generation:
                tasks.add(task)
        tasks.update(task for task, token in self._aps_tasks.get(job_id, {}).items()
                     if not task.done() and token is generation)
        return tasks - {exclude}

    def _execution_blockers(self, job_id, generation):
        # APS counts by job id, including an older generation still draining.
        # Do not discard a new trigger merely because its own generation is idle.
        return self._live_tasks(job_id, generation) | {
            task for task in self._aps_tasks.get(job_id, {}) if not task.done()
        }

    def _trigger_state(self, job_id, status, reason, scheduled):
        state = self._states.setdefault(job_id, {})
        state.update(last_trigger_status=status, last_trigger_reason=reason,
                     scheduled_run=scheduled.isoformat())
        if not self._live_tasks(job_id):
            state.update(status="pending" if status == "pending" else "skipped", step=reason)

    def _missed(self, event, generation):
        # An old generation's completion must not overwrite its replacement.
        if generation is not self._generations.get(event.job_id):
            return
        reason = "超过允许延迟时间，本次未执行"
        self._trigger_state(event.job_id, "skipped", reason, event.scheduled_run_time)
        logger.warning("定时任务跳过：%s，%s", event.job_id.split("::", 1)[-1], reason)

    def _defer(self, job, run_times):
        if self._stopping or not run_times:
            return
        scheduled = max(run_times)
        record = self._deferred.get(job.id)
        generation = self._generations.get(job.id)
        if record is not None and record["generation"] is generation and record["job"] is job:
            if scheduled > record["scheduled"]:
                record["scheduled"] = scheduled
                self._trigger_state(job.id, "pending", "上次执行尚未结束，等待补跑", scheduled)
            return
        if record is not None:
            self._retain_draining(record["worker"])
            record["worker"].cancel()
        reason = "上次执行尚未结束，等待补跑"
        record = {"job": job, "generation": generation, "scheduled": scheduled}
        self._deferred[job.id] = record
        self._trigger_state(job.id, "pending", reason, scheduled)
        logger.warning("定时任务延期：%s，上次执行尚未结束，结束后补跑一次", job.id.split("::", 1)[-1])
        task = asyncio.create_task(self._resume_deferred(job.id, record))
        record["worker"] = task

        def finished(done):
            if self._deferred.get(job.id) is record:
                self._deferred.pop(job.id, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    async def _resume_deferred(self, job_id, record):
        job, generation = record["job"], record["generation"]
        while not self._stopping and self._deferred.get(job_id) is record:
            current_job = self.scheduler.get_job(job_id)
            if (self._generations.get(job_id) is not generation
                    or (current_job is not job and not (current_job is None and isinstance(job.trigger, DateTrigger)))):
                return
            scheduled = record["scheduled"]
            grace = job.misfire_grace_time
            remaining = None if grace is None else grace - (datetime.now(scheduled.tzinfo) - scheduled).total_seconds()
            if remaining is not None and remaining <= 0:
                reason = "等待上次执行结束时超过允许延迟时间，本次未执行"
                self._trigger_state(job_id, "skipped", reason, scheduled)
                logger.warning("定时任务跳过：%s，%s", job_id.split("::", 1)[-1], reason)
                return
            tasks = self._execution_blockers(job_id, generation)
            if tasks:
                await asyncio.wait(tasks, timeout=remaining)
                continue
            # Let APS's completion callbacks release their instance counters.
            await asyncio.sleep(0)
            if self._execution_blockers(job_id, generation):
                continue
            if self._stopping or self._deferred.get(job_id) is not record:
                return
            if self._generations.get(job_id) is not generation:
                return
            scheduled = record["scheduled"]
            try:
                self.scheduler._lookup_executor(job.executor).submit_job(job, [scheduled])
            except MaxInstancesReachedError:
                # A counter without any live owner must not spin or self-enqueue.
                reason = "执行名额尚未释放，本次未执行"
                self._trigger_state(job_id, "skipped", reason, scheduled)
                logger.warning("定时任务跳过：%s，%s", job_id.split("::", 1)[-1], reason)
                return
            self._deferred.pop(job_id, None)
            self._states.setdefault(job_id, {}).update(last_trigger_status="resumed",
                last_trigger_reason="等待结束，补跑一次", scheduled_run=scheduled.isoformat())
            logger.info("定时任务补跑：%s", job_id.split("::", 1)[-1])
            return

    def _tracked(self, job_id, callback):
        generation = object()
        async def execute(*args, **kwargs):
            if self._generations.get(job_id) is not generation:
                raise LookupError("定时任务已停用或重新注册，本次未执行")
            task = asyncio.current_task()
            if self._live_tasks(job_id, generation, exclude=task):
                raise ValueError("任务正在执行，本次未执行")
            state = {"status": "running", "step": "执行中", "started": time.monotonic()}
            previous = self._states.get(job_id, {})
            state.update({key: value for key, value in previous.items()
                          if key.startswith("last_trigger_") or key == "scheduled_run"})
            state.update(_scheduler_ref=weakref.ref(self), _job_id=job_id, _task_ref=weakref.ref(task))
            self._states[job_id] = state
            self._running[job_id] = task
            self._running_generations[job_id] = generation
            token = _current_state.set(state)
            try:
                result = callback(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                failed = isinstance(result, dict) and result.get("ok") is False
                state.update(status="failed" if failed else "success",
                             step="执行失败，请查看运行日志" if failed else "执行完成")
                return result
            except asyncio.CancelledError:
                state.update(status="cancelled", step="执行已取消")
                raise
            except Exception as exc:
                if getattr(exc, "not_started", False):
                    state.update(status="skipped", step="等待超时，本次未执行" if isinstance(exc, TimeoutError)
                                 else "插件执行名额已占用，本次未执行")
                else:
                    state.update(status="failed", step="执行失败，请查看运行日志")
                    logger.exception("定时任务执行失败：%s", job_id.split("::", 1)[-1])
            except BaseException:
                state.update(status="failed", step="执行异常中止，请查看运行日志")
                raise
            finally:
                _current_state.reset(token)
                if self._running.get(job_id) is task:
                    self._running.pop(job_id, None)
                    self._running_generations.pop(job_id, None)
                state["duration_seconds"] = int(time.monotonic() - state["started"])
        execute._generation = generation
        return execute

    def _registered(self, job):
        self._generations[job.id] = job.func._generation
        record = self._deferred.pop(job.id, None)
        if record is not None:
            self._retain_draining(record["worker"])
            record["worker"].cancel()
        return job

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
        self._ensure_open()
        job = self.scheduler.get_job(job_id)
        if job is None:
            raise LookupError("定时任务不存在")
        if self._live_tasks(job_id) or job_id in self._deferred:
            raise ValueError("任务正在执行，请勿重复提交")
        self._states[job_id] = {"status": "pending", "step": "等待执行", "started": time.monotonic()}
        task = asyncio.create_task(job.func(*job.args, **job.kwargs))
        self._manual[job_id] = task
        self._manual_generations[job_id] = self._generations.get(job_id)
        def finished(done):
            if self._manual.get(job_id) is done:
                self._manual.pop(job_id, None)
                self._manual_generations.pop(job_id, None)
            if not done.cancelled():
                done.exception()
            self._finish_stop()
        task.add_done_callback(finished)

    def snapshot(self, job_id):
        raw = self._states.get(job_id, {})
        state = {key: value for key, value in raw.items() if not key.startswith("_")}
        started = state.pop("started", None)
        live = bool(self._live_tasks(job_id))
        deferred = job_id in self._deferred
        if state.get("status") in {"pending", "running"} and started is not None:
            state["duration_seconds"] = int(time.monotonic() - started)
        if raw.get("_waiting_started") is not None:
            state["waiting_seconds"] = state.get("waiting_seconds", 0.0) + time.monotonic() - raw["_waiting_started"]
        if deferred:
            state["pending_run"] = self._deferred[job_id]["scheduled"].isoformat()
            if not live:
                state.update(status="pending", step="等待补跑")
        elif not live and state.get("status") in {"pending", "running"}:
            state.update(status="skipped", step="执行已结束，本次未完成")
        return {"running": live or deferred, "progress": state}

    def start(self) -> None:
        if not self.scheduler.running:
            self._stopping = False
            self._shutdown_scheduled = False
            self.scheduler.start()

    def _ensure_open(self) -> None:
        if self._stopping:
            raise RuntimeError("系统正在停止，无法注册定时任务")

    def _check_limit(self, plugin_id: str) -> None:
        self._ensure_open()
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
            from .workers import run_sync
            value = await run_sync(callback, *args, **kwargs)
            return await value if inspect.isawaitable(value) else value
        job = self._registered(self.scheduler.add_job(self._tracked(job_id, invoke), trigger, id=job_id, **fields))
        self._catch_up_cron(job, explicit_next, fields.get("misfire_grace_time", 300))
        return job_id

    def _catch_up_cron(self, job, explicit_next, grace):
        if isinstance(job.trigger, CronTrigger) and not explicit_next:
            now = datetime.now(job.trigger.timezone)
            window = 300 if grace is None else min(300, grace)
            missed = job.trigger.get_next_fire_time(None, now - timedelta(seconds=window))
            if missed is not None and missed <= now:
                self.scheduler.modify_job(job.id, next_run_time=now)

    def add_interval(self, plugin_id: str, name: str, callback: Callable[..., Any],
                     *, seconds: int, replace_existing: bool = True) -> str:
        self._ensure_open()
        job_id = f"{plugin_id}::{name}"
        if not replace_existing or self.scheduler.get_job(job_id) is None:
            self._check_limit(plugin_id)
        self._registered(self.scheduler.add_job(
            self._tracked(job_id, callback),
            "interval",
            seconds=max(1, int(seconds)),
            id=job_id,
            replace_existing=replace_existing,
            coalesce=True,
            max_instances=1,
        ))
        return job_id

    def add_cron(self, plugin_id: str, name: str, callback: Callable[..., Any],
                 *, replace_existing: bool = True, **fields: Any) -> str:
        self._ensure_open()
        if not any(fields.get(key) is not None for key in
                   ("year", "month", "day", "week", "day_of_week", "hour", "minute", "second")):
            raise ValueError("Cron 必须提供至少一个有效时间字段")
        job_id = f"{plugin_id}::{name}"
        if not replace_existing or self.scheduler.get_job(job_id) is None:
            self._check_limit(plugin_id)
        explicit_next = "next_run_time" in fields
        options = {"coalesce": True, "max_instances": 1, **fields}
        job = self._registered(self.scheduler.add_job(
            self._tracked(job_id, callback),
            "cron",
            id=job_id,
            replace_existing=replace_existing,
            **options,
        ))
        self._catch_up_cron(job, explicit_next, fields.get("misfire_grace_time", 300))
        return job_id

    def _retain_draining(self, task: asyncio.Task) -> None:
        if task.done() or task in self._draining:
            return
        self._draining.add(task)
        task.add_done_callback(self._draining.discard)
        task.add_done_callback(self._finish_stop)

    def _finish_stop(self, _task=None) -> None:
        tasks = self._all_tasks()
        if (self._stopping and self.scheduler.running and not self._shutdown_scheduled
                and not any(not task.done() for task in tasks)):
            self._shutdown_scheduled = True
            self.scheduler.shutdown(wait=False)

    def remove_plugin(self, plugin_id: str, *, exclude: set[asyncio.Task] | None = None) -> None:
        prefix = f"{plugin_id}::"
        for job_id in [key for key in self._generations if key.startswith(prefix)]:
            self._generations.pop(job_id, None)
            record = self._deferred.pop(job_id, None)
            if record is not None:
                self._retain_draining(record["worker"])
                record["worker"].cancel()
        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                self.scheduler.remove_job(job.id)
        excluded = (exclude or set()) | {asyncio.current_task()}
        aps = [(job_id, task) for job_id, tasks in self._aps_tasks.items() for task in tasks]
        for job_id, task in [*self._manual.items(), *self._running.items(), *aps]:
            if job_id.startswith(prefix):
                self._retain_draining(task)
                if task not in excluded and not task.cancelling():
                    task.cancel()
        for job_id in [key for key in self._states if key.startswith(prefix)]:
            self._states.pop(job_id, None)

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
        already_stopping = self._stopping
        self._stopping = True
        if not already_stopping:
            if self.scheduler.running:
                self.scheduler.pause()
            self.scheduler.remove_all_jobs()
            self._states.clear()
            self._generations.clear()
            for record in self._deferred.values():
                self._retain_draining(record["worker"])
                record["worker"].cancel()
            self._deferred.clear()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in self._all_tasks():
            self._retain_draining(task)
            if not already_stopping and task is not current and not task.cancelling():
                task.cancel()
        # APScheduler's executor cancels every unfinished future on shutdown.
        # Delay that second cancellation until our tasks really finish, so an
        # in-flight storage transaction can retain its lock while draining.
        self._finish_stop()

    async def close(self):
        tasks = self._all_tasks() - {asyncio.current_task()}
        self.stop()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=TASK_DRAIN_TIMEOUT_SECONDS)
            if pending:
                logger.warning("仍有 %d 个定时任务未退出，继续清理其他资源", len(pending))

    def _all_tasks(self):
        return {*self._manual.values(), *self._running.values(), *self._draining,
                *(task for tasks in self._aps_tasks.values() for task in tasks),
                *(record["worker"] for record in self._deferred.values())}
