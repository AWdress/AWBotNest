from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import DATA_DIR


class PluginKV:
    def __init__(self, plugin_id: str) -> None:
        root = DATA_DIR / "kv"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{plugin_id}.sqlite"
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("单个 KV 值不能超过 10 MB")
        if self.path.exists() and self.path.stat().st_size > 256 * 1024 * 1024:
            raise RuntimeError("插件 KV 存储已达到 256 MB 上限")
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def delete(self, key: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM kv WHERE key = ?", (key,))
        return cursor.rowcount > 0

    def items(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM kv ORDER BY key").fetchall()
        return {key: json.loads(value) for key, value in rows}
