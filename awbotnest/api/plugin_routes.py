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

    @router.get("/api/plugins", dependencies=[Depends(require_admin)])
    async def plugins():
        values = []
        local_heat = market.local_install_counts()
        for meta in runtime.scan():
            item = meta.to_dict()
            item["install_count"] = local_heat.get(meta.id, 0)
            item.update(routes.describe(meta.id))
            values.append(item)
        order = [item for item in settings.plugin_order if any(meta["id"] == item for meta in values)]
        rank = {plugin_id: index for index, plugin_id in enumerate(order)}
        values.sort(key=lambda item: (rank.get(item["id"], len(rank)), item.get("name") or item["id"]))
        return {"plugins": values, "official_ids": [], "custom_order": bool(order)}

    @router.put("/api/plugins/order", dependencies=[Depends(require_admin)])
    async def save_plugin_order(request: Request):
        raw = await request.json()
        order = raw.get("order")
        known = {meta.id for meta in runtime.scan()}
        if not isinstance(order, list) or set(map(str, order)) != known or len(order) != len(known):
            raise HTTPException(status_code=400, detail="插件顺序与当前插件不一致")
        settings.plugin_order = [str(item) for item in order]
        save_settings(settings)
        return {"ok": True, "order": settings.plugin_order}

    @router.api_route("/api/plugin/{plugin_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   include_in_schema=False)
    async def plugin_webhook(plugin_id: str, path: str, request: Request):
        try:
            body = await request.body()
            if len(body) > 20 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Webhook 请求体超过 20 MB")
            wrapped = WebhookRequest(
                method=request.method, path=path, query=dict(request.query_params),
                headers={key.lower(): value for key, value in request.headers.items()}, body=body,
            )
            result = await routes.dispatch_webhook(plugin_id, path, wrapped)
        except HTTPException:
            raise
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("插件 Webhook 执行失败：%s/%s", runtime.display_name(plugin_id), path)
            raise HTTPException(status_code=502, detail="插件 Webhook 执行失败") from exc
        if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            return JSONResponse(content=result)
        return result

    @router.api_route("/api/plugins/{plugin_id}/api/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   dependencies=[Depends(require_admin)], include_in_schema=False)
    async def plugin_api_legacy(plugin_id: str, path: str, request: Request):
        """管理员插件接口；兼容以 on_webhook 注册的既有配置接口。"""
        if plugin_id not in runtime.loaded:
            if runtime.entry_file(plugin_id) is None:
                raise HTTPException(status_code=404, detail="插件不存在或尚未安装")
            detail = ("插件尚未运行：正在启动或启动失败，请查看插件启用日志"
                      if plugin_id in settings.enabled_plugins else "插件未启用，请先启用插件")
            raise HTTPException(status_code=409, detail=detail)
        body = await request.body()
        if len(body) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="插件接口请求体超过 20 MB")
        wrapped = WebhookRequest(method=request.method, path=path,
                                 query=dict(request.query_params),
                                 headers={key.lower(): value for key, value in request.headers.items()},
                                 body=body)
        try:
            result = await routes.dispatch_api(plugin_id, path, wrapped)
        except HTTPException:
            raise
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("插件接口执行失败：%s/%s", runtime.display_name(plugin_id), path)
            raise HTTPException(status_code=502, detail="插件接口执行失败，请查看运行日志") from exc
        if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            return JSONResponse(content=result)
        return result

    @router.post("/api/plugins/{plugin_id}/action/{action}", dependencies=[Depends(require_admin)])
    async def plugin_action_legacy(plugin_id: str, action: str):
        """V1 单数 action 路径兼容别名。"""
        try:
            result = await routes.dispatch_action(plugin_id, action, {})
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "result": result}

    @router.api_route("/api/v1/webhook", methods=["GET", "POST"], include_in_schema=False)
    async def platform_webhook(request: Request):
        """共享密钥鉴权后，把外部内容送入平台通知服务。"""
        secret = settings.webhook_secret.strip()
        if not secret:
            raise HTTPException(status_code=404, detail="Webhook 未开启")
        supplied = str(request.query_params.get("apikey") or "")
        if not token_matches(supplied, secret):
            raise HTTPException(status_code=401, detail="apikey 无效")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="Webhook 请求体超过 1 MB")
        payload: object = None
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("message") or payload.get("content") or "").strip()
            if not text:
                text = json.dumps(payload, ensure_ascii=False, indent=2)
            title = str(payload.get("title") or "").strip()
            category = str(payload.get("category") or "").strip()
            message = f"{title}\n{text}" if title else text
        else:
            message = body.decode("utf-8", errors="replace").strip() or "(空内容)"
            category = ""
        try:
            await runtime.notifier.send(
                message, plugin_id="__platform_webhook__", plugin_name="系统 Webhook",
                level="info", category=category,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("系统 Webhook 通知失败")
            raise HTTPException(status_code=502, detail="Webhook 通知投递失败") from exc
        return {"ok": True}

    @router.api_route("/api/v1/plugin/{plugin_id}/webhook", methods=["GET", "POST"], include_in_schema=False)
    async def public_plugin_webhook(plugin_id: str, request: Request):
        """V1 兼容的插件公开 Webhook，使用平台统一 WEBHOOK_SECRET。"""
        secret = settings.webhook_secret.strip()
        if not secret:
            raise HTTPException(status_code=404, detail="Webhook 未开启")
        supplied = str(request.query_params.get("apikey") or "")
        if not token_matches(supplied, secret):
            raise HTTPException(status_code=401, detail="apikey 无效")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="Webhook 请求体超过 1 MB")
        path = "receive"
        declared = routes.describe(plugin_id).get("webhooks", [])
        if declared:
            path = declared[0]
        wrapped = WebhookRequest(
            method=request.method, path=path,
            query={k: v for k, v in request.query_params.items() if k != "apikey"},
            headers={key.lower(): value for key, value in request.headers.items()}, body=body,
        )
        try:
            result = await routes.dispatch_webhook(plugin_id, path, wrapped)
        except LookupError as exc:
            raise HTTPException(status_code=503, detail="插件未启用或未注册 Webhook") from exc
        except Exception as exc:
            logger.exception("公开插件 Webhook 执行失败：%s", runtime.display_name(plugin_id))
            raise HTTPException(status_code=502, detail="插件 Webhook 执行失败") from exc
        if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            return JSONResponse(content=result)
        return result

    @router.post("/api/plugins/{plugin_id}/actions/{name}", dependencies=[Depends(require_admin)])
    async def plugin_action(plugin_id: str, name: str, body: PluginActionBody):
        try:
            result = await routes.dispatch_action(plugin_id, name, body.payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"插件动作执行失败：{exc}") from exc
        return {"ok": True, "result": result}
    return router, plugins
