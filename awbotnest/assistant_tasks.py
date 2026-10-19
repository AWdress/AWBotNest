"""Persistent date/Cron reservations. No sleeping coroutine per reservation."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from .assistant import ACTIVE, ident, now

ZONE = ZoneInfo("Asia/Shanghai")


def cron_trigger(expression):
    """The existing editor uses crontab weekdays (Sun=0/7), not APS Mon=0."""
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("请使用五段 Cron")
    names = {name: index for index, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
    weekdays = set()
    def number(value):
        result = names.get(value.lower()) if not value.isdecimal() else int(value)
        if result is None or not 0 <= result <= 7:
            raise ValueError("星期格式不正确")
        return result
    for entry in parts[4].split(","):
        base, separator, step_text = entry.partition("/")
        if separator and (not re.fullmatch(r"[1-7]", step_text)):
            raise ValueError("星期步长不正确")
        step = int(step_text) if separator else 1
        if base == "*":
            low, high = 0, 6
        elif "-" in base:
            start, end = base.split("-", 1)
            low, high = number(start), number(end)
        else:
            low = high = number(base)
            if separator:
                high = 7
        if low > high:
            raise ValueError("星期范围不正确")
        weekdays.update(value % 7 for value in range(low, high + 1, step))
    parts[4] = ",".join(list(names)[day] for day in sorted(weekdays))
    return CronTrigger.from_crontab(" ".join(parts), timezone=ZONE)


def validate_task(values):
    if not isinstance(values, dict) or values.keys() - {"name", "prompt", "run_at", "cron"}:
        raise ValueError("预约参数不正确")
    result = {}
    for key, maximum in (("name", 80), ("prompt", 2000)):
        value = values.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError("请填写预约名称和执行要求")
        result[key] = value.strip()
    run_at, cron = values.get("run_at"), values.get("cron")
    if bool(run_at) == bool(cron):
        raise ValueError("执行时间和 Cron 只能设置一个")
    if cron:
        if not isinstance(cron, str) or len(cron) > 100:
            raise ValueError("Cron 格式不正确")
        try:
            trigger = cron_trigger(cron)
            current = datetime.now(ZONE)
            first = trigger.get_next_fire_time(None, current)
            if first is None:
                raise ValueError("预约间隔不能少于 5 分钟")
            previous = first
            for _ in range(128):
                following = trigger.get_next_fire_time(previous, previous)
                if following is None:
                    break
                if (following - previous).total_seconds() < 300:
                    raise ValueError("预约间隔不能少于 5 分钟")
                previous = following
        except (ValueError, TypeError) as exc:
            raise ValueError("Cron 格式不正确，且间隔不能少于 5 分钟") from exc
        result.update(cron=cron, run_at=None, next_run=first.astimezone(timezone.utc).isoformat())
    else:
        try:
            date = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=ZONE)
            date = date.astimezone(timezone.utc)
            if date <= datetime.now(timezone.utc):
                raise ValueError("执行时间已过")
        except (ValueError, TypeError) as exc:
            raise ValueError("请选择未来的执行时间（时区 Asia/Shanghai）") from exc
        result.update(cron=None, run_at=date.isoformat(), next_run=date.isoformat())
    return result


async def save_task(service, values):
    task = validate_task(values)
    if len(await service.store.list("task", 32)) >= 32:
        raise ValueError("最多保存 32 个预约，请先整理旧任务")
    task = {**task, "id": ident(), "enabled": True, "created_at": now(),
            "last_run_id": None, "last_session_id": None, "last_status": "未执行"}
    task["name"] = service.safe_content(task["name"], 80)
    task["prompt"] = service.safe_content(task["prompt"], 2000)
    await service.store.put("task", task)
    return task


async def toggle_task(service, task_id, enabled):
    if type(enabled) is not bool:
        raise ValueError("开关格式不正确")
    task = await service.store.get("task", task_id)
    if task is None:
        raise KeyError("预约不存在")
    if enabled and not task.get("cron") and datetime.fromisoformat(task["run_at"]) <= datetime.now(timezone.utc):
        raise ValueError("该预约的执行时间已过，请新建预约")
    task["enabled"] = enabled
    task["revision"] = task.get("revision", 0) + 1
    if enabled and task.get("cron"):
        task["next_run"] = cron_trigger(task["cron"]).get_next_fire_time(
            None, datetime.now(ZONE)).astimezone(timezone.utc).isoformat()
    await service.store.put("task", task)
    return task


def clear_admission(task):
    for key in ("admission_pending", "admission_id", "admission_revision", "due_before_claim"):
        task.pop(key, None)


def restore_admission(task, *, restart=False):
    if task.get("admission_revision", 0) == task.get("revision", 0):
        task.update(enabled=True, next_run=task["due_before_claim"],
                    last_status="等待执行" if restart else "等待可用的 AI 服务")
    # Concurrent administrator changes keep their current enabled/due state.
    if restart:
        task.pop("claimed_at", None)
    clear_admission(task)


async def release_admission(service, task_id, admission_id, *, restart=False):
    """Restore only claims that never atomically acquired a run."""
    async with service._lock:
        task = await service.store.get("task", task_id)
        if not task or not task.get("admission_pending") or task.get("admission_id") != admission_id:
            return False
        restore_admission(task, restart=restart)
        await service.store.put("task", task)
        return True


async def recover_admissions(service):
    # Called only under the startup lock, before a ticker or runner can exist.
    # Do not acquire the admission lock: lazy API startup may already hold it.
    for task in await service.store.list("task", 32):
        if task.get("admission_pending"):
            restore_admission(task, restart=True)
            await service.store.put("task", task)


async def discard_empty_session(service, session):
    if session is None:
        return
    async with service._lock:
        current = await service.store.get("session", session["id"])
        if current and not current.get("run_id"):
            await service.store.delete_session(session["id"])


async def tick(service):
    for task in await service.store.list("task", 32):
        if task.get("last_run_id"):
            try:
                run = await service.run(task["last_run_id"])
            except KeyError:
                run = {"status": "interrupted"}
            if task.get("last_status") != run["status"]:
                async with service._lock:
                    current = await service.store.get("task", task["id"])
                    if current is None:
                        continue
                    if current.get("last_run_id") == task["last_run_id"]:
                        current["last_status"] = run["status"]
                        await service.store.put("task", current)
                    task = current
            if run["status"] in ACTIVE:
                continue  # Keep due time: perform one coalesced catch-up after completion.
        if not task.get("enabled") or datetime.fromisoformat(task["next_run"]) > datetime.now(timezone.utc):
            continue
        if task.get("claimed_at") and (datetime.now(timezone.utc) - datetime.fromisoformat(task["claimed_at"])).total_seconds() < 300:
            continue  # Runtime guard as well as validation; no high-frequency catch-up burst.
        if len(service._workers) >= 8:
            continue  # Do not advance or discard a task just because admission is full.
        async with service._lock:
            current = await service.store.get("task", task["id"])
            if (current is None or not current.get("enabled") or current.get("admission_pending")
                    or current["next_run"] != task["next_run"]):
                continue
            task = current
            # Keep the original due time until submit atomically binds a run.
            task.update(last_status="等待执行", claimed_at=now(), admission_pending=True,
                        admission_id=ident(), admission_revision=task.get("revision", 0),
                        due_before_claim=task["next_run"])
            if task.get("cron"):
                trigger = cron_trigger(task["cron"])
                next_date = trigger.get_next_fire_time(None, datetime.now(ZONE))
                task["next_run"] = next_date.astimezone(timezone.utc).isoformat()
            else:
                task["enabled"] = False
            await service.store.put("task", task)
        session = None
        try:
            session = await service.new_session(task["name"])
            await service.submit(session["id"], task["prompt"], task_id=task["id"],
                                 task_admission_id=task["admission_id"])
        except asyncio.CancelledError:
            await release_admission(service, task["id"], task["admission_id"], restart=True)
            await discard_empty_session(service, session)
            raise
        except (ValueError, RuntimeError, KeyError):
            # No model/tool ran. Preserve the due time for a safe admission retry.
            await release_admission(service, task["id"], task["admission_id"])
            await discard_empty_session(service, session)
            continue
        except Exception:
            await release_admission(service, task["id"], task["admission_id"])
            await discard_empty_session(service, session)
            raise
