from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


logger = logging.getLogger("awbotnest.delivery")


@dataclass(slots=True)
class _PendingEdit:
    message: Any
    text: str
    kwargs: dict[str, Any]
    future: asyncio.Future[Any]
    retries: int | None
    flood_wait_limit: float | None
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _ChatLock:
    lock: asyncio.Lock
    users: int = 0


class TelegramDelivery:
    """Optional governed Telegram delivery scoped to one plugin instance."""

    def __init__(self, plugin_id: str, instance_id: str, *, retries: int = 2,
                 flood_wait_limit: float = 60, coalesce_window: float = 0.05) -> None:
        if retries < 0:
            raise ValueError("Telegram Delivery 重试次数不能小于 0")
        if flood_wait_limit < 0:
            raise ValueError("Telegram Delivery FloodWait 上限不能小于 0")
        if coalesce_window < 0:
            raise ValueError("Telegram Delivery 合并窗口不能小于 0")
        self.plugin_id = plugin_id
        self.instance_id = instance_id
        self.retries = int(retries)
        self.flood_wait_limit = float(flood_wait_limit)
        self.coalesce_window = float(coalesce_window)
        self._chat_locks: dict[tuple[int, str], _ChatLock] = {}
        self._pending_edits: dict[tuple[int, str, int], _PendingEdit] = {}
        self._last_edits: dict[tuple[int, str, int], str] = {}
        self._registry_lock = asyncio.Lock()
        self._workers: set[asyncio.Task[Any]] = set()
        self._active: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Telegram Delivery 已关闭")

    @staticmethod
    def _chat_key(client: Any, chat: Any) -> tuple[int, str]:
        return id(client), str(getattr(chat, "id", chat))

    @staticmethod
    def _message_client(message: Any) -> Any:
        return getattr(message, "_client", None) or getattr(message, "client", None) or message

    def _message_key(self, message: Any) -> tuple[int, str, int]:
        client = self._message_client(message)
        chat = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        return id(client), str(chat), int(message_id) if message_id is not None else id(message)

    @staticmethod
    def _message_text(message: Any) -> str | None:
        for name in ("message", "text", "raw_text"):
            value = getattr(message, name, None)
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _flood_wait_seconds(error: BaseException) -> float | None:
        if type(error).__name__ not in {"FloodWait", "FloodWaitError"}:
            return None
        value = getattr(error, "seconds", getattr(error, "value", None))
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    async def _retry(self, operation: Callable[[], Awaitable[Any]], *, retries: int | None,
                     flood_wait_limit: float | None) -> Any:
        retry_limit = self.retries if retries is None else max(0, int(retries))
        wait_limit = self.flood_wait_limit if flood_wait_limit is None else max(0, float(flood_wait_limit))
        attempts = 0
        while True:
            self._ensure_open()
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                seconds = self._flood_wait_seconds(exc)
                if seconds is None or attempts >= retry_limit or seconds > wait_limit:
                    raise
                attempts += 1
                logger.warning(
                    "[%s] Telegram FloodWait %.3f 秒，进行第 %d/%d 次重试",
                    self.plugin_id, seconds, attempts, retry_limit,
                )
                await asyncio.sleep(seconds)

    def _track_current(self) -> asyncio.Task[Any] | None:
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        return task

    @asynccontextmanager
    async def _ordered_chat(self, client: Any, chat: Any):
        """Retain a per-chat lock only while an operation owns or awaits it."""
        key = self._chat_key(client, chat)
        async with self._registry_lock:
            state = self._chat_locks.get(key)
            if state is None:
                state = _ChatLock(asyncio.Lock())
                self._chat_locks[key] = state
            state.users += 1
        try:
            async with state.lock:
                yield
        finally:
            async with self._registry_lock:
                state.users -= 1
                if state.users == 0 and self._chat_locks.get(key) is state:
                    self._chat_locks.pop(key, None)

    async def _wait_pending_edits(self, client: Any, chat: Any) -> None:
        """Keep a later send behind edits already queued for the same account/chat."""
        chat_key = self._chat_key(client, chat)
        async with self._registry_lock:
            futures = [item.future for key, item in self._pending_edits.items()
                       if key[:2] == chat_key]
        if futures:
            await asyncio.gather(*(asyncio.shield(future) for future in futures),
                                 return_exceptions=True)

    async def send(self, client: Any, chat: Any, text: str, *, retries: int | None = None,
                   flood_wait_limit: float | None = None, **kwargs: Any) -> Any:
        """Send one message while preserving order for this account/chat pair."""
        self._ensure_open()
        task = self._track_current()
        try:
            await self._wait_pending_edits(client, chat)
            async with self._ordered_chat(client, chat):
                return await self._retry(
                    lambda: client.send_message(chat, text, **kwargs),
                    retries=retries, flood_wait_limit=flood_wait_limit,
                )
        finally:
            if task is not None:
                self._active.discard(task)

    def _remember_edit(self, key: tuple[int, str, int], text: str) -> None:
        self._last_edits[key] = text
        if len(self._last_edits) > 1000:
            self._last_edits.pop(next(iter(self._last_edits)))

    async def _edit_now(self, message: Any, text: str, kwargs: dict[str, Any], *,
                        retries: int | None, flood_wait_limit: float | None) -> Any:
        key = self._message_key(message)
        if self._message_text(message) == text or self._last_edits.get(key) == text:
            return message
        client = self._message_client(message)
        async with self._ordered_chat(client, getattr(message, "chat_id", None)):
            if self._message_text(message) == text or self._last_edits.get(key) == text:
                return message
            result = await self._retry(
                lambda: message.edit(text, **kwargs),
                retries=retries, flood_wait_limit=flood_wait_limit,
            )
            self._remember_edit(key, text)
            return result

    async def edit(self, message: Any, text: str, *, coalesce: bool = True,
                   retries: int | None = None, flood_wait_limit: float | None = None,
                   **kwargs: Any) -> Any:
        """Edit a message with optional short-window coalescing and duplicate suppression."""
        self._ensure_open()
        task = self._track_current()
        try:
            if not coalesce or self.coalesce_window == 0:
                return await self._edit_now(
                    message, text, kwargs, retries=retries, flood_wait_limit=flood_wait_limit,
                )
            key = self._message_key(message)
            async with self._registry_lock:
                pending = self._pending_edits.get(key)
                if pending is None:
                    future = asyncio.get_running_loop().create_future()
                    # Retrieve unobserved errors even if every caller is cancelled.
                    future.add_done_callback(lambda value: None if value.cancelled() else value.exception())
                    pending = _PendingEdit(
                        message, text, dict(kwargs), future, retries, flood_wait_limit,
                    )
                    self._pending_edits[key] = pending
                    worker = asyncio.create_task(
                        self._flush_edit(key, pending),
                        name=f"delivery-edit:{self.instance_id}:{key[1]}:{key[2]}",
                    )
                    pending.task = worker
                    self._workers.add(worker)
                    worker.add_done_callback(self._workers.discard)
                else:
                    pending.message = message
                    pending.text = text
                    pending.kwargs = dict(kwargs)
                    pending.retries = retries
                    pending.flood_wait_limit = flood_wait_limit
                future = pending.future
            return await asyncio.shield(future)
        finally:
            if task is not None:
                self._active.discard(task)

    async def _flush_edit(self, key: tuple[int, str, int], pending: _PendingEdit) -> None:
        try:
            await asyncio.sleep(self.coalesce_window)
            async with self._registry_lock:
                if self._pending_edits.get(key) is not pending:
                    return
                self._pending_edits.pop(key, None)
                message, text, kwargs = pending.message, pending.text, pending.kwargs
            result = await self._edit_now(
                message, text, kwargs,
                retries=pending.retries, flood_wait_limit=pending.flood_wait_limit,
            )
            if not pending.future.done():
                pending.future.set_result(result)
        except asyncio.CancelledError:
            if not pending.future.done():
                pending.future.cancel()
            raise
        except Exception as exc:
            if not pending.future.done():
                pending.future.set_exception(exc)
        finally:
            async with self._registry_lock:
                if self._pending_edits.get(key) is pending:
                    self._pending_edits.pop(key, None)

    async def close(self) -> None:
        """Cancel this plugin instance's pending delivery operations."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._drain(), name=f"delivery-close:{self.instance_id}",
            )
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.gather(self._close_task, return_exceptions=True)
            raise

    async def _drain(self) -> None:
        current = asyncio.current_task()
        tasks = (self._workers | self._active) - {current}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._registry_lock:
            pending = list(self._pending_edits.values())
            self._pending_edits.clear()
        for item in pending:
            if not item.future.done():
                item.future.cancel()
        self._workers.clear()
        self._active.clear()
        self._chat_locks.clear()
        self._last_edits.clear()
