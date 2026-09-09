from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PluginSession:
    """One in-memory session inside a fixed plugin instance namespace."""

    plugin_id: str
    instance_id: str
    key: str
    data: dict[str, Any] = field(default_factory=dict)
    lock: Any = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    _ttl_seconds: float | None = field(default=None, repr=False)

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    def touch(self, ttl: float | None = None) -> None:
        """Record activity and optionally replace the session TTL."""
        if ttl is not None:
            if ttl <= 0:
                raise ValueError("Session TTL 必须大于 0")
            self._ttl_seconds = float(ttl)
        self.updated_at = time.time()
        self.expires_at = (
            self.updated_at + self._ttl_seconds if self._ttl_seconds is not None else None
        )


class SessionManager:
    """In-memory sessions scoped permanently to one plugin runtime instance."""

    def __init__(self, plugin_id: str, instance_id: str, *, default_ttl: float | None = None,
                 cleanup_interval: float = 60.0, profiler: Any = None) -> None:
        if default_ttl is not None and default_ttl <= 0:
            raise ValueError("Session 默认 TTL 必须大于 0")
        if cleanup_interval <= 0:
            raise ValueError("Session 清理间隔必须大于 0")
        self.plugin_id = plugin_id
        self.instance_id = instance_id
        self.default_ttl = float(default_ttl) if default_ttl is not None else None
        self.cleanup_interval = float(cleanup_interval)
        self.profiler = profiler
        self._sessions: dict[str, PluginSession] = {}
        self._registry_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def size(self) -> int:
        return len(self._sessions)

    def _validate_key(self, key: str) -> str:
        value = str(key)
        if not value:
            raise ValueError("Session key 不能为空")
        if len(value) > 512:
            raise ValueError("Session key 不能超过 512 个字符")
        return value

    def _ensure_cleanup_task(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(), name=f"session-cleanup:{self.instance_id}",
            )

    async def get(self, key: str, *, ttl: float | None = None,
                  initial: dict[str, Any] | None = None) -> PluginSession:
        """Get or create an active session in this manager's fixed namespace."""
        if self._closed:
            raise RuntimeError("插件 Session Runtime 已关闭")
        key = self._validate_key(key)
        effective_ttl = self.default_ttl if ttl is None else float(ttl)
        if effective_ttl is not None and effective_ttl <= 0:
            raise ValueError("Session TTL 必须大于 0")
        async with self._registry_lock:
            if self._closed:
                raise RuntimeError("插件 Session Runtime 已关闭")
            session = self._sessions.get(key)
            if session is not None and session.expired and not session.lock.locked():
                self._sessions.pop(key, None)
                session = None
            if session is None:
                session = PluginSession(
                    self.plugin_id, self.instance_id, key, dict(initial or {}),
                    lock=self.profiler.lock(key) if self.profiler is not None else asyncio.Lock(),
                    _ttl_seconds=effective_ttl,
                )
                self._sessions[key] = session
            session.touch(effective_ttl)
            if session.expires_at is not None:
                self._ensure_cleanup_task()
            return session

    async def open(self, key: str, *, ttl: float | None = None,
                   initial: dict[str, Any] | None = None) -> PluginSession:
        """Alias for get(), useful when a plugin treats lookup as opening a session."""
        return await self.get(key, ttl=ttl, initial=initial)

    async def reset(self, key: str) -> bool:
        """Remove one session after any active mutation has released its lock."""
        key = self._validate_key(key)
        async with self._registry_lock:
            session = self._sessions.get(key)
        if session is None:
            return False
        async with session.lock:
            async with self._registry_lock:
                if self._sessions.get(key) is not session:
                    return False
                self._sessions.pop(key, None)
                session.data.clear()
                return True

    async def cleanup(self) -> int:
        """Remove expired, currently-unlocked sessions and return their count."""
        async with self._registry_lock:
            candidates = [session for session in self._sessions.values()
                          if session.expired and not session.lock.locked()]
        removed = 0
        for session in candidates:
            async with session.lock:
                async with self._registry_lock:
                    if self._sessions.get(session.key) is session and session.expired:
                        self._sessions.pop(session.key, None)
                        session.data.clear()
                        removed += 1
        return removed

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval)
                await self.cleanup()
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        """Stop TTL cleanup and clear all sessions during plugin unload/shutdown."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._drain(), name=f"session-close:{self.instance_id}",
            )
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.gather(self._close_task, return_exceptions=True)
            raise

    async def _drain(self) -> None:
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        async with self._registry_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            async with session.lock:
                session.data.clear()
