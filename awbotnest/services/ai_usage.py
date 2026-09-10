from __future__ import annotations

import json
import logging
import threading
from pathlib import Path


logger = logging.getLogger("awbotnest.ai")

_PERSISTED_FIELDS = (
    "total",
    "succeeded",
    "failed",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)


class AIUsageTracker:
    """Thread-safe, best-effort counters for logical AI requests."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._active = 0
        self._data = self._read()

    @staticmethod
    def _empty() -> dict[str, int]:
        return {field: 0 for field in _PERSISTED_FIELDS}

    @staticmethod
    def _counter(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _read(self) -> dict[str, int]:
        values = self._empty()
        if self.path is None:
            return values
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return values
            for field in _PERSISTED_FIELDS:
                values[field] = self._counter(raw.get(field))
        except (OSError, json.JSONDecodeError):
            pass
        return values

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            # Telemetry must never make an otherwise valid AI request fail.
            logger.warning("AI 使用统计保存失败：%s", type(exc).__name__)

    def begin(self) -> None:
        with self._lock:
            self._active += 1

    @classmethod
    def _tokens(cls, response: object) -> tuple[int, int, int]:
        if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
            return 0, 0, 0
        usage = response["usage"]
        input_tokens = cls._counter(usage.get("prompt_tokens", usage.get("input_tokens")))
        output_tokens = cls._counter(usage.get("completion_tokens", usage.get("output_tokens")))
        total_value = usage.get("total_tokens")
        total_tokens = cls._counter(total_value) if total_value is not None else input_tokens + output_tokens
        return input_tokens, output_tokens, total_tokens

    def succeed(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._data["total"] += 1
            self._data["succeeded"] += 1
            self._save()

    def fail(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._data["total"] += 1
            self._data["failed"] += 1
            self._save()

    def record_tokens(self, response: object) -> None:
        input_tokens, output_tokens, total_tokens = self._tokens(response)
        if not (input_tokens or output_tokens or total_tokens):
            return
        with self._lock:
            self._data["input_tokens"] += input_tokens
            self._data["output_tokens"] += output_tokens
            self._data["total_tokens"] += total_tokens

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {**self._data, "active": self._active}
