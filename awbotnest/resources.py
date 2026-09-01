from __future__ import annotations

import platform
from pathlib import Path

import psutil


_MIB = 1024 * 1024


def _read_positive_int(path: Path) -> int | None:
    """Read a positive cgroup byte counter, ignoring unlimited values."""
    try:
        value = path.read_text(encoding="ascii").strip()
        if value == "max":
            return None
        parsed = int(value)
        # Cgroup v1 commonly represents "unlimited" with a huge sentinel.
        return parsed if 0 < parsed < (1 << 60) else None
    except (OSError, ValueError):
        return None


def _linux_cgroup_memory() -> tuple[int, int] | None:
    candidates = (
        (
            Path("/sys/fs/cgroup/memory.current"),
            Path("/sys/fs/cgroup/memory.max"),
        ),
        (
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        ),
    )
    for usage_path, limit_path in candidates:
        usage = _read_positive_int(usage_path)
        limit = _read_positive_int(limit_path)
        if usage is not None and limit is not None:
            return min(usage, limit), limit
    return None


class ResourceSampler:
    """Collect host resource usage consistently on Windows and Linux."""

    def __init__(self) -> None:
        # Prime psutil's non-blocking CPU counter so later requests represent the
        # interval since the previous sample instead of always returning 0.0.
        psutil.cpu_percent(interval=None)

    def snapshot(self) -> dict[str, float]:
        memory = psutil.virtual_memory()
        used_bytes = int(memory.used)
        limit_bytes = int(memory.total)

        if platform.system() == "Linux":
            cgroup = _linux_cgroup_memory()
            if cgroup is not None and cgroup[1] < limit_bytes:
                used_bytes, limit_bytes = cgroup

        return {
            "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
            "memory_used_mb": round(used_bytes / _MIB, 1),
            "memory_limit_mb": round(limit_bytes / _MIB, 1),
        }
