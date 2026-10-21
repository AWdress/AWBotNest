from __future__ import annotations

import platform
import logging
import gc
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import psutil


_MIB = 1024 * 1024
_MEMORY_WARNING_RATIO = 0.85
_MEMORY_RECOVERY_RATIO = 0.75
_MEMORY_WARNING_INTERVAL = 5 * 60
logger = logging.getLogger("awbotnest.resources")


def _trim_idle_allocator() -> None:
    """Return unused glibc arenas, when this process exposes malloc_trim.

    This does not unload modules, evict live objects or clear filesystem caches.
    Other Linux allocators may not provide the optional symbol at all.
    """
    import ctypes

    trim = getattr(ctypes.CDLL(None), "malloc_trim", None)
    if trim is not None:
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)


class IdleMemoryReclaimer:
    """Best-effort reclamation after tracked work becomes globally idle.

    Work never acquires the collection lock. A task starting during collection
    proceeds normally, and causes the optional allocator trim to be skipped.
    """

    def __init__(self, *, idle_seconds: float = 15.0,
                 cooldown_seconds: float = 60.0,
                 max_wait_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 collector: Callable[[int], object] | None = None,
                 trimmer: Callable[[], object] | None = None) -> None:
        self._idle_seconds = max(0.0, float(idle_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._max_wait_seconds = max(0.0, float(max_wait_seconds))
        self._clock = clock
        self._collector = gc.collect if collector is None else collector
        self._trimmer = _trim_idle_allocator if trimmer is None else trimmer
        self._state_lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._active = 0
        self._pending = False
        self._pending_since: float | None = None
        self._last_activity: float | None = None
        self._last_reclaim: float | None = None
        self._epoch = 0

    @contextmanager
    def track(self) -> Iterator[None]:
        """Track actual work, including nesting and exception/cancel cleanup."""
        with self._state_lock:
            self._active += 1
            self._epoch += 1
        try:
            yield
        finally:
            with self._state_lock:
                self._active -= 1
                self._request_locked()

    def _request_locked(self) -> None:
        self._epoch += 1
        self._pending = True
        try:
            now = self._clock()
            self._last_activity = now
            # Short callbacks must not keep moving the oldest pending request.
            if self._pending_since is None:
                self._pending_since = now
        except Exception:
            # Maintenance must not replace a task's return/exception.
            self._last_activity = None

    def request(self) -> None:
        """Signal completed work without keeping permanent background loops active."""
        with self._state_lock:
            self._request_locked()

    def next_check_delay(self) -> float:
        """Retry overdue requests off the normal thirty-second phase."""
        try:
            with self._state_lock:
                now = self._clock()
                if (self._pending and self._pending_since is not None
                        and now - self._pending_since >= self._max_wait_seconds
                        and (self._last_reclaim is None
                             or now - self._last_reclaim >= self._cooldown_seconds)):
                    return 1.0
        except Exception:
            pass
        return 30.0

    def reclaim_if_idle(self) -> bool:
        """Run at most one idle collection; errors never affect tracked work."""
        if not self._collect_lock.acquire(blocking=False):
            return False
        attempted = False
        try:
            with self._state_lock:
                now = self._clock()
                if self._pending and (self._last_activity is None or self._pending_since is None):
                    if self._last_activity is None:
                        self._last_activity = now
                    if self._pending_since is None:
                        self._pending_since = now
                    return False
                if (self._active or not self._pending
                        or (now - self._last_activity < self._idle_seconds
                            and now - self._pending_since < self._max_wait_seconds)
                        or self._last_reclaim is not None
                        and now - self._last_reclaim < self._cooldown_seconds):
                    return False
                epoch = self._epoch
                self._pending = False
                self._pending_since = None
                self._last_reclaim = now
                attempted = True
            # Neither the state lock nor a plugin execution permit is held during GC.
            self._collector(2)
            with self._state_lock:
                still_idle = not self._active and self._epoch == epoch
            if still_idle and platform.system() == "Linux":
                self._trimmer()
            return True
        except Exception:
            if attempted:
                with self._state_lock:
                    # Retry at most once per cooldown, including trim failures.
                    self._request_locked()
            return False
        finally:
            self._collect_lock.release()


memory_reclaimer = IdleMemoryReclaimer()


def _read_positive_int(path: Path, *, allow_zero: bool = False) -> int | None:
    """Read a positive cgroup byte counter, ignoring unlimited values."""
    try:
        value = path.read_text(encoding="ascii").strip()
        if value == "max":
            return None
        parsed = int(value)
        # Cgroup v1 commonly represents "unlimited" with a huge sentinel.
        return parsed if (0 if allow_zero else 1) <= parsed < (1 << 60) else None
    except (OSError, ValueError):
        return None


def _linux_cgroup_memory() -> tuple[int, int | None] | None:
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
        usage = _read_positive_int(usage_path, allow_zero=True)
        limit = _read_positive_int(limit_path)
        if usage is not None:
            return usage, limit
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
            if cgroup is not None:
                used_bytes = cgroup[0]
                if cgroup[1] is not None:
                    limit_bytes = min(cgroup[1], limit_bytes)

        return {
            "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
            "memory_used_mb": round(used_bytes / _MIB, 1),
            "memory_limit_mb": round(limit_bytes / _MIB, 1),
        }


class MemoryPressureMonitor:
    """Report sustained memory pressure without changing or stopping workloads."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pressured = False
        self._last_warning: float | None = None

    @staticmethod
    def _process_usage() -> tuple[int | None, int, int, int, list[tuple[str, int]]]:
        parent_rss = None
        child_rss = 0
        readable = 0
        largest: list[tuple[str, int]] = []
        try:
            parent = psutil.Process()
        except (psutil.Error, OSError):
            return None, 0, 0, 0, []
        try:
            parent_rss = max(0, int(parent.memory_info().rss))
        except (psutil.Error, OSError, ValueError, TypeError):
            pass
        try:
            children = parent.children(recursive=True)
        except (psutil.Error, OSError):
            return parent_rss, 0, 0, 0, []
        for child in children:
            try:
                rss = max(0, int(child.memory_info().rss))
            except (psutil.Error, OSError, ValueError, TypeError):
                continue
            child_rss += rss
            readable += 1
            try:
                # Never inspect command lines, executable paths or environments.
                raw_name = child.name()
                name = raw_name if re.fullmatch(r"[\w.-]{1,40}", raw_name) else "unknown"
            except (psutil.Error, OSError, ValueError, TypeError):
                name = "unknown"
            largest.append((name, rss))
            largest.sort(key=lambda item: item[1], reverse=True)
            del largest[3:]
        return parent_rss, child_rss, len(children), readable, largest

    def check(self) -> bool:
        """Return whether a warning was emitted; sampling failures are non-fatal."""
        try:
            with self._lock:
                memory = psutil.virtual_memory()
                total = int(memory.total)
                if total <= 0:
                    return False
                scope = "主机"
                used = max(0, total - max(0, min(int(memory.available), total)))
                limit = total
                if platform.system() == "Linux":
                    cgroup = _linux_cgroup_memory()
                    if cgroup is not None and cgroup[1] is not None:
                        used, limit = cgroup
                        scope = "容器"
                if limit <= 0:
                    return False
                ratio = used / limit
                if ratio < _MEMORY_RECOVERY_RATIO:
                    self._pressured = False
                    self._last_warning = None
                if ratio < _MEMORY_WARNING_RATIO:
                    return False
                now = self._clock()
                if (self._pressured and self._last_warning is not None
                        and now - self._last_warning < _MEMORY_WARNING_INTERVAL):
                    return False
                parent_rss, child_rss, children, readable, largest = self._process_usage()
                parent_text = "无法读取" if parent_rss is None else f"{parent_rss / _MIB:.1f} MiB"
                largest_text = "、".join(f"{name} {rss / _MIB:.1f} MiB" for name, rss in largest) or "无"
                logger.warning(
                    "内存占用偏高：%s %.1f/%.1f MiB（%.1f%%），主进程 RSS %s，"
                    "子进程 RSS 合计 %.1f MiB（%d 个，可读取 %d 个）；子进程占用前三：%s",
                    scope, used / _MIB, limit / _MIB, ratio * 100, parent_text,
                    child_rss / _MIB, children, readable, largest_text,
                )
                self._pressured = True
                self._last_warning = now
                return True
        except Exception:
            # Diagnostics must never interfere with accounts, plugins or shutdown.
            return False
