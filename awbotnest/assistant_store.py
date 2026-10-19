"""Small, bounded SQLite store for the administrator assistant.

Connections and transactions live entirely in a worker thread. Cancellation
waits for the short transaction to finish; it never leaves an orphan writer.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class AssistantStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def transaction(self, callback):
        async with self._lock:
            def work():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=5)) as db:
                    with db:
                        db.execute("PRAGMA busy_timeout=5000")
                        db.execute("CREATE TABLE IF NOT EXISTS documents "
                                   "(kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, "
                                   "updated REAL NOT NULL, PRIMARY KEY(kind,id))")
                        return callback(db)
            task = asyncio.create_task(asyncio.to_thread(work))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return result

    @staticmethod
    def _put(db, kind, item):
        encoded = json.dumps(item, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 512_000:
            raise ValueError("助手记录过长，请新建会话")
        db.execute("INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(kind,id) "
                   "DO UPDATE SET body=excluded.body,updated=excluded.updated",
                   (kind, item["id"], encoded, time.time()))

    async def put(self, kind, item):
        await self.transaction(lambda db: self._put(db, kind, item))
        return item

    async def get(self, kind, item_id):
        def work(db):
            row = db.execute("SELECT body FROM documents WHERE kind=? AND id=?",
                             (kind, item_id)).fetchone()
            return json.loads(row[0]) if row else None
        return await self.transaction(work)

    async def list(self, kind, limit=200):
        def work(db):
            return [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM documents WHERE kind=? ORDER BY updated DESC LIMIT ?",
                (kind, min(500, max(1, limit))))]
        return await self.transaction(work)

    async def delete(self, kind, item_id):
        await self.transaction(lambda db: db.execute(
            "DELETE FROM documents WHERE kind=? AND id=?", (kind, item_id)).rowcount)

    async def interrupt_active(self, updated_at):
        """Restart recovery must include old active runs outside the list limit."""
        await self.transaction(lambda db: db.execute(
            "UPDATE documents SET body=json_set(body,'$.status','interrupted',"
            "'$.error','系统已重启，本次未继续执行','$.pending_confirmation',NULL,"
            "'$.updated_at',?),updated=? WHERE kind='run' AND "
            "json_extract(body,'$.status') IN ('queued','running','waiting_confirmation')",
            (updated_at, time.time())).rowcount)

    async def sessions(self):
        def work(db):
            return [json.loads(row[0]) for row in db.execute(
                "SELECT json_remove(body,'$.messages') FROM documents WHERE kind='session' "
                "ORDER BY updated DESC LIMIT 100")]
        return await self.transaction(work)

    async def interrupt_issue_submissions(self, message):
        return await self.transaction(lambda db: db.execute(
            "UPDATE documents SET body=json_set(body,'$.status','uncertain',"
            "'$.error',?,'$.updated_at',?),updated=? WHERE kind='issue_draft' "
            "AND json_extract(body,'$.status')='submitting'",
            (message, time.time(), time.time())).rowcount)

    async def search_history(self, query):
        def work(db):
            return [{"session_id": row[0], "role": row[1], "content": row[2]} for row in db.execute(
                "SELECT d.id,json_extract(m.value,'$.role'),substr(json_extract(m.value,'$.content'),1,1000) "
                "FROM documents d,json_each(d.body,'$.messages') m WHERE d.kind='session' "
                "AND instr(lower(json_extract(m.value,'$.content')),lower(?))>0 "
                "ORDER BY d.updated DESC LIMIT 8", (query,))]
        return await self.transaction(work)

    async def prune(self):
        def work(db):
            # Sessions carry their own bounded history; remove orphan runs too.
            db.execute("DELETE FROM documents WHERE kind='session' AND id NOT IN "
                       "(SELECT id FROM documents WHERE kind='session' ORDER BY updated DESC LIMIT 100) "
                       "AND id NOT IN (SELECT json_extract(body,'$.session_id') FROM documents "
                       "WHERE kind='run' AND json_extract(body,'$.status') IN ('queued','running','waiting_confirmation'))")
            db.execute("DELETE FROM documents WHERE kind='run' AND id NOT IN "
                       "(SELECT id FROM documents WHERE kind='run' ORDER BY updated DESC LIMIT 200) "
                       "AND json_extract(body,'$.status') NOT IN ('queued','running','waiting_confirmation')")
            db.execute("DELETE FROM documents WHERE kind='run' AND "
                       "json_extract(body,'$.session_id') NOT IN "
                       "(SELECT id FROM documents WHERE kind='session')")
            db.execute("PRAGMA incremental_vacuum(32)")
        await self.transaction(work)

    async def delete_session(self, session_id):
        def work(db):
            db.execute("DELETE FROM documents WHERE kind='session' AND id=?", (session_id,))
            db.execute("DELETE FROM documents WHERE kind='run' AND "
                       "json_extract(body,'$.session_id')=?", (session_id,))
        await self.transaction(work)
