"""插件运行治理：统一执行、熔断、能力链、事件记录和任务取消。"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Any, Awaitable, Callable

import logging
from telethon.events import StopPropagation
from .config import DATA_DIR
from .scheduler import waiting_phase
logger = logging.getLogger("awbotnest.governance")


EVENT_FILE = DATA_DIR / "plugin_events.jsonl"
MAX_EVENT_FILE_BYTES = 8 * 1024 * 1024
MAX_MEMORY_EVENTS = 1000
# A UI restart rebuilds the services in the same process. Keep unfinished
# shutdown work visible to the replacement runtime, not only its old governor.
_DRAINING_TASKS: dict[str, set[asyncio.Task]] = {}


@dataclass(frozen=True)
class ResourcePolicy:
    timeout_seconds: float = 120.0
    max_concurrency: int = 8
    max_background_tasks: int = 32
    failure_threshold: int = 5
    recovery_seconds: float = 60.0
    max_pending_tasks: int = 64

    @classmethod
    def from_mapping(cls, value: Any) -> "ResourcePolicy":
        raw = value if isinstance(value, dict) else {}

        def number(name: str, default: float, minimum: float, maximum: float) -> float:
            try:
                value = float(raw.get(name, default))
                if not math.isfinite(value):
                    return default
                return min(max(value, minimum), maximum)
            except (TypeError, ValueError):
                return default

        return cls(
            timeout_seconds=number("timeout_seconds", 120, 1, 1800),
            max_concurrency=int(number("max_concurrency", 8, 1, 100)),
            max_background_tasks=int(number("max_background_tasks", 32, 1, 500)),
            failure_threshold=int(number("failure_threshold", 5, 1, 100)),
            recovery_seconds=number("recovery_seconds", 60, 1, 3600),
            max_pending_tasks=int(number("max_pending_tasks", 64, 1, 1000)),
        )


@dataclass
class CircuitState:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""

    @property
    def open(self) -> bool:
        return self.opened_until > time.monotonic()


class PluginBusyError(RuntimeError):
    """Admission was rejected; this is not a failure of the plugin callback."""
    not_started = True


class PluginQueueTimeout(TimeoutError):
    """The callback never started before its deadline."""
    not_started = True


def _safe_text(value: str) -> str:
    text = value[:1000]
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1***", text)
    text = re.sub(
        r"(?i)((?:token|secret|password|passwd|api[_-]?key)\s*[:=]\s*)[^\s,;&]+",
        r"\1***",
        text,
    )
    return re.sub(r"(://)[^/@\s:]+:[^/@\s]+@", r"\1***:***@", text)


def _safe_value(value: Any, depth: int = 0) -> Any:
    """事件只保留便于诊断的安全数据，避免把令牌和大对象写进磁盘。"""
    if depth > 3:
        return "<内容过深>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        result = {}
        for key, item in islice(value.items(), 50):
            name = str(key)[:80]
            if any(word in name.lower() for word in (
                "token", "secret", "password", "passwd", "cookie", "apikey", "api_key",
                "authorization", "credential",
            )):
                result[name] = "***"
            else:
                result[name] = _safe_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth + 1) for item in islice(value, 50)]
    return f"<{value.__class__.__name__}>"


class EventJournal:
    def __init__(self, path: Path = EVENT_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_MEMORY_EVENTS)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_tail()

    def _load_tail(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as stream:
                lines = deque(stream, maxlen=MAX_MEMORY_EVENTS)
            for line in lines:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        self._events.append(event)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    def append(self, plugin_id: str, event_type: str, *, persist: bool = True, **data: Any) -> dict[str, Any]:
        event = {
            "id": uuid.uuid4().hex,
            "time": time.time(),
            "plugin_id": plugin_id,
            "event_type": event_type,
            **{key: _safe_value(value) for key, value in data.items()},
        }
        with self._lock:
            if persist:
                try:
                    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                    if self.path.exists() and self.path.stat().st_size >= MAX_EVENT_FILE_BYTES:
                        backup = self.path.with_suffix(".previous.jsonl")
                        self.path.replace(backup)
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(line)
                except OSError as exc:
                    logger.debug("写入插件事件记录失败: %r", exc)
            self._events.append(event)
        return event

    def query(self, plugin_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            values = list(self._events)
        if plugin_id:
            values = [item for item in values if item.get("plugin_id") == plugin_id]
        return list(reversed(values[-limit:]))

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((dict(item) for item in reversed(self._events) if item.get("id") == event_id), None)


class CapabilityRegistry:
    """同一能力允许多个提供者，按优先级调用，失败时自动尝试备用提供者。"""

    def __init__(self):
        self._providers: dict[str, list[tuple[int, str, Any]]] = defaultdict(list)

    def register(self, owner: str, name: str, provider: Any, priority: int = 100) -> Callable[[], None]:
        entry = (int(priority), owner, provider)
        values = self._providers[str(name)]
        values.append(entry)
        values.sort(key=lambda item: item[0], reverse=True)

        def remove() -> None:
            current = self._providers.get(str(name), [])
            self._providers[str(name)] = [item for item in current if item is not entry]
            if not self._providers[str(name)]:
                self._providers.pop(str(name), None)

        return remove

    def providers(self, name: str) -> list[tuple[int, str, Any]]:
        return list(self._providers.get(str(name), []))

    def names(self) -> list[str]:
        return sorted(self._providers)


class PluginGovernor:
    def __init__(self, event_path: Path = EVENT_FILE):
        self.events = EventJournal(event_path)
        self.capabilities = CapabilityRegistry()
        self._policies: dict[str, ResourcePolicy] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._circuits: dict[tuple[str, str], CircuitState] = defaultdict(CircuitState)
        self._tasks: dict[str, set[asyncio.Task]] = defaultdict(set)
        self._replayers: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {}
        self._release_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, int] = {}
        self._executions: dict[str, int] = {}
        self._draining_tasks = _DRAINING_TASKS

    def configure(self, plugin_id: str, resources: Any) -> ResourcePolicy:
        policy = ResourcePolicy.from_mapping(resources)
        previous = self._policies.get(plugin_id)
        if previous is not None and previous.max_concurrency != policy.max_concurrency:
            if self._executions.get(plugin_id, 0):
                # Live instances keep their shared budget until unload. Other
                # limits can change without replacing a semaphore in use.
                policy = replace(policy, max_concurrency=previous.max_concurrency)
            else:
                self._semaphores[plugin_id] = asyncio.Semaphore(policy.max_concurrency)
        self._policies[plugin_id] = policy
        # Account instances share one budget. Replacing a live semaphore would
        # allow each newly configured instance to start another full batch.
        self._semaphores.setdefault(plugin_id, asyncio.Semaphore(policy.max_concurrency))
        return policy

    def policy(self, plugin_id: str) -> ResourcePolicy:
        return self._policies.get(plugin_id) or self.configure(plugin_id, {})

    async def execute(
        self,
        plugin_id: str,
        operation: str,
        func: Callable[[], Any],
        *,
        timeout: float | None = None,
        fallback: Callable[[], Any] | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> Any:
        policy = self.policy(plugin_id)
        semaphore = self._semaphores[plugin_id]
        queued = semaphore.locked()
        if queued and self._pending.get(plugin_id, 0) >= policy.max_pending_tasks:
            raise PluginBusyError("插件任务队列已满，请稍后重试")
        key = (plugin_id, operation)
        circuit = self._circuits[key]
        if circuit.open:
            self.events.append(plugin_id, "circuit_rejected", operation=operation)
            if fallback is not None:
                return await self._invoke(fallback)
            raise RuntimeError(f"插件功能暂时降级：{operation} 连续失败，请稍后重试")

        started = time.monotonic()
        effective_timeout = policy.timeout_seconds if timeout is None else timeout
        acquired = False
        released = False
        callback_started = False
        if queued:
            self._pending[plugin_id] = self._pending.get(plugin_id, 0) + 1
            self.events.append(plugin_id, "execution_queued", persist=False, operation=operation)
        self._executions[plugin_id] = self._executions.get(plugin_id, 0) + 1
        try:
            # Include admission wait in the deadline and run in the caller's
            # task, so a plugin can close its own context without cancelling a
            # separate wait_for parent. Explicit timeout<=0 retains workers.
            async with asyncio.timeout(effective_timeout if effective_timeout > 0 else None):
                if queued:
                    with waiting_phase("等待插件执行名额"):
                        await semaphore.acquire()
                else:
                    await semaphore.acquire()
                acquired = True
                if queued:
                    self._remove_pending(plugin_id)
                    queued = False
                try:
                    if self._semaphores.get(plugin_id) is not semaphore or plugin_id in self._release_tasks:
                        raise PluginBusyError("插件已停用或正在重新加载")
                    self.events.append(
                        plugin_id, "execution_started", persist=False,
                        operation=operation, data=event_data or {},
                    )
                    callback_started = True
                    result = await self._invoke(func)
                finally:
                    semaphore.release()
                    released = True
            circuit.failures = 0
            circuit.opened_until = 0
            circuit.last_error = ""
            self.events.append(
                plugin_id, "execution_succeeded", persist=False, operation=operation,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            return result
        except asyncio.CancelledError:
            self.events.append(plugin_id, "execution_cancelled", operation=operation)
            raise
        except (PluginBusyError, PluginQueueTimeout) as exc:
            # Nested capability calls may also be waiting on a saturated
            # provider. Overload must not open the caller's circuit either.
            if callback_started:
                exc.not_started = False
            raise
        except TimeoutError as exc:
            if not acquired:
                self.events.append(
                    plugin_id, "queue_timeout", operation=operation,
                )
                raise PluginQueueTimeout("等待插件任务执行超时") from None
            circuit.failures += 1
            circuit.last_error = (
                _safe_text(f"{exc.__class__.__name__}: {exc}")[:500]
                if str(exc) else "TimeoutError: 插件任务执行超时"
            )
            self._record_failure(plugin_id, operation, circuit, policy)
            if fallback is not None:
                return await self._invoke(fallback)
            raise
        except Exception as exc:
            if isinstance(exc, StopPropagation):
                # Telegram propagation control is successful handling, not a plugin failure.
                circuit.failures = 0
                circuit.opened_until = 0
                circuit.last_error = ""
                raise
            circuit.failures += 1
            circuit.last_error = _safe_text(f"{exc.__class__.__name__}: {exc}")[:500]
            self._record_failure(plugin_id, operation, circuit, policy)
            if fallback is not None:
                return await self._invoke(fallback)
            raise
        finally:
            if queued:
                self._remove_pending(plugin_id)
            if acquired and not released:
                semaphore.release()
            count = self._executions.get(plugin_id, 0) - 1
            if count > 0:
                self._executions[plugin_id] = count
            else:
                self._executions.pop(plugin_id, None)

    def _remove_pending(self, plugin_id: str) -> None:
        count = self._pending.get(plugin_id, 0) - 1
        if count > 0:
            self._pending[plugin_id] = count
        else:
            self._pending.pop(plugin_id, None)

    def _record_failure(self, plugin_id: str, operation: str,
                        circuit: CircuitState, policy: ResourcePolicy) -> None:
        if circuit.failures >= policy.failure_threshold:
            circuit.opened_until = time.monotonic() + policy.recovery_seconds
            self.events.append(plugin_id, "circuit_opened", operation=operation, error=circuit.last_error)
        else:
            self.events.append(plugin_id, "execution_failed", operation=operation, error=circuit.last_error)

    def track_draining_task(self, owner_id: str, task: asyncio.Task) -> None:
        """Keep timed-out shutdown work visible until it really finishes.

        Do not force-cancel these tasks: they may be closing SQLite transactions
        or waiting for a native worker to release a browser safely.
        """
        if task.done():
            if not task.cancelled():
                task.exception()
            return
        tasks = self._draining_tasks.setdefault(owner_id, set())
        if task in tasks:
            return
        tasks.add(task)

        def completed(done_task: asyncio.Task) -> None:
            tasks.discard(done_task)
            if not tasks and self._draining_tasks.get(owner_id) is tasks:
                self._draining_tasks.pop(owner_id, None)
            if not done_task.cancelled():
                done_task.exception()

        task.add_done_callback(completed)

    def pending_shutdown_tasks(self, plugin_id: str) -> int:
        return sum(
            not task.done()
            for owner_id, tasks in self._draining_tasks.items()
            if self._belongs_to(owner_id, plugin_id)
            for task in tasks
        )

    @staticmethod
    async def _invoke(func: Callable[[], Any]) -> Any:
        result = func()
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _belongs_to(owner_id: str, plugin_id: str) -> bool:
        return owner_id == plugin_id or owner_id.startswith(f"{plugin_id}@")

    def background_tasks(self, plugin_id: str) -> int:
        return sum(
            not task.done()
            for owner_id, tasks in self._tasks.items()
            if self._belongs_to(owner_id, plugin_id)
            for task in tasks
        )

    def create_task(self, plugin_id: str, awaitable: Awaitable, *, name: str | None = None,
                    owner_id: str | None = None) -> asyncio.Task:
        policy = self.policy(plugin_id)
        owner_id = owner_id or plugin_id
        if not self._belongs_to(owner_id, plugin_id):
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise ValueError("后台任务 owner 必须属于当前插件")
        tasks = self._tasks[owner_id]
        active_count = self.background_tasks(plugin_id)
        if active_count >= policy.max_background_tasks:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError(f"插件后台任务已达到上限（{policy.max_background_tasks}）")
        task = asyncio.create_task(awaitable, name=name)
        tasks.add(task)

        def completed(done_task: asyncio.Task) -> None:
            tasks.discard(done_task)
            if not tasks and self._tasks.get(owner_id) is tasks:
                self._tasks.pop(owner_id, None)
            if done_task.cancelled():
                return
            # 后台任务可能无人显式 await；读取异常可避免事件循环再报
            # “Task exception was never retrieved”，具体失败已经由执行管道记录。
            done_task.exception()

        task.add_done_callback(completed)
        return task

    async def cancel_all(self, owner_id: str, timeout: float = 10.0, *,
                         exclude: set[asyncio.Task] | None = None) -> dict[str, int]:
        tracked = self._tasks.get(owner_id, set())
        tasks = [task for task in tracked if not task.done()]
        current = asyncio.current_task()
        excluded = exclude or set()
        tasks = [task for task in tasks if task is not current and task not in excluded]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                self.track_draining_task(owner_id, task)
                logger.warning("插件后台任务未能及时退出 [%s]: %s", owner_id, task.get_name())
        else:
            done, pending = set(), set()
        if tasks:
            plugin_id = owner_id.split("@", 1)[0]
            self.events.append(plugin_id, "tasks_cancelled", completed=len(done), pending=len(pending))
        if not pending:
            self._tasks.pop(owner_id, None)
        return {"completed": len(done), "pending": len(pending)}

    async def call_capability(self, caller: str, name: str, method: str | None, *args, **kwargs) -> Any:
        providers = self.capabilities.providers(name)
        if not providers:
            raise LookupError(f"没有可用能力：{name}")
        errors = []
        admission_errors = []
        for index, (_, owner, provider) in enumerate(providers):
            try:
                target = getattr(provider, method) if method else provider
                operation = f"capability:{name}:{method or 'call'}:{index}"
                return await self.execute(owner, operation, lambda: target(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 - 失败后继续备用链
                errors.append(f"{owner}: {exc}")
                if isinstance(exc, (PluginBusyError, PluginQueueTimeout)):
                    admission_errors.append(exc)
        self.events.append(caller, "capability_exhausted", capability=name, errors=errors)
        if len(admission_errors) == len(providers):
            # No provider actually failed: retain admission classification for
            # the caller's circuit and the API's retryable overload response.
            raise admission_errors[-1]
        raise RuntimeError(f"能力 {name} 的所有提供者都不可用：{'；'.join(errors)}")

    def register_replayer(self, plugin_id: str, event_type: str, handler: Callable) -> Callable[[], None]:
        key = (plugin_id, event_type)
        self._replayers[key] = handler

        def remove() -> None:
            if self._replayers.get(key) is handler:
                self._replayers.pop(key, None)
        return remove

    async def replay(self, plugin_id: str, event_id: str) -> Any:
        event = self.events.get(event_id)
        if not event or event.get("plugin_id") != plugin_id:
            raise LookupError("事件不存在或不属于该插件")
        event_type = str(event.get("replay_type") or event.get("event_type") or "")
        instance_id = str(event.get("instance_id") or plugin_id)
        handler = self._replayers.get((instance_id, event_type))
        if handler is None:
            raise LookupError("该事件没有可回放的处理器")
        self.events.append(plugin_id, "event_replayed", source_event_id=event_id, replay_type=event_type)
        return await self.execute(
            plugin_id,
            f"replay:{instance_id}:{event_type}",
            lambda: handler(dict(event.get("payload") or {})),
        )

    def status(self, plugin_id: str) -> dict[str, Any]:
        policy = self.policy(plugin_id)
        circuits = []
        for (owner, operation), state in self._circuits.items():
            if owner != plugin_id:
                continue
            circuits.append({
                "operation": operation,
                "failures": state.failures,
                "open": state.open,
                "last_error": state.last_error,
            })
        return {
            "policy": asdict(policy),
            "background_tasks": self.background_tasks(plugin_id),
            "pending_tasks": self._pending.get(plugin_id, 0),
            "pending_shutdown_tasks": self.pending_shutdown_tasks(plugin_id),
            "circuits": circuits,
        }

    async def release(self, plugin_id: str) -> None:
        task = self._release_tasks.get(plugin_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._release(plugin_id, caller=asyncio.current_task()), name=f"governor-release:{plugin_id}",
            )
            self._release_tasks[plugin_id] = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _release(self, plugin_id: str, *, caller: asyncio.Task | None = None) -> None:
        try:
            owners = [owner_id for owner_id in self._tasks if self._belongs_to(owner_id, plugin_id)]
            for owner_id in owners:
                await self.cancel_all(owner_id, exclude={caller} if caller is not None else None)
            self._policies.pop(plugin_id, None)
            self._semaphores.pop(plugin_id, None)
            for key in [key for key in self._circuits if key[0] == plugin_id]:
                self._circuits.pop(key, None)
            for key in [
                key for key in self._replayers
                if key[0] == plugin_id or key[0].startswith(f"{plugin_id}@")
            ]:
                self._replayers.pop(key, None)
        finally:
            current = asyncio.current_task()
            if self._release_tasks.get(plugin_id) is current:
                self._release_tasks.pop(plugin_id, None)
