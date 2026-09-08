from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import platform
import secrets
import tempfile
import time
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .. import __version__
from ..activity import activity
from ..auth import token_matches
from ..backup import BackupManager, MAX_BACKUP_SIZE
from ..config import APP_ROOT, DATA_DIR, PLUGINS_DIR, SESSIONS_DIR, BotSettings, save_settings
from ..logs import memory_logs
from ..market import normalize_repo
from ..routing import WebhookRequest
from .models import *

logger = logging.getLogger("awbotnest.api")

def create_router(deps) -> APIRouter:
    router = APIRouter()
    settings = deps.settings
    accounts = deps.accounts
    runtime = deps.runtime
    scheduler = deps.scheduler
    routes = deps.routes
    restart_event = deps.restart_event
    market = deps.market
    require_admin = deps.require_admin
    started_at = deps.started_at
    resource_sampler = deps.resource_sampler

    @router.post("/api/plugins/{plugin_id}/enable", dependencies=[Depends(require_admin)])
    async def enable_plugin(plugin_id: str):
        logger.info("插件启用开始：%s", runtime.display_name(plugin_id))
        try:
            meta = await runtime.enable(plugin_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if meta.error:
            logger.error("插件启用失败：%s（%s）", meta.name, meta.error)
            raise HTTPException(status_code=409, detail=meta.error)
        logger.info("插件启用成功：%s", meta.name)
        value = meta.to_dict()
        return {**value, "plugin": value}

    @router.post("/api/plugins/{plugin_id}/disable", dependencies=[Depends(require_admin)])
    async def disable_plugin(plugin_id: str):
        plugin_name = runtime.display_name(plugin_id)
        logger.info("插件停用开始：%s", plugin_name)
        await runtime.disable(plugin_id)
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        value = meta.to_dict() if meta else {"id": plugin_id, "enabled": False, "loaded": False}
        value["enabled"] = False
        value["loaded"] = False
        logger.info("插件停用成功：%s", plugin_name)
        return {"ok": True, "plugin": value}

    @router.post("/api/plugins/{plugin_id}/reload", dependencies=[Depends(require_admin)])
    async def reload_plugin(plugin_id: str):
        meta = await runtime.reload(plugin_id)
        if meta.error:
            raise HTTPException(status_code=409, detail=meta.error)
        return {"ok": True, "plugin": meta.to_dict()}

    @router.post("/api/plugins/{plugin_id}/self-check", dependencies=[Depends(require_admin)])
    async def self_check_plugin(plugin_id: str):
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        missing = runtime.deps.missing(meta.requirements or [])
        checks = [
            {"id": "manifest", "name": "插件规范", "ok": not bool(meta.error), "detail": meta.error or "正常"},
            {"id": "dependencies", "name": "Python 依赖", "ok": not missing,
             "detail": "正常" if not missing else "缺少：" + "、".join(missing)},
            {"id": "runtime", "name": "运行状态", "ok": (not meta.enabled) or meta.id in runtime.loaded,
             "detail": "已加载" if meta.id in runtime.loaded else "未启用"},
        ]
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    @router.get("/api/plugins/dependencies", dependencies=[Depends(require_admin)])
    async def plugin_dependencies():
        nodes = []
        edges = []
        for meta in runtime.scan():
            nodes.append({"id": meta.id, "name": meta.name, "scope": meta.scope,
                          "requirements": meta.requirements or [], "enabled": meta.enabled,
                          "requires_plugins": meta.requires_plugins,
                          "requires_capabilities": meta.requires_capabilities,
                          "provides_capabilities": meta.provides_capabilities})
            edges.extend({"source": meta.id, "target": required, "type": "plugin"}
                         for required in meta.requires_plugins)
        return {"nodes": nodes, "edges": edges}

    @router.get("/api/plugins/{plugin_id}/accounts", dependencies=[Depends(require_admin)])
    async def get_plugin_accounts(plugin_id: str):
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        options = [
            {"session": item.id, "name": item.display_name or item.username or item.id}
            for item in await accounts.states() if item.kind == "user"
        ]
        return {"accounts": options, "selected": settings.plugin_accounts.get(plugin_id, []), "scope": meta.scope}

    @router.put("/api/plugins/{plugin_id}/accounts", dependencies=[Depends(require_admin)])
    async def set_plugin_accounts(plugin_id: str, request: Request):
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        raw = await request.json()
        sessions = raw.get("sessions") or []
        valid = {item.id for item in await accounts.states() if item.kind == "user"}
        if not isinstance(sessions, list) or any(str(item) not in valid for item in sessions):
            raise HTTPException(status_code=400, detail="账号范围包含不存在的账号")
        settings.plugin_accounts[plugin_id] = [str(item) for item in sessions]
        save_settings(settings)
        if plugin_id in runtime.loaded:
            await runtime.disable(plugin_id, persist=False)
            meta = await runtime.enable(plugin_id)
            if meta.error:
                raise HTTPException(status_code=409, detail=meta.error)
        return {"ok": True, "selected": settings.plugin_accounts[plugin_id]}

    @router.get("/api/plugins/{plugin_id}/webhook", dependencies=[Depends(require_admin)])
    async def get_plugin_webhook(plugin_id: str):
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        described = routes.describe(plugin_id)
        return {"webhooks": described.get("webhooks", []), "actions": described.get("actions", []),
                "base_url": f"/api/plugin/{plugin_id}/"}

    @router.get("/api/plugins/{plugin_id}/runtime", dependencies=[Depends(require_admin)])
    async def plugin_runtime_status(plugin_id: str):
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        loaded = runtime.loaded.get(plugin_id)
        context = loaded.context if loaded else None
        return {"id": plugin_id, "enabled": meta.enabled, "loaded": bool(loaded),
                "error": meta.error or runtime._errors.get(plugin_id, ""),
                "handlers": sum(len(item._handlers) for item in loaded.contexts) if loaded else 0,
                "background_tasks": runtime.services.governor.status(plugin_id)["background_tasks"],
                "instances": ([{"id": item.instance_id, "account": item.account_name or "全局实例"}
                               for item in loaded.contexts] if loaded else []),
                "circuits": runtime.services.governor.status(plugin_id)["circuits"],
                "policy": runtime.services.governor.status(plugin_id)["policy"],
                "events": runtime.services.governor.events.query(plugin_id)}

    @router.post("/api/plugins/{plugin_id}/events/{event_id}/replay", dependencies=[Depends(require_admin)])
    async def replay_plugin_event(plugin_id: str, event_id: str):
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        if plugin_id not in runtime.loaded:
            raise HTTPException(status_code=409, detail="插件未启用")
        try:
            return {"ok": True, "result": await runtime.services.governor.replay(plugin_id, event_id)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/plugins/{plugin_id}/dialogs", dependencies=[Depends(require_admin)])
    async def list_plugin_dialogs(plugin_id: str, session: str = ""):
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        client = accounts.users.get(session) if session else next(iter(accounts.connected_users), None)
        if client is None:
            return {"dialogs": []}
        values = []
        async for dialog in client.iter_dialogs(limit=200):
            entity = getattr(dialog, "entity", None)
            values.append({
                "id": str(dialog.id),
                "title": dialog.name or str(dialog.id),
                "type": "channel" if getattr(entity, "broadcast", False) else (
                    "group" if getattr(entity, "title", None) else "private"),
            })
        return {"dialogs": values}

    @router.get("/api/chats/{chat_id}", dependencies=[Depends(require_admin)])
    async def get_chat_info(chat_id: str, session: str = ""):
        client = accounts.users.get(session) if session else next(iter(accounts.connected_users), None)
        if client is None:
            raise HTTPException(status_code=409, detail="没有可用的已连接用户账号")
        target: int | str = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        try:
            entity = await client.get_entity(target)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"获取会话信息失败：{exc}") from exc
        title = (getattr(entity, "title", None) or
                 " ".join(part for part in (getattr(entity, "first_name", ""),
                                               getattr(entity, "last_name", "")) if part) or
                 getattr(entity, "username", None) or str(getattr(entity, "id", chat_id)))
        kind = "channel" if getattr(entity, "broadcast", False) else (
            "group" if getattr(entity, "megagroup", False) or getattr(entity, "title", None) else "private")
        return {"id": getattr(entity, "id", chat_id), "title": title, "type": kind}

    @router.get("/api/plugins/repo/status", dependencies=[Depends(require_admin)])
    async def plugin_repo_status():
        return {"repos": [{"url": item, "enabled": True} for item in settings.plugin_repos],
                "manifest": "manifest_v2.json"}

    @router.post("/api/plugins/upload", dependencies=[Depends(require_admin)])
    async def upload_plugin(file: UploadFile = File(...)):
        filename = file.filename or ""
        if not filename.endswith(".py") or not filename[:-3].replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="仅支持名称安全的 .py 插件文件")
        content = await file.read(2 * 1024 * 1024 + 1)
        if not content or len(content) > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="插件文件大小不能超过 2 MB")
        target = PLUGINS_DIR / filename
        temporary = PLUGINS_DIR / f".{filename}.upload"
        backup = target.read_bytes() if target.exists() else None
        try:
            content.decode("utf-8")
            temporary.write_bytes(content)
            temporary.replace(target)
            meta = next((item for item in runtime.scan() if item.id == target.stem), None)
            if meta is None or meta.error:
                raise ValueError(meta.error if meta else "插件元数据无法识别")
        except Exception as exc:
            logger.error("安装失败：%s（%s）", runtime.display_name(target.stem), exc)
            target.unlink(missing_ok=True)
            if backup is not None:
                target.write_bytes(backup)
            raise HTTPException(status_code=400, detail=f"插件校验失败：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        logger.info("已安装：%s", runtime.display_name(target.stem))
        return {"ok": True, "plugin": meta.to_dict()}

    @router.delete("/api/plugins/{plugin_id}", dependencies=[Depends(require_admin)])
    async def delete_plugin(plugin_id: str):
        if not plugin_id.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(status_code=400, detail="插件 ID 不合法")
        await runtime.disable(plugin_id)
        file_target = (PLUGINS_DIR / f"{plugin_id}.py").resolve()
        dir_target = (PLUGINS_DIR / plugin_id).resolve()
        root = PLUGINS_DIR.resolve()
        if file_target.parent != root or dir_target.parent != root:
            raise HTTPException(status_code=400, detail="插件路径不合法")
        removed = False
        if file_target.exists():
            file_target.unlink()
            removed = True
        if dir_target.exists() and dir_target.is_dir():
            import shutil
            shutil.rmtree(dir_target)
            removed = True
        if not removed:
            raise HTTPException(status_code=404, detail="插件不存在")
        settings.plugin_config.pop(plugin_id, None)
        settings.plugin_accounts.pop(plugin_id, None)
        settings.bot_routing.pop(plugin_id, None)
        settings.plugin_order = [item for item in settings.plugin_order if item != plugin_id]
        save_settings(settings)
        # 删除后立即使市场缓存失效，避免已删除插件仍显示为“已安装”。
        market.clear_cache()
        return {"ok": True}

    @router.get("/api/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
    async def plugin_config(plugin_id: str):
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        schema = meta.config_schema or {}
        values = dict(settings.plugin_config.get(plugin_id, {}))
        for key, spec in schema.items():
            if runtime.secret_field(spec) and values.get(key):
                values[key] = "********"
        return {
            "values": values,
            "schema": schema,
            "render_mode": meta.render_mode,
            "has_frontend": runtime.has_frontend(plugin_id),
        }

    @router.get("/api/plugins/{plugin_id}/fe/{path:path}")
    async def plugin_frontend_asset(plugin_id: str, path: str, request: Request):
        resource_token = request.cookies.get("awbotnest_resource", "")
        if not token_matches(resource_token, settings.admin_token):
            raise HTTPException(status_code=403, detail="无权访问插件资源，请重新登录")
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None or meta.render_mode != "vue":
            raise HTTPException(status_code=404, detail="Vue 插件不存在")
        dist = runtime.frontend_dist_dir(plugin_id).resolve()

        def resolve_asset(relative: str):
            target = (dist / relative).resolve()
            if target != dist and dist not in target.parents:
                raise HTTPException(status_code=400, detail="非法资源路径")
            return target if target.is_file() else None

        target = resolve_asset(path) or resolve_asset(f"assets/{path}")
        if target is None:
            raise HTTPException(status_code=404, detail="插件前端资源不存在")
        media_type = {
            ".js": "application/javascript", ".mjs": "application/javascript",
            ".css": "text/css", ".json": "application/json",
        }.get(target.suffix.lower())
        response = FileResponse(target, media_type=media_type)
        response.headers["Cache-Control"] = (
            "no-cache" if target.name == "remoteEntry.js"
            else "public, max-age=31536000, immutable"
        )
        return response

    @router.put("/api/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
    async def update_plugin_config(plugin_id: str, request: Request):
        plugin = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if plugin is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        raw = await request.json()
        values = dict(raw.get("values", raw))
        if len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="插件配置超过 1 MB")
        current_values = settings.plugin_config.get(plugin_id, {})
        for key, spec in (plugin.config_schema or {}).items():
            if runtime.secret_field(spec) and values.get(key) == "********":
                values[key] = current_values.get(key, "")
        try:
            runtime.validate_config(plugin.config_schema or {}, values,
                                    allow_extra=plugin.render_mode == "vue")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        settings.plugin_config[plugin_id] = values
        save_settings(settings)
        loaded = runtime.loaded.get(plugin_id)
        if loaded:
            await runtime.disable(plugin_id)
            if plugin_id not in settings.enabled_plugins:
                settings.enabled_plugins.append(plugin_id)
                save_settings(settings)
            meta = await runtime.enable(plugin_id)
            if meta.error:
                raise HTTPException(status_code=409, detail=meta.error)
        safe_values = dict(settings.plugin_config[plugin_id])
        for key, spec in (plugin.config_schema or {}).items():
            if runtime.secret_field(spec) and safe_values.get(key):
                safe_values[key] = "********"
        return {"ok": True, "values": safe_values}
    return router
