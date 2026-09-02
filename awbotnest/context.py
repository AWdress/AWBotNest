from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import TelegramClient, events

from .telegram import TelegramAccounts
from .scheduler import PluginScheduler
from .storage import PluginKV
from .config import DATA_DIR, Settings, save_settings
from .services import PlatformServices, PluginAI
from .routing import PluginRoutes
from .notifier import NotificationService
from .activity import activity, set_current, reset_current, record_current

EventCallback = Callable[[Any], Awaitable[Any]]


class PluginContext:
    """Telethon 原生的插件能力入口。"""

    def __init__(self, plugin_id: str, scope: str, accounts: TelegramAccounts,
                 scheduler: PluginScheduler, settings: Settings, services: PlatformServices,
                 routes: PluginRoutes, notifier: NotificationService, bot_id: str = "",
                 resources: dict[str, object] | None = None) -> None:
        self.plugin_id = plugin_id
        self.scope = scope
        self.accounts = accounts
        self.scheduler = scheduler
        self.settings = settings
        self.bot_id = bot_id
        self.kv = PluginKV(plugin_id)
        self.data_dir = DATA_DIR / "plugins" / plugin_id
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.services = services
        self._plugin_ai = PluginAI(services.ai, plugin_id, self.data_dir)
        self.routes = routes
        self.notifier = notifier
        policy = resources or {}
        self.timeout = max(1, min(float(policy.get("timeout_seconds", 120)), 1800))
        self.failure_threshold = max(1, min(int(policy.get("failure_threshold", 5)), 100))
        self.max_tasks = max(1, min(int(policy.get("max_background_tasks", 32)), 500))
        self.max_concurrency = max(1, min(int(policy.get("max_concurrency", 8)), 100))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self.log = logging.getLogger(f"awbotnest.plugin.{plugin_id}")
        self._handlers: list[tuple[TelegramClient, EventCallback, object]] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def bot(self) -> TelegramClient | None:
        return self.accounts.choose_bot(self.bot_id)

    @property
    def users(self) -> list[TelegramClient]:
        selected = self.settings.plugin_accounts.get(self.plugin_id, [])
        return [client for name, client in self.accounts.users.items()
                if client.is_connected() and (not selected or name in selected)]

    @property
    def config(self) -> dict[str, object]:
        return dict(self.settings.plugin_config.get(self.plugin_id, {}))

    @property
    def http(self):
        return self._services.http

    @property
    def cookies(self):
        return self._services.cookies

    @property
    def browser(self):
        return self._services.browser

    @property
    def ai(self):
        return self._plugin_ai

    @property
    def _services(self) -> PlatformServices:
        if self.services is None:
            raise RuntimeError("平台服务尚未初始化")
        return self.services

    def update_config(self, values: dict[str, object]) -> dict[str, object]:
        current = self.settings.plugin_config.setdefault(self.plugin_id, {})
        current.update(values)
        save_settings(self.settings)
        return dict(current)

    def _register(self, builder: object, callback: EventCallback) -> EventCallback:
        clients = self.accounts.clients_for_scope(self.scope, self.bot_id)
        if self.settings.plugin_accounts.get(self.plugin_id):
            user_clients = list(self.accounts.users.values())
            allowed = self.users
            clients = [client for client in clients if client not in user_clients or client in allowed]
        if not clients and self.scope != "standalone":
            raise RuntimeError(f"插件 {self.plugin_id} 没有可用的 {self.scope} Telegram 客户端")
        failures = 0
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            nonlocal failures
            event = args[0] if args else None
            raw_id = getattr(event, "id", "") or getattr(getattr(event, "message", None), "id", "")
            chat_id = getattr(event, "chat_id", "") or getattr(getattr(event, "message", None), "chat_id", "")
            event_id = f"{chat_id}:{raw_id}" if raw_id else f"task:{id(asyncio.current_task())}"
            token = set_current(self.plugin_id, event_id)
            try:
                async with self._semaphore:
                    value = callback(*args, **kwargs)
                    result = await asyncio.wait_for(value, timeout=self.timeout) if isinstance(value, Awaitable) else value
                failures = 0
                return result
            except Exception:
                failures += 1
                self.log.exception("事件处理失败（连续 %s 次）", failures)
                if failures >= self.failure_threshold:
                    for registered_client, registered_callback, registered_builder in list(self._handlers):
                        if registered_callback is guarded:
                            registered_client.remove_event_handler(registered_callback, registered_builder)
                            self._handlers.remove((registered_client, registered_callback, registered_builder))
                    self.log.error("事件处理连续失败 %s 次，已触发熔断", self.failure_threshold)
            finally:
                reset_current(token)
        for client in clients:
            client.add_event_handler(guarded, builder)
            self._handlers.append((client, guarded, builder))
        return callback

    def on_message(self, *, pattern: str | None = None, chats: object = None,
                   incoming: bool = True, outgoing: bool = False):
        builder = events.NewMessage(
            pattern=pattern, chats=chats, incoming=incoming, outgoing=outgoing,
        )
        return lambda callback: self._register(builder, callback)

    def on_edited_message(self, *, pattern: str | None = None, chats: object = None):
        builder = events.MessageEdited(pattern=pattern, chats=chats)
        return lambda callback: self._register(builder, callback)

    def on_callback(self, *, pattern: str | bytes | None = None):
        builder = events.CallbackQuery(pattern=pattern)
        return lambda callback: self._register(builder, callback)

    def create_task(self, awaitable: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
        if len(self._tasks) >= self.max_tasks:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise RuntimeError(f"插件后台任务已达到 {self.max_tasks} 个上限")
        task = asyncio.create_task(awaitable, name=name or f"plugin:{self.plugin_id}")
        self._tasks.add(task)
        def finished(value: asyncio.Task[Any]) -> None:
            self._tasks.discard(value)
            if value.cancelled():
                return
            try:
                value.result()
            except Exception:
                self.log.exception("后台任务执行失败")
        task.add_done_callback(finished)
        return task

    async def notify(self, text: str, entity: object = None, *, channel: str = "",
                     level: str = "info", category: str = "") -> object:
        try:
            result = await self.notifier.send(
                text, channel=channel, entity=entity, bot_id=self.bot_id,
                plugin_id=self.plugin_id, plugin_name=self.plugin_id, level=level, category=category,
            )
            return result
        except Exception:
            raise

    def on_webhook(self, path: str, callback: Callable[..., Any]) -> None:
        self.routes.webhook(self.plugin_id, path, callback)

    def action(self, name: str, callback: Callable[..., Any]) -> None:
        self.routes.action(self.plugin_id, name, callback)

    def schedule_interval(self, name: str, callback: Callable[..., Any], *, seconds: int) -> str:
        async def guarded_schedule() -> None:
            try:
                value = callback()
                if isinstance(value, Awaitable):
                    await asyncio.wait_for(value, timeout=self.timeout)
            except Exception:
                self.log.exception("定时任务执行失败：%s", name)
        return self.scheduler.add_interval(
            self.plugin_id, name, guarded_schedule, seconds=seconds,
        )

    def schedule_cron(self, name: str, callback: Callable[..., Any], **fields: Any) -> str:
        async def guarded_schedule() -> None:
            try:
                value = callback()
                if isinstance(value, Awaitable):
                    await asyncio.wait_for(value, timeout=self.timeout)
            except Exception:
                self.log.exception("定时任务执行失败：%s", name)
        return self.scheduler.add_cron(self.plugin_id, name, guarded_schedule, **fields)

    async def close(self) -> None:
        self.routes.remove_plugin(self.plugin_id)
        self.scheduler.remove_plugin(self.plugin_id)
        for client, callback, builder in reversed(self._handlers):
            client.remove_event_handler(callback, builder)
        self._handlers.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
