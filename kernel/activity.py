"""
kernel/activity.py
插件活跃度统计 —— 按时间桶累计每个插件的触发次数与成功次数，
供状态页「插件活跃时间线」与「活跃占比」展示。

- 插件出站动作开始时调用 record()，成功返回后调用 record_success()。
- 环形窗口保留最近 7 天的小时桶，状态页可按 24 小时或 7 天查看。
- 持久化到 data/activity.json：导入时加载、record 后节流落盘，平台重启不丢历史。
- 线程安全：record 通常在事件循环线程触发，timeline 在 Web 请求线程读取。
"""
from __future__ import annotations

import json
import math
import time
import threading
import contextvars
from pathlib import Path
from collections import OrderedDict, Counter

BUCKET_SECONDS = 3600   # 每个时间桶的跨度：1 小时
BUCKETS = 24 * 7        # 保留的桶数：近 7 天
_STATE_PATH = Path("data") / "activity.json"
_SAVE_MIN_INTERVAL = 10  # 落盘最小间隔（秒），节流防频繁写盘

_lock = threading.Lock()
# bucket_start_epoch -> Counter(plugin_id -> count)
_data: "OrderedDict[int, Counter]" = OrderedDict()
_success_data: "OrderedDict[int, Counter]" = OrderedDict()
_last_save = 0.0

# 当前正在执行 handler 的插件 id（contextvar，随 async 任务上下文传播）。
# 由 ctx._track 在进入插件 handler 前设置，使该 handler 内部的出站发送能归属到本插件。
_current_plugin: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "current_plugin", default=None
)


def set_current(plugin_id: str):
    """进入插件 handler 前调用，返回 token 供 reset。"""
    return _current_plugin.set(plugin_id)


def reset_current(token) -> None:
    try:
        _current_plugin.reset(token)
    except Exception:  # noqa: BLE001
        pass


def record_current(n: int = 1) -> None:
    """记一次「当前插件的出站动作」（发消息/回复/编辑时调用）。无当前插件则忽略。"""
    pid = _current_plugin.get()
    if pid:
        record(pid, n)


def record_success_current(n: int = 1) -> None:
    """记一次成功完成的当前插件出站动作。无当前插件则忽略。"""
    pid = _current_plugin.get()
    if pid:
        record_success(pid, n)


def _bucket_of(ts: float) -> int:
    return int(ts // BUCKET_SECONDS) * BUCKET_SECONDS


def _trim(now_bucket: int) -> None:
    oldest = now_bucket - (BUCKETS - 1) * BUCKET_SECONDS
    for store in (_data, _success_data):
        for k in list(store.keys()):
            if k < oldest:
                store.pop(k, None)


def _load() -> None:
    """导入时从磁盘恢复，并按当前时间裁掉过期桶。"""
    if not _STATE_PATH.exists():
        return
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key, store in (("buckets", _data), ("success_buckets", _success_data)):
        for k, counts in (raw.get(key) or {}).items():
            try:
                store[int(k)] = Counter(counts)
            except (ValueError, TypeError):
                continue
        # 按桶时间排序，保持有序
        for k in sorted(store.keys()):
            store.move_to_end(k)
    _trim(_bucket_of(time.time()))


def _save(force: bool = False) -> None:
    """节流落盘：距上次保存超过 _SAVE_MIN_INTERVAL 秒，或 force。须在持锁状态调用。"""
    global _last_save
    now = time.time()
    if not force and (now - _last_save) < _SAVE_MIN_INTERVAL:
        return
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "buckets": {str(k): dict(c) for k, c in _data.items()},
            "success_buckets": {str(k): dict(c) for k, c in _success_data.items()},
        }
        _STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _last_save = now
    except OSError:
        pass


def _record(store: "OrderedDict[int, Counter]", plugin_id: str, n: int) -> None:
    b = _bucket_of(time.time())
    with _lock:
        c = store.get(b)
        if c is None:
            c = Counter()
            store[b] = c
        c[plugin_id] += n
        _trim(b)
        _save()


def record(plugin_id: str, n: int = 1) -> None:
    """记一次插件出站动作。"""
    _record(_data, plugin_id, n)


def record_success(plugin_id: str, n: int = 1) -> None:
    """记一次成功完成的插件出站动作。"""
    _record(_success_data, plugin_id, n)


def timeline(hours: int = 24, group_hours: int = 1) -> dict:
    """
    返回指定时间范围的活跃数据 + 各插件总计。
    buckets 按时间升序（最旧 → 最新），缺失的桶补空，便于前端等宽渲染。
    """
    hours = max(1, min(int(hours), BUCKETS))
    group_hours = max(1, min(int(group_hours), hours))
    now_b = _bucket_of(time.time())
    with _lock:
        buckets = []
        totals: Counter = Counter()
        success_totals: Counter = Counter()
        group_count = math.ceil(hours / group_hours)
        if group_hours == 24:
            local_now = time.localtime()
            current_day = int(time.mktime((
                local_now.tm_year, local_now.tm_mon, local_now.tm_mday,
                0, 0, 0, local_now.tm_wday, local_now.tm_yday, local_now.tm_isdst,
            )))
            oldest = current_day - (group_count - 1) * 24 * BUCKET_SECONDS
        else:
            oldest = now_b - (hours - 1) * BUCKET_SECONDS
        for offset in range(0, group_count * group_hours, group_hours):
            grouped: Counter = Counter()
            success_grouped: Counter = Counter()
            start = oldest + offset * BUCKET_SECONDS
            for index in range(group_hours):
                bucket = start + index * BUCKET_SECONDS
                if bucket > now_b:
                    break
                counts = _data.get(bucket)
                if counts:
                    grouped.update(counts)
                success_counts = _success_data.get(bucket)
                if success_counts:
                    success_grouped.update(success_counts)
            buckets.append({
                "t": start,
                "counts": dict(grouped),
                "success_counts": dict(success_grouped),
            })
            totals.update(grouped)
            success_totals.update(success_grouped)
        return {
            "bucket_seconds": BUCKET_SECONDS * group_hours,
            "buckets": buckets,
            "totals": dict(totals),
            "success_totals": dict(success_totals),
        }


def reset() -> None:
    """清空统计（测试 / 重置用）。"""
    with _lock:
        _data.clear()
        _success_data.clear()
        _save(force=True)


# 导入时恢复历史
_load()
