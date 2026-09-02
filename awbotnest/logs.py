from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime, timezone


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
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
