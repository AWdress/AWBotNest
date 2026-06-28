"""
kernel/activity.py
插件活跃度统计 —— 按时间桶累计每个插件被触发的次数，
供状态页「插件活跃时间线」与「活跃占比」展示。

- 通过 ctx.on_message / ctx.on_callback 注册的处理器，每次触发时调用 record()。
- 环形窗口仅保留最近 BUCKETS 个时间桶（默认 24 个 1 小时桶 = 近 24 小时）。
- 持久化到 data/activity.json：导入时加载、record 后节流落盘，平台重启不丢历史。
- 线程安全：record 通常在事件循环线程触发，timeline 在 Web 请求线程读取。
"""
from __future__ import annotations

import json
import time
import threading
import contextvars
from pathlib import Path
from collections import OrderedDict, Counter

BUCKET_SECONDS = 3600   # 每个时间桶的跨度：1 小时
BUCKETS = 24            # 保留的桶数：近 24 小时
_STATE_PATH = Path("data") / "activity.json"
_SAVE_MIN_INTERVAL = 10  # 落盘最小间隔（秒），节流防频繁写盘

_lock = threading.Lock()
# bucket_start_epoch -> Counter(plugin_id -> count)
_data: "OrderedDict[int, Counter]" = OrderedDict()
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



def _bucket_of(ts: float) -> int:
    return int(ts // BUCKET_SECONDS) * BUCKET_SECONDS


def _trim(now_bucket: int) -> None:
    oldest = now_bucket - (BUCKETS - 1) * BUCKET_SECONDS
    for k in list(_data.keys()):
        if k < oldest:
            _data.pop(k, None)


def _load() -> None:
    """导入时从磁盘恢复，并按当前时间裁掉过期桶。"""
    if not _STATE_PATH.exists():
        return
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for k, counts in (raw.get("buckets") or {}).items():
        try:
            _data[int(k)] = Counter(counts)
        except (ValueError, TypeError):
            continue
    # 按桶时间排序，保持有序
    for k in sorted(_data.keys()):
        _data.move_to_end(k)
    _trim(_bucket_of(time.time()))


def _save(force: bool = False) -> None:
    """节流落盘：距上次保存超过 _SAVE_MIN_INTERVAL 秒，或 force。须在持锁状态调用。"""
    global _last_save
    now = time.time()
    if not force and (now - _last_save) < _SAVE_MIN_INTERVAL:
        return
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"buckets": {str(k): dict(c) for k, c in _data.items()}}
        _STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _last_save = now
    except OSError:
        pass


def record(plugin_id: str, n: int = 1) -> None:
    """记一次插件活跃（处理器被触发）。"""
    b = _bucket_of(time.time())
    with _lock:
        c = _data.get(b)
        if c is None:
            c = Counter()
            _data[b] = c
        c[plugin_id] += n
        _trim(b)
        _save()


def timeline() -> dict:
    """
    返回近 BUCKETS 个时间桶的活跃数据 + 各插件总计。
    buckets 按时间升序（最旧 → 最新），缺失的桶补空，便于前端等宽渲染。
    """
    now_b = _bucket_of(time.time())
    with _lock:
        buckets = []
        totals: Counter = Counter()
        for i in range(BUCKETS - 1, -1, -1):
            b = now_b - i * BUCKET_SECONDS
            c = _data.get(b)
            counts = dict(c) if c else {}
            buckets.append({"t": b, "counts": counts})
            if c:
                totals.update(c)
        return {
            "bucket_seconds": BUCKET_SECONDS,
            "buckets": buckets,
            "totals": dict(totals),
        }


def reset() -> None:
    """清空统计（测试 / 重置用）。"""
    with _lock:
        _data.clear()
        _save(force=True)


# 导入时恢复历史
_load()
