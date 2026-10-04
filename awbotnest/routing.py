from __future__ import annotations

import inspect
import json
import asyncio
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from .wecom_messages import WECOM_MESSAGE_TYPES, WeComMessage


def _wecom_filter_values(values: Any, name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, (tuple, list)) or not values:
        raise ValueError(f"企业微信 {name} 必须是非空字符串或字符串元组")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"企业微信 {name} 中的每项必须是非空字符串")
    return tuple(dict.fromkeys(value.strip().lower() for value in values))


def _normalize_wecom_filters(message_types: Any, events: Any) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
    types = _wecom_filter_values(message_types, "message_types")
    if any(message_type not in WECOM_MESSAGE_TYPES for message_type in types):
        raise ValueError("企业微信消息类型不支持")
    if events is not None and "event" not in types:
        raise ValueError("企业微信 events 过滤仅适用于 event 消息")
    return types, _wecom_filter_values(events, "events") if events is not None else None


@dataclass(frozen=True, slots=True)
class _WeComHandler:
    callback: Callable[..., Any]
    message_types: tuple[str, ...]
    events: tuple[str, ...] | None
    token: str

    def matches(self, message_type: str, event: str) -> bool:
        if not isinstance(message_type, str) or not isinstance(event, str):
            return False
        message_type = message_type.strip().lower()
        return message_type in self.message_types and (
            message_type != "event" or self.events is None or event.strip().lower() in self.events
        )


@dataclass(slots=True)
class WebhookRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


class PluginRoutes:
    """插件 Webhook、控制台动作和已鉴权消息的隔离路由表。"""

    def __init__(self) -> None:
        self._webhooks: dict[tuple[str, str], Callable[..., Any]] = {}
        self._actions: dict[tuple[str, str], Callable[..., Any]] = {}
        self._apis: dict[tuple[str, str], Callable[..., Any]] = {}
        self._method_apis = {}
        self._wecom_handlers: dict[str, _WeComHandler] = {}

    def wecom_message(self, plugin_id: str, callback: Callable[..., Any],
                      message_types=("text", "image", "file", "event"), events=None) -> str:
        if not callable(callback):
            raise TypeError("企业微信消息回调必须可调用")
        types, event_names = _normalize_wecom_filters(message_types, events)
        token = uuid.uuid4().hex
        self._wecom_handlers[plugin_id] = _WeComHandler(callback, types, event_names, token)
        return token

    def wecom_handler_token(self, plugin_id: str, message_type: str, event: str = "") -> str | None:
        handler = self._wecom_handlers.get(plugin_id)
        return handler.token if handler is not None and handler.matches(message_type, event) else None

    def has_wecom_handler(self, plugin_id: str, message_type: str, event: str = "") -> bool:
        return self.wecom_handler_token(plugin_id, message_type, event) is not None

    def wecom_handler_count(self, plugin_id: str) -> int:
        return int(plugin_id in self._wecom_handlers)

    async def dispatch_wecom_message(self, plugin_id: str, message: WeComMessage, *, expected_token: str | None = None) -> Any:
        """调用方必须先校验消息、渠道权限及入队时的 handler token。"""
        if not isinstance(message, WeComMessage):
            raise TypeError("企业微信消息必须是 WeComMessage")
        handler = self._wecom_handlers.get(plugin_id)
        if (handler is None or not handler.matches(message.message_type, message.event)
                or expected_token is not None and handler.token != expected_token):
            raise LookupError("企业微信消息处理器未注册或不匹配")
        value = handler.callback(message)
        return await asyncio.wait_for(value, timeout=120) if inspect.isawaitable(value) else value

    def api(self, plugin_id: str, path: str, callback: Callable[..., Any], methods=None) -> None:
        if not callable(callback):
            raise ValueError("插件 API 回调必须可调用")
        if methods is None:
            self._apis[(plugin_id, self._name(path))] = callback
        else:
            normalized = [str(method).upper() for method in ([methods] if isinstance(methods, str) else methods)]
            if not normalized or any(method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                                     for method in normalized):
                raise ValueError("插件 API HTTP 方法无效")
            for method in normalized:
                self._method_apis[(plugin_id, self._name(path), method)] = callback

    async def dispatch_api(self, plugin_id: str, path: str, request: Any) -> Any:
        key = (plugin_id, self._name(path))
        callback = self._method_apis.get((*key, str(getattr(request, 'method', 'GET')).upper()))
        callback = callback or self._apis.get(key) or self._webhooks.get(key)
        if callback is None:
            raise LookupError(f"插件接口未注册：{path}")
        value = callback(request)
        return await asyncio.wait_for(value, timeout=120) if inspect.isawaitable(value) else value

    @staticmethod
    def _name(value: str) -> str:
        result = value.strip().strip("/")
        if not result and value.strip() == "/":
            return "/"
        if not result or ".." in result:
            raise ValueError("路由名称不合法")
        return result

    def webhook(self, plugin_id: str, path: str, callback: Callable[..., Any]) -> None:
        self._webhooks[(plugin_id, self._name(path))] = callback

    def action(self, plugin_id: str, name: str, callback: Callable[..., Any]) -> None:
        self._actions[(plugin_id, self._name(name))] = callback

    async def dispatch_webhook(self, plugin_id: str, path: str, request: Any) -> Any:
        callback = self._webhooks.get((plugin_id, self._name(path)))
        if callback is None:
            raise LookupError("Webhook 不存在")
        value = callback(request)
        return await asyncio.wait_for(value, timeout=120) if inspect.isawaitable(value) else value

    async def dispatch_action(self, plugin_id: str, name: str, payload: dict[str, Any]) -> Any:
        callback = self._actions.get((plugin_id, self._name(name)))
        if callback is None:
            raise LookupError("插件动作不存在")
        value = callback(payload)
        return await asyncio.wait_for(value, timeout=120) if inspect.isawaitable(value) else value

    def remove_plugin(self, plugin_id: str) -> None:
        self._wecom_handlers.pop(plugin_id, None)
        self._method_apis = {key: value for key, value in self._method_apis.items() if key[0] != plugin_id}
        self._apis = {key: value for key, value in self._apis.items() if key[0] != plugin_id}
        self._webhooks = {key: value for key, value in self._webhooks.items() if key[0] != plugin_id}
        self._actions = {key: value for key, value in self._actions.items() if key[0] != plugin_id}

    def describe(self, plugin_id: str) -> dict[str, list[str]]:
        return {
            "apis": sorted({key[1] for key in [*self._apis, *self._method_apis] if key[0] == plugin_id}),
            "webhooks": sorted(key[1] for key in self._webhooks if key[0] == plugin_id),
            "actions": sorted(key[1] for key in self._actions if key[0] == plugin_id),
        }
