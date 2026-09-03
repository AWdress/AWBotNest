from __future__ import annotations

import logging
import re
from pathlib import Path
from collections import deque
from datetime import datetime, timezone


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=capacity)

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
        self.records.appendleft({
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "source": record.name,
            "message": message[:8000],
        })

    def recent(self, limit: int = 200) -> list[dict[str, str]]:
        self.acquire()
        try:
            return list(self.records)[:max(1, min(limit, 1000))]
        finally:
            self.release()

    def trim(self, keep: int) -> int:
        limit = max(1, min(int(keep), self.records.maxlen or 1000))
        self.acquire()
        try:
            removed = max(0, len(self.records) - limit)
            while len(self.records) > limit:
                self.records.pop()
            return removed
        finally:
            self.release()


memory_logs = MemoryLogHandler()

# V1 also persisted the application stream to a rotating file.  Keep that
# durability in V2 while retaining the clean Docker stdout format.
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "app.log"


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
