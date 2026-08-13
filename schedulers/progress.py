"""定时任务运行状态与插件进度上报。"""
from __future__ import annotations

import contextvars
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_current_run: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "awbotnest_current_job_run", default=None,
)
_states: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()
_MAX_STATES = 1000


@dataclass(frozen=True)
class RunHandle:
    token: contextvars.Token
    run_id: str


def start(job_id: str) -> RunHandle:
    now = datetime.now().astimezone()
    run_id = uuid.uuid4().hex
    with _lock:
        if job_id not in _states and len(_states) >= _MAX_STATES:
            completed = (
                (key, state) for key, state in _states.items()
                if state.get("status") != "running"
            )
            oldest = min(completed, key=lambda item: item[1].get("finished_at") or "", default=None)
            if oldest is not None:
                _states.pop(oldest[0], None)
        _states[job_id] = {
            "_run_id": run_id,
            "status": "running",
            "progress": 0,
            "step": "任务已开始",
            "started_at": now.isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "error": None,
            "_started": time.perf_counter(),
        }
    return RunHandle(_current_run.set((job_id, run_id)), run_id)


def report(percent: float | int | None = None, step: str | None = None) -> bool:
    current = _current_run.get()
    if not current:
        return False
    job_id, run_id = current
    with _lock:
        state = _states.get(job_id)
        if not state or state.get("_run_id") != run_id or state.get("status") != "running":
            return False
        if percent is not None:
            try:
                value = float(percent)
            except (TypeError, ValueError):
                value = math.nan
            if math.isfinite(value):
                state["progress"] = max(0, min(100, round(value, 1)))
        if step is not None:
            state["step"] = str(step).strip()[:200]
        return True


def finish(job_id: str, handle: RunHandle, error: BaseException | None = None) -> None:
    try:
        with _lock:
            state = _states.get(job_id)
            if not state or state.get("_run_id") != handle.run_id:
                return
            started = float(state.pop("_started", time.perf_counter()))
            state.update({
                "status": "failed" if error else "completed",
                "progress": state.get("progress", 0) if error else 100,
                "step": "执行失败" if error else "执行完成",
                "finished_at": datetime.now().astimezone().isoformat(),
                "duration_seconds": round(max(0, time.perf_counter() - started), 2),
                "error": f"{error.__class__.__name__}: {error}"[:300] if error else None,
            })
    finally:
        _current_run.reset(handle.token)


def snapshot(job_id: str) -> dict[str, Any]:
    with _lock:
        state = dict(_states.get(job_id) or {})
    state.pop("_run_id", None)
    started = state.pop("_started", None)
    if state.get("status") == "running" and started is not None:
        state["duration_seconds"] = round(max(0, time.perf_counter() - float(started)), 2)
    return state


def remove(job_id: str) -> None:
    with _lock:
        _states.pop(job_id, None)
