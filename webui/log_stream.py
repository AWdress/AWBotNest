"""
webui/log_stream.py
日志流：把平台 logger 的输出接到一个环形缓冲 + WebSocket 广播，
供前端「运行日志」页实时查看。

实现：
- LogStreamHandler 挂到 "main" / "error" logger 上，emit 时把记录格式化为 dict，
  存入环形缓冲（最近 N 条），并推送给所有已连接的 WebSocket 订阅者。
- 日志可能来自任意线程，跨线程投递用 loop.call_soon_threadsafe。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

# 内存和磁盘都只保留最近记录，避免历史文件无限增长。
_BUFFER_LIMIT = 1000
_HISTORY_MAX_BYTES = 5 * 1024 * 1024
_COMPACT_EVERY_WRITES = 200
_HISTORY_FILE = Path("logs") / "webui_history.jsonl"
_BUFFER: deque[dict] = deque(maxlen=_BUFFER_LIMIT)
_HISTORY_LOCK = threading.RLock()
_HISTORY_LOADED = False
_WRITES_SINCE_COMPACT = 0
# 订阅者队列集合（每个 WebSocket 一个）
_SUBSCRIBERS: set[asyncio.Queue] = set()
# Web 服务的事件循环（启动时设置）
_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """记录 Web 服务事件循环，供跨线程投递"""
    global _LOOP
    _LOOP = loop


def _record_to_dict(record: logging.LogRecord) -> dict:
    occurred = datetime.fromtimestamp(record.created).astimezone()
    source = getattr(record, "plugin", None) or record.module
    return {
        "timestamp": occurred.isoformat(timespec="seconds"),
        "date": occurred.strftime("%Y-%m-%d"),
        "time": occurred.strftime("%H:%M:%S"),
        "level": record.levelname,
        "name": record.name,
        # 提取插件名：plugin context logger 会带 plugin 字段（structlog 不一定有，兜底用 module）
        "source": str(source),
        "msg": record.getMessage(),
    }


def _is_history_item(value) -> bool:
    """只恢复前端需要的日志字段，跳过损坏或被手工改坏的行。"""
    return (
        isinstance(value, dict)
        and isinstance(value.get("time"), str)
        and isinstance(value.get("level"), str)
        and isinstance(value.get("source"), str)
        and isinstance(value.get("msg"), str)
    )


def _compact_history_locked() -> None:
    """用当前内存快照覆盖历史文件；调用方必须持有锁。"""
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _HISTORY_FILE.with_suffix(_HISTORY_FILE.suffix + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            for item in _BUFFER:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temp_path, _HISTORY_FILE)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _load_history() -> None:
    """启动时恢复最近的结构化日志，重复调用不会重复装载。"""
    global _HISTORY_LOADED
    with _HISTORY_LOCK:
        if _HISTORY_LOADED:
            return
        _HISTORY_LOADED = True
        if not _HISTORY_FILE.is_file():
            return
        try:
            with _HISTORY_FILE.open("r", encoding="utf-8", errors="replace") as stream:
                recent_lines = deque(stream, maxlen=_BUFFER_LIMIT)
            for line in recent_lines:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if _is_history_item(item):
                    _BUFFER.append(item)
            if _HISTORY_FILE.stat().st_size > _HISTORY_MAX_BYTES:
                _compact_history_locked()
        except OSError:
            return


def _persist(item: dict) -> None:
    """追加一条结构化历史；失败时静默退回内存日志。"""
    global _WRITES_SINCE_COMPACT
    with _HISTORY_LOCK:
        try:
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _HISTORY_FILE.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            _WRITES_SINCE_COMPACT += 1
            should_compact = _WRITES_SINCE_COMPACT >= _COMPACT_EVERY_WRITES
            if not should_compact:
                should_compact = _HISTORY_FILE.stat().st_size > _HISTORY_MAX_BYTES
            if should_compact:
                _compact_history_locked()
                _WRITES_SINCE_COMPACT = 0
        except OSError:
            pass


def _broadcast(item: dict) -> None:
    """投递到所有订阅者（在事件循环线程内执行）"""
    for q in list(_SUBSCRIBERS):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass  # 订阅者太慢，丢弃


class LogStreamHandler(logging.Handler):
    """把日志记录推入缓冲并广播"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = _record_to_dict(record)
        except Exception:  # noqa: BLE001
            return
        with _HISTORY_LOCK:
            _BUFFER.append(item)
            _persist(item)
        if _LOOP is not None and _SUBSCRIBERS:
            try:
                _LOOP.call_soon_threadsafe(_broadcast, item)
            except RuntimeError:
                pass  # 循环已关闭


def recent_logs() -> list[dict]:
    """返回缓冲中的历史日志（前端连接时先拉一批）"""
    _load_history()
    with _HISTORY_LOCK:
        return list(_BUFFER)


def trim_history(keep_lines: int) -> bool:
    """同步清理内存和磁盘历史，防止旧记录在下次写入或重启后回来。"""
    keep_lines = max(1, int(keep_lines))
    _load_history()
    with _HISTORY_LOCK:
        if len(_BUFFER) <= keep_lines:
            return False
        retained = list(_BUFFER)[-keep_lines:]
        _BUFFER.clear()
        _BUFFER.extend(retained)
        try:
            _compact_history_locked()
        except OSError:
            return False
        return True


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _SUBSCRIBERS.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _SUBSCRIBERS.discard(q)


def install() -> None:
    """把流式 handler 挂到平台 logger 上（幂等）"""
    _load_history()
    handler = LogStreamHandler()
    handler.setLevel(logging.INFO)  # 与主 logger 一致，不推 DEBUG 噪音到前端
    for name in ("main", "error"):
        lg = logging.getLogger(name)
        # 避免重复安装
        if not any(isinstance(h, LogStreamHandler) for h in lg.handlers):
            lg.addHandler(handler)
