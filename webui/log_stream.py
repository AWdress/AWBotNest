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
import logging
from collections import deque
from datetime import datetime
from typing import Optional

# 环形缓冲：保留最近 500 条
_BUFFER: deque[dict] = deque(maxlen=500)
# 订阅者队列集合（每个 WebSocket 一个）
_SUBSCRIBERS: set[asyncio.Queue] = set()
# Web 服务的事件循环（启动时设置）
_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """记录 Web 服务事件循环，供跨线程投递"""
    global _LOOP
    _LOOP = loop


def _record_to_dict(record: logging.LogRecord) -> dict:
    return {
        "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
        "level": record.levelname,
        "name": record.name,
        # 提取插件名：plugin context logger 会带 plugin 字段（structlog 不一定有，兜底用 module）
        "source": getattr(record, "plugin", None) or record.module,
        "msg": record.getMessage(),
    }


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
        _BUFFER.append(item)
        if _LOOP is not None and _SUBSCRIBERS:
            try:
                _LOOP.call_soon_threadsafe(_broadcast, item)
            except RuntimeError:
                pass  # 循环已关闭


def recent_logs() -> list[dict]:
    """返回缓冲中的历史日志（前端连接时先拉一批）"""
    return list(_BUFFER)


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _SUBSCRIBERS.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _SUBSCRIBERS.discard(q)


def install() -> None:
    """把流式 handler 挂到平台 logger 上（幂等）"""
    handler = LogStreamHandler()
    handler.setLevel(logging.INFO)  # 与主 logger 一致，不推 DEBUG 噪音到前端
    for name in ("main", "error"):
        lg = logging.getLogger(name)
        # 避免重复安装
        if not any(isinstance(h, LogStreamHandler) for h in lg.handlers):
            lg.addHandler(handler)
