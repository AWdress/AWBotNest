from __future__ import annotations

import inspect
import json
import asyncio
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any


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
    """插件 Webhook 与控制台动作的隔离路由表。"""

    def __init__(self) -> None:
        self._webhooks: dict[tuple[str, str], Callable[..., Any]] = {}
        self._actions: dict[tuple[str, str], Callable[..., Any]] = {}
        self._apis: dict[tuple[str, str], Callable[..., Any]] = {}

    def api(self, plugin_id: str, path: str, callback: Callable[..., Any]) -> None:
        if not callable(callback):
            raise ValueError("插件 API 回调必须可调用")
        self._apis[(plugin_id, self._name(path))] = callback

    async def dispatch_api(self, plugin_id: str, path: str, request: Any) -> Any:
        key = (plugin_id, self._name(path))
        callback = self._apis.get(key) or self._webhooks.get(key)
        if callback is None:
            raise LookupError(f"插件接口未注册：{path}")
        value = callback(request)
        return await asyncio.wait_for(value, timeout=120) if inspect.isawaitable(value) else value

    @staticmethod
    def _name(value: str) -> str:
        result = value.strip().strip("/")
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
        self._apis = {key: value for key, value in self._apis.items() if key[0] != plugin_id}
        self._webhooks = {key: value for key, value in self._webhooks.items() if key[0] != plugin_id}
        self._actions = {key: value for key, value in self._actions.items() if key[0] != plugin_id}

    def describe(self, plugin_id: str) -> dict[str, list[str]]:
        return {
            "apis": sorted(key[1] for key in self._apis if key[0] == plugin_id),
            "webhooks": sorted(key[1] for key in self._webhooks if key[0] == plugin_id),
            "actions": sorted(key[1] for key in self._actions if key[0] == plugin_id),
        }
