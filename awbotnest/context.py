from __future__ import annotations

import asyncio
import logging
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import TelegramClient, events

from .telegram import TelegramAccounts
from .scheduler import PluginScheduler
from .storage import PluginKV
from .config import DATA_DIR, Settings, save_settings
from .services import PlatformServices, PluginAI
from .plugin_cookies import PluginCookies
from .routing import PluginRoutes
from .notifier import NotificationService
from .activity import set_current, reset_current, track_call

EventCallback = Callable[[Any], Awaitable[Any]]


class PluginLogger(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['plugin_name']}] {msg}", kwargs


class PluginContext:
    """Telethon 原生的插件能力入口。"""
    StopPropagation = events.StopPropagation

    def __init__(self, plugin_id: str, scope: str, accounts: TelegramAccounts,
                 scheduler: PluginScheduler, settings: Settings, services: PlatformServices,
                 routes: PluginRoutes, notifier: NotificationService, bot_id: str = "",
                 resources: dict[str, object] | None = None, plugin_name: str = "",
                 account_name: str | None = None, primary_instance: bool = True,
                 cookie_domains: list[str] | None = None) -> None:
        self.plugin_id = plugin_id
        self.account_name = account_name
        self.instance_id = f"{plugin_id}@{account_name}" if account_name else plugin_id
        self.is_primary_instance = primary_instance
        self.plugin_name = plugin_name or plugin_id
        self.scope = scope
        self.accounts = accounts
        self.scheduler = scheduler
        self.settings = settings
        self.bot_id = bot_id
        self.kv = PluginKV(self.instance_id)
        self.data_dir = DATA_DIR / "plugins" / plugin_id
        if account_name:
            self.data_dir = self.data_dir / "instances" / account_name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.services = services
        self.governor = services.governor
        if plugin_id not in self.governor._policies:
            self.governor.configure(plugin_id, resources)
        self._plugin_ai = PluginAI(services.ai, plugin_id, self.data_dir)
        self.routes = routes
        self.notifier = notifier
        self._plugin_cookies = PluginCookies(services.cookies, settings, cookie_domains or [], self.notify)
        policy = self.governor.policy(plugin_id)
        self.timeout = policy.timeout_seconds
        self.failure_threshold = policy.failure_threshold
        self.max_tasks = policy.max_background_tasks
        self.max_concurrency = policy.max_concurrency
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self.log = PluginLogger(logging.getLogger(f"awbotnest.plugin.{plugin_id}"),
                                {"plugin_name": self.plugin_name})
        self._handlers: list[tuple[TelegramClient, EventCallback, object]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._active: set[asyncio.Task[Any]] = set()
        self._cleanups: list[Callable[..., Any]] = []
        self._closed = False

    def add_cleanup(self, callback: Callable[..., Any]) -> None:
        if self._closed:
            raise RuntimeError("插件已停用")
        if not callable(callback):
            raise TypeError("清理回调必须可调用")
        self._cleanups.append(callback)

    def report_progress(self, percent=None, step=None) -> bool:
        return self.scheduler.report_progress(percent=percent, step=step)

    async def execute(self, operation, callback, *, timeout=None, fallback=None, event_data=None):
        if self._closed:
            raise RuntimeError("插件已停用")
        async def invoke():
            token = set_current(self.plugin_id)
            try:
                if str(operation).startswith(("schedule:", "job:", "action:")):
                    return await track_call(self.plugin_id, callback)
                value = callback()
                return await value if inspect.isawaitable(value) else value
            finally:
                reset_current(token)
        return await self.governor.execute(self.plugin_id, f"{self.instance_id}:{operation}", invoke,
            timeout=timeout, fallback=fallback, event_data=event_data)

    def provide_capability(self, name, provider, *, priority=100):
        self.add_cleanup(self.governor.capabilities.register(self.plugin_id, name, provider, priority))

    async def call_capability(self, name, *args, method=None, **kwargs):
        if self._closed:
            raise RuntimeError("插件已停用")
        return await self.governor.call_capability(self.plugin_id, name, method, *args, **kwargs)

    def record_event(self, event_type, payload=None, *, replay_type=None):
        return self.governor.events.append(self.plugin_id, event_type,
            payload=payload or {}, replay_type=replay_type, instance_id=self.instance_id)

    def on_replay(self, event_type):
        def register(callback):
            self.add_cleanup(self.governor.register_replayer(
                self.instance_id, event_type, self._managed(callback, governed=False)))
            return callback
        return register

    def _managed(self, callback, *, operation=None, governed=True):
        async def invoke(*args, **kwargs):
            if self._closed:
                raise RuntimeError("插件已停用")
            task = asyncio.current_task()
            self._active.add(task)
            try:
                if not governed:
                    result = callback(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result
                return await self.execute(operation or f"callback:{getattr(callback, '__name__', 'call')}",
                                          lambda: callback(*args, **kwargs))
            finally:
                self._active.discard(task)
        return invoke

    @property
    def bot(self) -> TelegramClient | None:
        return self.accounts.choose_bot(self.bot_id)

    @property
    def users(self) -> list[TelegramClient]:
        selected = self.settings.plugin_accounts.get(self.plugin_id, [])
        return [client for name, client in self.accounts.users.items()
                if client.is_connected() and (not selected or name in selected)
                and (not self.account_name or name == self.account_name)]

    @property
    def user(self):
        return next(iter(self.users), None)

    @property
    def user_apps(self):
        return self.users

    def get_bot(self, bot_id=None):
        return self.accounts.choose_bot(bot_id or self.bot_id)

    @property
    def config(self) -> dict[str, object]:
        return dict(self.settings.plugin_config.get(self.plugin_id, {}))

    @property
    def http(self):
        return self._services.http

    @property
    def cookies(self):
        return self._plugin_cookies

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
        if self.settings.plugin_accounts.get(self.plugin_id) or self.account_name:
            user_clients = list(self.accounts.users.values())
            allowed = self.users
            clients = [client for client in clients if client not in user_clients or client in allowed]
        if not self.is_primary_instance:
            clients = [client for client in clients if client not in self.accounts.bots.values()]
        if not clients and self.scope != "standalone":
            raise RuntimeError(f"插件 {self.plugin_id} 没有可用的 {self.scope} Telegram 客户端")
        failures = 0
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            nonlocal failures
            if self._closed:
                return
            task = asyncio.current_task()
            self._active.add(task)
            event = args[0] if args else None
            raw_id = getattr(event, "id", "") or getattr(getattr(event, "message", None), "id", "")
            chat_id = getattr(event, "chat_id", "") or getattr(getattr(event, "message", None), "chat_id", "")
            event_id = f"{chat_id}:{raw_id}" if raw_id else f"task:{id(asyncio.current_task())}"
            token = set_current(self.plugin_id, event_id)
            try:
                result = await self.execute(f"event:{getattr(callback, '__name__', 'call')}",
                                            lambda: callback(*args, **kwargs))
                failures = 0
                return result
            except events.StopPropagation:
                raise
            except Exception:
                failures += 1
                self.log.exception("事件处理失败（连续 %s 次）", failures)
                if failures == self.failure_threshold:
                    self.log.error("事件处理连续失败 %s 次，暂时熔断，冷却后自动恢复", self.failure_threshold)
            finally:
                self._active.discard(task)
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
        if self._closed or len(self._tasks) >= self.max_tasks:
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
                     level: str = "info", category: str = "", format: str = "text", account: Any = None) -> object:
        try:
            result = await track_call(self.plugin_id, lambda: self.notifier.send(
                text, channel=channel, entity=entity, bot_id=self.bot_id,
                plugin_id=self.plugin_id, plugin_name=self.plugin_name, level=level, category=category,
                format=format, account=account,
            ))
            return result
        except Exception:
            raise

    def on_webhook(self, path, callback=None):
        if callable(path) and callback is None:
            callback, path = path, "receive"
        if callback is None:
            return lambda handler: self.on_webhook(path, handler)
        if self.is_primary_instance:
            self.routes.webhook(self.plugin_id, path, self._managed(callback))
        return callback

    def on_api(self, path: str, callback: Callable[..., Any] | None = None, *, methods=None):
        """注册管理员接口；支持直接调用及装饰器，回调接收 WebhookRequest。"""
        def register(handler):
            if self.is_primary_instance:
                self.routes.api(self.plugin_id, path, self._managed(handler), methods=methods)
            return handler
        return register(callback) if callback is not None else register

    def action(self, name: str, callback: Callable[..., Any] | None = None):
        if callback is None:
            return lambda handler: self.action(name, handler)
        if self.is_primary_instance:
            if not inspect.signature(callback).parameters:
                async def no_args(payload):
                    value = callback()
                    return await value if inspect.isawaitable(value) else value
                self.routes.action(self.plugin_id, name, self._managed(no_args, operation=f"action:{name}"))
            else:
                self.routes.action(self.plugin_id, name, self._managed(callback, operation=f"action:{name}"))
        return callback

    def schedule(self, callback, trigger="interval", **fields):
        name = str(fields.pop("id", None) or getattr(callback, "__name__", "task"))
        async def invoke(*args, **kwargs):
            if inspect.iscoroutinefunction(callback):
                return await callback(*args, **kwargs)
            value = await asyncio.to_thread(callback, *args, **kwargs)
            return await value if inspect.isawaitable(value) else value
        return self.scheduler.add(self.instance_id, name,
            self._managed(invoke, operation=f"schedule:{name}"), trigger, **fields)

    def schedule_interval(self, name: str, callback: Callable[..., Any], *, seconds: int) -> str:
        async def guarded_schedule() -> None:
            try:
                value = callback()
                if isinstance(value, Awaitable):
                    return await asyncio.wait_for(value, timeout=self.timeout)
                return value
            except Exception:
                self.log.exception("定时任务执行失败：%s", name)
                raise
        return self.scheduler.add_interval(
            self.instance_id, name, self._managed(guarded_schedule, operation=f"job:{name}"), seconds=seconds,
        )

    def schedule_cron(self, name: str, callback: Callable[..., Any], **fields: Any) -> str:
        async def guarded_schedule() -> None:
            try:
                value = callback()
                if isinstance(value, Awaitable):
                    return await asyncio.wait_for(value, timeout=self.timeout)
                return value
            except Exception:
                self.log.exception("定时任务执行失败：%s", name)
                raise
        return self.scheduler.add_cron(self.instance_id, name,
            self._managed(guarded_schedule, operation=f"job:{name}"), **fields)

    async def close(self) -> None:
        self._closed = True
        if self.is_primary_instance:
            self.routes.remove_plugin(self.plugin_id)
        self.scheduler.remove_plugin(self.instance_id)
        for client, callback, builder in reversed(self._handlers):
            client.remove_event_handler(callback, builder)
        self._handlers.clear()
        pending = (self._tasks | self._active) - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()
        while self._cleanups:
            callback = self._cleanups.pop()
            try:
                value = callback()
                if isinstance(value, Awaitable):
                    await asyncio.wait_for(value, timeout=10)
            except Exception:
                self.log.exception("插件资源清理失败")
