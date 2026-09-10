from __future__ import annotations

import logging
import json
import os
import re
import threading
from pathlib import Path
from collections import deque
from datetime import datetime, timezone

from .config import DATA_DIR


# Keep the human-readable application log for diagnostics, and a compact
# structured history for the admin console.  The latter is what lets plugin
# log dialogs survive a process restart without trying to recover logger names
# from the plain text app.log file.
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"
HISTORY_FILE = LOG_DIR / "webui_history.jsonl"
HISTORY_MAX_BYTES = 5 * 1024 * 1024
HISTORY_COMPACT_WRITES = 200


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=capacity)
        self._history_lock = threading.RLock()
        self._history_loaded = False
        self._writes_since_compact = 0

    @staticmethod
    def _valid_history_item(value: object) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("timestamp"), str)
            and isinstance(value.get("level"), str)
            and isinstance(value.get("source"), str)
            and isinstance(value.get("message"), str)
        )

    def _compact_history_locked(self) -> None:
        """Rewrite history from the bounded in-memory snapshot atomically."""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = HISTORY_FILE.with_suffix(HISTORY_FILE.suffix + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
                # records are newest-first; the file stays chronological.
                for item in reversed(self.records):
                    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(temp_path, HISTORY_FILE)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def load_persisted(self) -> None:
        """Restore the latest structured records once per process."""
        with self._history_lock:
            if self._history_loaded:
                return
            self._history_loaded = True
            if not HISTORY_FILE.is_file():
                return
            try:
                with HISTORY_FILE.open("r", encoding="utf-8", errors="replace") as stream:
                    recent_lines = deque(stream, maxlen=self.records.maxlen or 1000)
                for line in recent_lines:
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if self._valid_history_item(item):
                        self.records.appendleft({
                            "timestamp": item["timestamp"],
                            "level": item["level"],
                            "source": item["source"],
                            "message": item["message"],
                        })
                if HISTORY_FILE.stat().st_size > HISTORY_MAX_BYTES:
                    self._compact_history_locked()
            except OSError:
                return

    def _persist_locked(self, item: dict[str, str]) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with HISTORY_FILE.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._writes_since_compact += 1
            if self._writes_since_compact >= HISTORY_COMPACT_WRITES:
                self._compact_history_locked()
                self._writes_since_compact = 0
            elif HISTORY_FILE.stat().st_size > HISTORY_MAX_BYTES:
                self._compact_history_locked()
                self._writes_since_compact = 0
        except OSError:
            # Logging must never take the platform down when data is read-only.
            return

    def emit(self, record: logging.LogRecord) -> None:
        # Uvicorn access/connection chatter is not an application event and
        # only obscures the platform log stream. Warnings and errors remain.
        if record.name.startswith("uvicorn") and record.levelno < logging.WARNING:
            return
        message = self.format(record)
        if record.name == "asyncio" and "ConnectionResetError: [WinError 10054]" in message:
            return
        message = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1***", message)
        message = re.sub(r"(?i)((?:token|secret|password|api[_-]?key)\s*[:=]\s*)[^\s,;&]+", r"\1***", message)
        item = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "source": record.name,
            "message": message[:8000],
        }
        self.load_persisted()
        with self._history_lock:
            self.records.appendleft(item)
            self._persist_locked(item)

    def recent(self, limit: int = 200) -> list[dict[str, str]]:
        self.load_persisted()
        self.acquire()
        try:
            return list(self.records)[:max(1, min(limit, 1000))]
        finally:
            self.release()

    def trim(self, keep: int) -> int:
        limit = max(1, min(int(keep), self.records.maxlen or 1000))
        self.load_persisted()
        self.acquire()
        try:
            removed = max(0, len(self.records) - limit)
            while len(self.records) > limit:
                self.records.pop()
            with self._history_lock:
                # Compact even when memory already has <= keep records so
                # stale lines from an older run cannot return after restart.
                if HISTORY_FILE.exists():
                    self._compact_history_locked()
                    self._writes_since_compact = 0
            return removed
        finally:
            self.release()


memory_logs = MemoryLogHandler()


class PyrogramNoiseFilter(logging.Filter):
    """Drop known framework chatter that is not useful to operators."""
    _noisy = ("PEER_ID_INVALID", "ID not found:", "PeerIdInvalid",
              "CHANNEL_INVALID", "CHANNEL_PRIVATE")

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("pyrogram"):
            return True
        text = f"{record.getMessage()} {record.exc_text or ''}"
        return not any(item in text for item in self._noisy)


def create_file_handler(formatter: logging.Formatter) -> logging.Handler | None:
    """Create V1-compatible UTF-8 rotating persistence, failing soft on mounts."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.addFilter(PyrogramNoiseFilter())
        return handler
    except OSError as exc:
        logging.getLogger("awbotnest.logs").warning("文件日志不可用，继续使用终端日志：%s", exc)
        return None
