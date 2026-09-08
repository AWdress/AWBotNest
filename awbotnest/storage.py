from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from .config import DATA_DIR


T = TypeVar("T")


class PluginKV:
    """Asynchronous, namespace-isolated SQLite storage for one plugin instance.

    SQLite work runs in a worker thread. Cancellation waits for an active transaction
    before releasing its operation lock, so later operations cannot race an orphaned write.
    """

    _locks_guard = threading.Lock()
    _database_locks: dict[Path, threading.RLock] = {}

    def __init__(self, plugin_id: str) -> None:
        self.path = DATA_DIR / "kv" / f"{plugin_id}.sqlite"
        resolved = self.path.resolve()
        with self._locks_guard:
            self._database_lock = self._database_locks.setdefault(resolved, threading.RLock())
        self._operation_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            if not self._initialized:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                self._initialized = True
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _run(self, operation: Callable[[], T]) -> T:
        if self._closed:
            raise RuntimeError("插件存储已关闭")
        async with self._operation_lock:
            if self._closed:
                raise RuntimeError("插件存储已关闭")

            def guarded() -> T:
                with self._database_lock:
                    return operation()

            task = asyncio.create_task(asyncio.to_thread(guarded))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # sqlite transactions cannot be interrupted safely. Wait for commit or
                # rollback while retaining the namespace lock, then propagate cancellation.
                await asyncio.gather(task, return_exceptions=True)
                raise

    def _get(self, key: str, default: Any) -> Any:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    async def get(self, key: str, default: Any = None) -> Any:
        return await self._run(lambda: self._get(key, default))

    def _set(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("单个 KV 值不能超过 10 MB")
        if self.path.exists() and self.path.stat().st_size > 256 * 1024 * 1024:
            raise RuntimeError("插件 KV 存储已达到 256 MB 上限")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    async def set(self, key: str, value: Any) -> None:
        await self._run(lambda: self._set(key, value))

    def _delete(self, key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM kv WHERE key = ?", (key,))
        return cursor.rowcount > 0

    async def delete(self, key: str) -> bool:
        return await self._run(lambda: self._delete(key))

    def _items(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM kv ORDER BY key").fetchall()
        return {key: json.loads(value) for key, value in rows}

    async def items(self) -> dict[str, Any]:
        return await self._run(self._items)

    async def close(self) -> None:
        """Reject new operations and wait for the active SQLite operation to finish."""
        self._closed = True

        async def drain() -> None:
            async with self._operation_lock:
                return

        task = asyncio.create_task(drain())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise
