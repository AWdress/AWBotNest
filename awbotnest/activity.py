from __future__ import annotations

import json
import time
import threading
from collections import Counter
from pathlib import Path

from .config import DATA_DIR


class ActivityTracker:
    def __init__(self, path: Path = DATA_DIR / "activity.json") -> None:
        self.path = path
        self._data = self._read()
        self._last_save = 0.0
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, int]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def record(self, plugin_id: str, success: bool) -> None:
        with self._lock:
            bucket = str(int(time.time() // 3600) * 3600)
            values = self._data.setdefault(bucket, {})
            key = f"{plugin_id}:success" if success else f"{plugin_id}:total"
            values[key] = int(values.get(key, 0)) + 1
            cutoff = int(time.time()) - 8 * 24 * 3600
            self._data = {key: value for key, value in self._data.items() if int(key) >= cutoff}
            if time.monotonic() - self._last_save < 10:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
            self._last_save = time.monotonic()

    def timeline(self, hours: int = 24) -> dict[str, object]:
        hours = max(1, min(int(hours), 168))
        with self._lock:
            data = {key: dict(value) for key, value in self._data.items()}
        current = int(time.time() // 3600) * 3600
        buckets = []
        totals: Counter[str] = Counter()
        successes: Counter[str] = Counter()
        for offset in reversed(range(hours)):
            stamp = current - offset * 3600
            values = data.get(str(stamp), {})
            counts: dict[str, int] = {}
            for key, count in values.items():
                plugin_id, kind = key.rsplit(":", 1)
                if kind == "total":
                    counts[plugin_id] = int(count)
                    totals[plugin_id] += int(count)
                else:
                    successes[plugin_id] += int(count)
            buckets.append({"time": stamp, "counts": counts})
        return {"buckets": buckets, "totals": dict(totals), "successes": dict(successes)}

    def flush(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
            self._last_save = time.monotonic()


activity = ActivityTracker()
