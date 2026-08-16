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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from libs.log import logger


EVENT_FILE = Path("data/plugin_events.jsonl")
MAX_EVENT_FILE_BYTES = 8 * 1024 * 1024
MAX_MEMORY_EVENTS = 1000


@dataclass(frozen=True)
class ResourcePolicy:
    timeout_seconds: float = 120.0
    max_concurrency: int = 8
    max_background_tasks: int = 32
    failure_threshold: int = 5
    recovery_seconds: float = 60.0

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
        )


@dataclass
class CircuitState:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""

    @property
    def open(self) -> bool:
        return self.opened_until > time.monotonic()


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
        for key, item in list(value.items())[:50]:
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
        return [_safe_value(item, depth + 1) for item in list(value)[:50]]
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
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_MEMORY_EVENTS:]
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
                    logger.warning("写入插件事件记录失败: %r", exc)
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
    def __init__(self):
        self.events = EventJournal()
        self.capabilities = CapabilityRegistry()
        self._policies: dict[str, ResourcePolicy] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._circuits: dict[tuple[str, str], CircuitState] = defaultdict(CircuitState)
        self._tasks: dict[str, set[asyncio.Task]] = defaultdict(set)
        self._replayers: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {}

    def configure(self, plugin_id: str, resources: Any) -> ResourcePolicy:
        policy = ResourcePolicy.from_mapping(resources)
        self._policies[plugin_id] = policy
        self._semaphores[plugin_id] = asyncio.Semaphore(policy.max_concurrency)
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
        key = (plugin_id, operation)
        circuit = self._circuits[key]
        if circuit.open:
            self.events.append(plugin_id, "circuit_rejected", operation=operation)
            if fallback is not None:
                return await self._invoke(fallback)
            raise RuntimeError(f"插件功能暂时降级：{operation} 连续失败，请稍后重试")

        started = time.monotonic()
        self.events.append(
            plugin_id, "execution_started", persist=False,
            operation=operation, data=event_data or {},
        )
        try:
            async with self._semaphores[plugin_id]:
                result = await asyncio.wait_for(
                    self._invoke(func), timeout=timeout or policy.timeout_seconds,
                )
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
        except Exception as exc:
            circuit.failures += 1
            circuit.last_error = _safe_text(f"{exc.__class__.__name__}: {exc}")[:500]
            if circuit.failures >= policy.failure_threshold:
                circuit.opened_until = time.monotonic() + policy.recovery_seconds
                self.events.append(plugin_id, "circuit_opened", operation=operation, error=circuit.last_error)
            else:
                self.events.append(plugin_id, "execution_failed", operation=operation, error=circuit.last_error)
            if fallback is not None:
                return await self._invoke(fallback)
            raise

    @staticmethod
    async def _invoke(func: Callable[[], Any]) -> Any:
        result = func()
        if inspect.isawaitable(result):
            return await result
        return result

    def create_task(self, plugin_id: str, awaitable: Awaitable, *, name: str | None = None) -> asyncio.Task:
        policy = self.policy(plugin_id)
        tasks = self._tasks[plugin_id]
        active_count = sum(not task.done() for task in tasks)
        if active_count >= policy.max_background_tasks:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError(f"插件后台任务已达到上限（{policy.max_background_tasks}）")
        task = asyncio.create_task(awaitable, name=name)
        tasks.add(task)

        def completed(done_task: asyncio.Task) -> None:
            tasks.discard(done_task)
            if done_task.cancelled():
                return
            # 后台任务可能无人显式 await；读取异常可避免事件循环再报
            # “Task exception was never retrieved”，具体失败已经由执行管道记录。
            done_task.exception()

        task.add_done_callback(completed)
        return task

    async def cancel_all(self, plugin_id: str, timeout: float = 10.0) -> dict[str, int]:
        tasks = [task for task in self._tasks.pop(plugin_id, set()) if not task.done()]
        current = asyncio.current_task()
        tasks = [task for task in tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                logger.warning("插件后台任务未能及时退出 [%s]: %s", plugin_id, task.get_name())
        else:
            done, pending = set(), set()
        if tasks:
            self.events.append(plugin_id, "tasks_cancelled", completed=len(done), pending=len(pending))
        return {"completed": len(done), "pending": len(pending)}

    async def call_capability(self, caller: str, name: str, method: str | None, *args, **kwargs) -> Any:
        providers = self.capabilities.providers(name)
        if not providers:
            raise LookupError(f"没有可用能力：{name}")
        errors = []
        for index, (_, owner, provider) in enumerate(providers):
            try:
                target = getattr(provider, method) if method else provider
                operation = f"capability:{name}:{method or 'call'}:{index}"
                return await self.execute(owner, operation, lambda: target(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 - 失败后继续备用链
                errors.append(f"{owner}: {exc}")
        self.events.append(caller, "capability_exhausted", capability=name, errors=errors)
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
            "background_tasks": sum(not task.done() for task in self._tasks.get(plugin_id, set())),
            "circuits": circuits,
        }

    async def release(self, plugin_id: str) -> None:
        await self.cancel_all(plugin_id)
        self._policies.pop(plugin_id, None)
        self._semaphores.pop(plugin_id, None)
        for key in [key for key in self._circuits if key[0] == plugin_id]:
            self._circuits.pop(key, None)
        for key in [
            key for key in self._replayers
            if key[0] == plugin_id or key[0].startswith(f"{plugin_id}@")
        ]:
            self._replayers.pop(key, None)


governor = PluginGovernor()
