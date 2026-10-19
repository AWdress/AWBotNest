"""Small, bounded SQLite store for the administrator assistant.

Connections and transactions live entirely in a worker thread. Cancellation
waits for the short transaction to finish; it never leaves an orphan writer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
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
            "'$.pending_input',NULL,"
            "'$.updated_at',?),updated=? WHERE kind='run' AND "
            "json_extract(body,'$.status') IN ('queued','running','waiting_confirmation','waiting_input')",
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
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("历史搜索内容不能为空，且不超过 2000 字")
        pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
        def work(db):
            matches = []
            # Stream at most one bounded session at a time, not a list of all
            # 100 bodies. Search saved observations too, never raw media.
            for session_id, body in db.execute("SELECT id,body FROM documents WHERE kind='session' "
                                               "ORDER BY updated DESC LIMIT 100"):
                for message in json.loads(body).get("messages", []):
                    text = self._history_text(message)
                    match = pattern.search(text)
                    if not match:
                        continue
                    width = 640
                    while True:
                        offset = max(0, match.start() - min(160, width // 3))
                        result = self._history_page(session_id, message, text, offset, width)
                        result["match_offset"] = match.start()
                        if len(json.dumps(result, ensure_ascii=False)) <= 2000 or width <= 32:
                            break
                        width //= 2
                    candidate = [*matches, result]
                    if len(json.dumps(candidate, ensure_ascii=False)) > 7000:
                        return matches
                    matches.append(result)
                    if len(matches) == 8:
                        return matches
            return matches
        return await self.transaction(work)

    @staticmethod
    def _history_text(message):
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            return ""
        from .assistant_context import content
        try:
            value = content(message)
            return value if isinstance(value, str) else ""
        except (KeyError, TypeError):
            return ""

    @staticmethod
    def _history_id(message):
        if isinstance(message.get("id"), str) and 0 < len(message["id"]) <= 200:
            return message["id"]
        # Old records did not have IDs. A content-derived reference remains
        # stable when new messages are appended or old ones are trimmed.
        encoded = json.dumps(message, ensure_ascii=False, sort_keys=True)
        return "legacy:" + hashlib.sha256(encoded.encode()).hexdigest()[:32]

    @classmethod
    def _history_page(cls, session_id, message, text, offset, limit):
        end = min(len(text), offset + limit)
        return {"session_id": session_id, "message_id": cls._history_id(message),
                "role": message["role"], "content": text[offset:end], "offset": offset,
                "next_offset": end if end < len(text) else None, "total": len(text),
                "complete": end >= len(text), "truncated": offset > 0 or end < len(text), "untrusted": True}

    async def read_history_message(self, session_id, message_id, offset=0, limit=1500):
        if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 200
                or not isinstance(message_id, str) or not 1 <= len(message_id) <= 200
                or type(offset) is not int or not 0 <= offset <= 512_000
                or type(limit) is not int or not 1 <= limit <= 2000):
            raise ValueError("历史消息或读取位置不正确")
        def work(db):
            row = db.execute("SELECT body FROM documents WHERE kind='session' AND id=?", (session_id,)).fetchone()
            messages = [] if row is None else json.loads(row[0]).get("messages", [])
            found = [message for message in messages if isinstance(message, dict)
                     and message.get("role") in {"user", "assistant"} and self._history_id(message) == message_id]
            if len(found) != 1:
                raise ValueError("历史消息不存在、已清理或编号不唯一，请重新搜索")
            message, text = found[0], self._history_text(found[0])
            result = self._history_page(session_id, message, text, offset, limit)
            # Escaped control characters can expand sixfold in JSON. Keep the
            # pagination envelope intact instead of letting callers chop it.
            while len(json.dumps(result, ensure_ascii=False)) > 7000 and limit > 1:
                limit_reduced = max(1, len(result["content"]) // 2)
                result = self._history_page(session_id, message, text, offset, limit_reduced)
                if limit_reduced == 1:
                    break
            if len(json.dumps(result, ensure_ascii=False)) > 7000:
                raise ValueError("历史消息标识过长，无法完整展示")
            return result
        return await self.transaction(work)

    async def prune(self):
        def work(db):
            # Sessions carry their own bounded history; remove orphan runs too.
            db.execute("DELETE FROM documents WHERE kind='session' AND id NOT IN "
                       "(SELECT id FROM documents WHERE kind='session' ORDER BY updated DESC LIMIT 100) "
                       "AND id NOT IN (SELECT json_extract(body,'$.session_id') FROM documents "
                       "WHERE kind='run' AND json_extract(body,'$.status') IN ('queued','running','waiting_confirmation','waiting_input'))")
            db.execute("DELETE FROM documents WHERE kind='run' AND id NOT IN "
                       "(SELECT id FROM documents WHERE kind='run' ORDER BY updated DESC LIMIT 200) "
                       "AND json_extract(body,'$.status') NOT IN ('queued','running','waiting_confirmation','waiting_input')")
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
