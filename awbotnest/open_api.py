from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from . import __version__
from .auth import api_key_dependency
from .config import Settings, save_settings
from .logs import memory_logs
from .plugins import PluginRuntime
from .storage import PluginKV
from .telegram import TelegramAccounts


def register_open_api(
    app: Any,
    settings: Settings,
    accounts: TelegramAccounts,
    runtime: PluginRuntime,
) -> None:
    router = APIRouter(
        prefix="/api/v1",
        tags=["开放平台 API"],
        dependencies=[Depends(api_key_dependency(settings))],
    )

    def meta(plugin_id: str):
        value = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if value is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        return value

    def entry(plugin_id: str) -> Path:
        value = next((item for item in runtime._entries()
                      if (item.parent.name if item.name == "__init__.py" else item.stem) == plugin_id), None)
        if value is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        return value

    @router.get("/plugins")
    async def plugins():
        return {"plugins": [item.to_dict() for item in runtime.scan()]}

    @router.get("/plugins/{plugin_id}")
    async def plugin_detail(plugin_id: str):
        return meta(plugin_id).to_dict()

    @router.get("/plugins/{plugin_id}/source")
    async def plugin_source(plugin_id: str):
        path = entry(plugin_id)
        return {
            "plugin_id": plugin_id,
            "path": str(path.relative_to(runtime.plugins_dir.parent)).replace("\\", "/"),
            "source": path.read_text(encoding="utf-8"),
            "is_package": path.name == "__init__.py",
        }

    @router.put("/plugins/{plugin_id}/source")
    async def update_plugin_source(plugin_id: str):
        raise HTTPException(status_code=403, detail="此 API 端点已因安全原因被禁用")

    @router.post("/plugins/{plugin_id}/enable")
    async def enable_plugin(plugin_id: str):
        value = await runtime.enable(plugin_id)
        if value.error:
            raise HTTPException(status_code=409, detail=value.error)
        return {"ok": True, "message": "插件已启用"}

    @router.post("/plugins/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str):
        meta(plugin_id)
        await runtime.disable(plugin_id)
        return {"ok": True, "message": "插件已停用"}

    @router.post("/plugins/{plugin_id}/reload")
    async def reload_plugin(plugin_id: str):
        meta(plugin_id)
        await runtime.disable(plugin_id, persist=False)
        value = await runtime.enable(plugin_id)
        if value.error:
            raise HTTPException(status_code=409, detail=value.error)
        return {"ok": True, "message": "插件已重载"}

    @router.get("/plugins/{plugin_id}/config")
    async def get_plugin_config(plugin_id: str):
        meta(plugin_id)
        return {"plugin_id": plugin_id, "config": dict(settings.plugin_config.get(plugin_id, {}))}

    @router.put("/plugins/{plugin_id}/config")
    async def put_plugin_config(plugin_id: str, request: Request):
        plugin = meta(plugin_id)
        raw = await request.json()
        values = raw.get("config")
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="config 必须是对象")
        try:
            runtime.validate_config(plugin.config_schema or {}, values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        settings.plugin_config[plugin_id] = dict(values)
        save_settings(settings)
        reloaded = plugin_id in runtime.loaded
        if reloaded:
            await runtime.disable(plugin_id, persist=False)
            value = await runtime.enable(plugin_id)
            if value.error:
                raise HTTPException(status_code=409, detail=value.error)
        return {"ok": True, "message": "配置已更新", "reloaded": reloaded}

    @router.get("/plugins/{plugin_id}/kv")
    async def list_kv(plugin_id: str):
        meta(plugin_id)
        return {"plugin_id": plugin_id, "keys": list(PluginKV(plugin_id).items())}

    @router.get("/plugins/{plugin_id}/kv/{key}")
    async def get_kv(plugin_id: str, key: str):
        meta(plugin_id)
        values = PluginKV(plugin_id).items()
        if key not in values:
            raise HTTPException(status_code=404, detail="键不存在")
        return {"plugin_id": plugin_id, "key": key, "value": values[key]}

    @router.put("/plugins/{plugin_id}/kv/{key}")
    async def put_kv(plugin_id: str, key: str, request: Request):
        meta(plugin_id)
        raw = await request.json()
        if "value" not in raw:
            raise HTTPException(status_code=400, detail="缺少 value")
        PluginKV(plugin_id).set(key, raw["value"])
        return {"ok": True, "message": "键值已设置"}

    @router.delete("/plugins/{plugin_id}/kv/{key}")
    async def delete_kv(plugin_id: str, key: str):
        meta(plugin_id)
        if not PluginKV(plugin_id).delete(key):
            raise HTTPException(status_code=404, detail="键不存在")
        return {"ok": True, "message": "键已删除"}

    def sender(kind: str, session: str = ""):
        if kind == "bot":
            client = accounts.choose_bot(session)
        elif kind == "user":
            client = accounts.users.get(session) if session else next(iter(accounts.connected_users), None)
        else:
            raise HTTPException(status_code=400, detail="sender 只能是 bot 或 user")
        if client is None or not client.is_connected():
            raise HTTPException(status_code=503, detail="指定的 Telegram 账号未连接")
        return client

    @router.post("/messages/send")
    async def send_message(request: Request):
        raw = await request.json()
        if raw.get("chat_id") in (None, "") or not str(raw.get("text") or ""):
            raise HTTPException(status_code=400, detail="chat_id 和 text 为必填项")
        client = sender(str(raw.get("sender") or "bot"), str(raw.get("session") or ""))
        try:
            message = await client.send_message(
                raw["chat_id"], str(raw["text"]), parse_mode=raw.get("parse_mode") or None,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"消息发送失败：{exc}") from exc
        return {
            "ok": True,
            "message_id": getattr(message, "id", None),
            "chat_id": raw["chat_id"],
            "date": getattr(message, "date", None),
        }

    @router.get("/chats/{chat_id}")
    async def chat_info(chat_id: str, session: str = ""):
        client = sender("user", session)
        try:
            entity = await client.get_entity(int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"会话不存在或不可访问：{exc}") from exc
        kind = "channel" if getattr(entity, "broadcast", False) else (
            "supergroup" if getattr(entity, "megagroup", False) else (
                "bot" if getattr(entity, "bot", False) else "private"
            )
        )
        return {
            "id": getattr(entity, "id", None),
            "title": getattr(entity, "title", None) or getattr(entity, "first_name", None) or "",
            "username": getattr(entity, "username", None),
            "type": kind,
        }

    @router.get("/accounts")
    async def account_list():
        return {"accounts": [asdict(item) for item in await accounts.states()]}

    @router.get("/logs")
    async def logs(limit: int = 100):
        return {"logs": memory_logs.recent(limit)}

    @router.get("/logs/plugins/{plugin_id}")
    async def plugin_logs(plugin_id: str, limit: int = 100):
        meta(plugin_id)
        records = [item for item in memory_logs.recent(1000)
                   if item.get("source") == f"awbotnest.plugin.{plugin_id}"][:max(1, min(limit, 1000))]
        return {"plugin_id": plugin_id, "logs": records}

    @router.get("/status")
    async def open_status():
        plugins = runtime.scan()
        states = [asdict(item) for item in await accounts.states()]
        return {
            "version": __version__,
            "bot_connected": any(item["kind"] == "bot" and item["connected"] for item in states),
            "user_accounts_count": sum(1 for item in states if item["kind"] == "user" and item["connected"]),
            "total_plugins": len(plugins),
            "enabled_plugins": len(runtime.loaded),
            "enabled_plugin_ids": sorted(runtime.loaded),
        }

    app.include_router(router)
