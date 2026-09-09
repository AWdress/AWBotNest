"""Opt-in, in-memory profiling for latency-sensitive Telegram handlers."""
from __future__ import annotations

import contextvars
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


_current_trace: contextvars.ContextVar["InteractiveTrace | None"] = contextvars.ContextVar(
    "awbotnest_interactive_trace", default=None,
)


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _masked_chat(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "unknown"
    return f"***{text[-4:]}" if len(text) > 4 else "***"


@dataclass(slots=True)
class SessionSpan:
    key: str
    wait_started_ns: int
    acquired_ns: int | None = None
    released_ns: int | None = None


@dataclass(slots=True)
class RpcSpan:
    operation: str
    started_ns: int
    returned_ns: int | None = None
    failed: bool = False


@dataclass(slots=True)
class InteractiveTrace:
    plugin: str
    instance: str
    handler: str
    chat: str
    wrapper_entered_ns: int
    callback_entered_ns: int | None = None
    callback_exited_ns: int | None = None
    sessions: list[SessionSpan] = field(default_factory=list)
    rpcs: list[RpcSpan] = field(default_factory=list)


class ProfiledLock:
    """asyncio.Lock-compatible wrapper which records wait/hold spans when a trace exists."""

    def __init__(self, profiler: "InteractiveProfiler", key: str) -> None:
        self._lock = __import__("asyncio").Lock()
        self._profiler = profiler
        self._key = key

    async def acquire(self) -> bool:
        span = self._profiler.session_wait_started(self._key)
        acquired = await self._lock.acquire()
        self._profiler.session_acquired(span)
        return acquired

    def release(self) -> None:
        self._lock.release()
        self._profiler.session_released(self._key)

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> None:
        await self.acquire()
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class InteractiveProfiler:
    """Collect bounded traces only when AWBOTNEST_INTERACTIVE_PROFILE is enabled."""

    def __init__(self, plugin: str, instance: str, logger: logging.LoggerAdapter | logging.Logger,
                 *, enabled: bool | None = None, slow_ms: float | None = None) -> None:
        self.plugin = plugin
        self.instance = instance
        self.log = logger
        self.enabled = _enabled(os.getenv("AWBOTNEST_INTERACTIVE_PROFILE")) if enabled is None else enabled
        if slow_ms is None:
            try:
                slow_ms = float(os.getenv("AWBOTNEST_INTERACTIVE_PROFILE_SLOW_MS", "100"))
            except ValueError:
                slow_ms = 100.0
        self.slow_ms = max(0.0, float(slow_ms))
        self._recent: deque[dict[str, Any]] = deque(maxlen=200)

    @staticmethod
    def timestamp() -> int:
        return time.perf_counter_ns()

    def start(self, handler: str, chat: Any, *, wrapper_entered_ns: int | None = None
              ) -> contextvars.Token[InteractiveTrace | None] | None:
        if not self.enabled:
            return None
        trace = InteractiveTrace(
            self.plugin, self.instance, handler, _masked_chat(chat),
            wrapper_entered_ns or time.perf_counter_ns(),
        )
        return _current_trace.set(trace)

    @staticmethod
    def callback_entered() -> None:
        trace = _current_trace.get()
        if trace is not None:
            trace.callback_entered_ns = time.perf_counter_ns()

    @staticmethod
    def callback_exited() -> None:
        trace = _current_trace.get()
        if trace is not None:
            trace.callback_exited_ns = time.perf_counter_ns()

    @staticmethod
    def session_wait_started(key: str) -> SessionSpan | None:
        trace = _current_trace.get()
        if trace is None:
            return None
        span = SessionSpan(str(key)[:128], time.perf_counter_ns())
        trace.sessions.append(span)
        return span

    @staticmethod
    def session_acquired(span: SessionSpan | None) -> None:
        if span is not None:
            span.acquired_ns = time.perf_counter_ns()

    @staticmethod
    def session_released(key: str) -> None:
        trace = _current_trace.get()
        if trace is None:
            return
        for span in reversed(trace.sessions):
            if span.key == str(key)[:128] and span.acquired_ns is not None and span.released_ns is None:
                span.released_ns = time.perf_counter_ns()
                return

    @staticmethod
    def rpc_started(operation: str) -> RpcSpan | None:
        trace = _current_trace.get()
        if trace is None:
            return None
        span = RpcSpan(str(operation)[:80], time.perf_counter_ns())
        trace.rpcs.append(span)
        return span

    @staticmethod
    def rpc_returned(span: RpcSpan | None, *, failed: bool = False) -> None:
        if span is not None:
            span.returned_ns = time.perf_counter_ns()
            span.failed = failed

    def finish(self, token: contextvars.Token[InteractiveTrace | None] | None) -> dict[str, Any] | None:
        if token is None:
            return None
        trace = _current_trace.get()
        try:
            if trace is None:
                return None
            now = trace.callback_exited_ns or time.perf_counter_ns()
            entered = trace.callback_entered_ns or trace.wrapper_entered_ns
            sessions = [{
                "key": span.key,
                "from_callback_ms": self._ms(entered, span.acquired_ns),
                "wait_ms": self._ms(span.wait_started_ns, span.acquired_ns),
                "hold_ms": self._ms(span.acquired_ns, span.released_ns),
            } for span in trace.sessions]
            rpcs = []
            for span in trace.rpcs:
                releases = [
                    session.released_ns for session in trace.sessions
                    if session.released_ns is not None and session.released_ns <= span.started_ns
                ]
                pre_send_start = max(releases) if releases else (entered if not trace.sessions else None)
                rpcs.append({
                    "operation": span.operation,
                    "pre_send_ms": self._ms(pre_send_start, span.started_ns),
                    "telegram_rpc_ms": self._ms(span.started_ns, span.returned_ns),
                    "failed": span.failed,
                })
            result = {
                "plugin": trace.plugin,
                "instance": trace.instance,
                "handler": trace.handler,
                "chat": trace.chat,
                "dispatch_ms": self._ms(trace.wrapper_entered_ns, entered),
                "callback_total_ms": self._ms(entered, now),
                "session_wait_ms": self._ms(
                    entered,
                    next((span.acquired_ns for span in trace.sessions
                          if span.acquired_ns is not None), None),
                ),
                "session_hold_ms": self._ms(
                    next((span.acquired_ns for span in trace.sessions
                          if span.acquired_ns is not None), None),
                    next((span.released_ns for span in trace.sessions
                          if span.released_ns is not None), None),
                ),
                "total_local_to_rpc_ms": self._ms(
                    trace.wrapper_entered_ns,
                    max((span.returned_ns for span in trace.rpcs if span.returned_ns is not None), default=now),
                ),
                "sessions": sessions,
                "rpcs": rpcs,
            }
            self._recent.append(result)
            total = float(result["callback_total_ms"] or 0.0)
            if total >= self.slow_ms:
                self.log.warning(
                    "Interactive 慢事件：handler=%s chat=%s dispatch=%.3fms callback=%.3fms "
                    "session_wait=%.3fms telegram_rpc=%.3fms",
                    trace.handler, trace.chat, float(result["dispatch_ms"] or 0.0), total,
                    sum(float(item["wait_ms"] or 0.0) for item in sessions),
                    sum(float(item["telegram_rpc_ms"] or 0.0) for item in rpcs),
                )
            return result
        finally:
            _current_trace.reset(token)

    @staticmethod
    def _ms(start: int | None, end: int | None) -> float | None:
        if start is None or end is None:
            return None
        return round((end - start) / 1_000_000, 6)

    def lock(self, key: str):
        return ProfiledLock(self, key) if self.enabled else __import__("asyncio").Lock()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._recent)[-max(1, min(int(limit), 200)):]
