from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger("awbotnest.ai")

_PERSISTED_FIELDS = (
    "total",
    "succeeded",
    "failed",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)
_RECENT_LIMIT = 200


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

    def _read(self) -> dict[str, Any]:
        values = self._empty()
        values["recent"] = []
        if self.path is None:
            return values
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return values
            for field in _PERSISTED_FIELDS:
                values[field] = self._counter(raw.get(field))
            recent = raw.get("recent")
            if isinstance(recent, list):
                values["recent"] = [item for item in recent[-_RECENT_LIMIT:]
                                    if isinstance(item, dict)]
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

    def record_attempt(self, *, source: str, plugin_id: str, capability: str,
                       provider_id: str, provider: str, model: str, protocol: str,
                       succeeded: bool, latency_ms: int, response: object = None,
                       error_type: str = "", error_message: str = "",
                       used_fallback: bool = False) -> None:
        input_tokens, output_tokens, total_tokens = self._tokens(response)
        item = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": source,
            "plugin_id": plugin_id,
            "capability": capability,
            "provider_id": provider_id,
            "provider": provider,
            "model": model,
            "protocol": protocol,
            "status": "success" if succeeded else "failed",
            "latency_ms": max(0, int(latency_ms)),
            "error_type": str(error_type or ""),
            "error_message": str(error_message or "")[:300],
            "used_fallback": bool(used_fallback),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        with self._lock:
            self._data.setdefault("recent", []).append(item)
            del self._data["recent"][:-_RECENT_LIMIT]
            self._save()

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in reversed(self._data.get("recent", []))][:max(1, min(limit, 200))]

    def _plugin_summary(self, recent: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for item in recent:
            plugin_id = str(item.get("plugin_id") or "")
            if not plugin_id:
                continue
            group = groups.setdefault(plugin_id, {
                "plugin_id": plugin_id,
                "name": str(item.get("source") or plugin_id).removeprefix("插件:"),
                "calls": 0, "succeeded": 0, "failed": 0,
                "fallbacks": 0, "latency_total": 0, "total_tokens": 0,
            })
            group["calls"] += 1
            group["succeeded" if item.get("status") == "success" else "failed"] += 1
            group["fallbacks"] += int(bool(item.get("used_fallback")))
            group["latency_total"] += self._counter(item.get("latency_ms"))
            group["total_tokens"] += self._counter(item.get("total_tokens"))
        result = []
        for group in groups.values():
            calls = max(1, int(group.pop("calls")))
            latency_total = int(group.pop("latency_total"))
            group["calls"] = calls
            group["avg_latency_ms"] = round(latency_total / calls)
            result.append(group)
        return sorted(result, key=lambda item: (-int(item["calls"]), str(item["name"])))

    def plugin_summary(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._plugin_summary(self._data.get("recent", []))

    def overview(self, limit: int = 20) -> dict[str, Any]:
        """Return recent calls and their plugin aggregation from one snapshot."""
        safe_limit = max(1, min(limit, _RECENT_LIMIT))
        with self._lock:
            recent = self._data.get("recent", [])
            return {
                "items": [dict(item) for item in reversed(recent[-safe_limit:])],
                "plugins": self._plugin_summary(recent),
                "total_items": len(recent),
            }

    def clear_recent(self) -> int:
        with self._lock:
            removed = len(self._data.get("recent", []))
            self._data["recent"] = []
            self._save()
            return removed

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {field: self._counter(self._data.get(field)) for field in _PERSISTED_FIELDS} | {
                "active": self._active,
            }
