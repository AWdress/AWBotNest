"""Cancellation-safe execution of synchronous plugin callbacks."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from .scheduler import waiting_phase


async def run_sync(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    # Cancelling an asyncio waiter cannot stop its native worker. Retain task
    # ownership and concurrency permits until that worker really exits.
    task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with waiting_phase("等待后台线程退出"):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
        if not task.cancelled() and task.exception() is None:
            result = task.result()
            if inspect.iscoroutine(result):
                result.close()
        raise
