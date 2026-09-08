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

    @router.get("/api/accounts", dependencies=[Depends(require_admin)])
    async def list_accounts():
        rows = []
        for item in [asdict(value) for value in await accounts.states()]:
            if item.get("kind") != "user":
                continue
            session = str(item.get("id") or "")
            avatar = DATA_DIR / "avatars" / f"{session}.jpg"
            rows.append({
                **item,
                "session": session,
                "name": item.get("display_name") or item.get("username") or item.get("id"),
                "tgid": item.get("user_id"),
                "online": bool(item.get("connected")),
                "has_session": (SESSIONS_DIR / f"{session}.session").exists(),
                "session_exists": (SESSIONS_DIR / f"{session}.session").exists(),
                "avatar_id": str(avatar.stat().st_mtime_ns) if avatar.exists() else "",
                "is_premium": bool(item.get("premium")),
            })
        return {"accounts": rows}

    @router.get("/api/bots", dependencies=[Depends(require_admin)])
    async def list_bots():
        states = {item.id: item for item in await accounts.states() if item.kind == "bot"}
        return {"bots": [
            {"id": spec.id, "name": spec.name, "online": bool(states.get(spec.id) and states[spec.id].connected)}
            for spec in settings.bot_specs() if spec.token
        ]}

    @router.get("/api/bots/routing", dependencies=[Depends(require_admin)])
    async def get_bot_routing():
        states = {item.id: item for item in await accounts.states() if item.kind == "bot"}
        bot_rows = [
            {"id": spec.id, "name": spec.name,
             "online": bool(states.get(spec.id) and states[spec.id].connected),
             "username": states[spec.id].username if states.get(spec.id) else "", "type": "telegram"}
            for spec in settings.bot_specs() if spec.token
        ]
        known = {item["id"] for item in bot_rows}
        bot_rows.extend({"id": str(item.get("id")), "name": str(item.get("name") or item.get("id")),
                         "online": bool(item.get("enabled", True)), "username": "",
                         "type": str(item.get("type") or "webhook")}
                        for item in settings.notification_channels if item.get("id") and str(item.get("id")) not in known)
        return {"bots": bot_rows, "plugins": [
            {"id": meta.id, "name": meta.name, "scope": meta.scope,
             "bot": settings.bot_routing.get(meta.id, meta.bot or "")}
            for meta in runtime.scan()
        ]}

    @router.put("/api/bots/routing", dependencies=[Depends(require_admin)])
    async def set_bot_routing(request: Request):
        raw = await request.json()
        plugin_id = str(raw.get("plugin_id") or "")
        bot_id = str(raw.get("bot_id") or "")
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        valid_bots = {spec.id for spec in settings.bot_specs() if spec.token}
        valid_bots.update(str(item.get("id") or "") for item in settings.notification_channels
                          if item.get("enabled", True) and item.get("id"))
        selected = [item.strip() for item in bot_id.split(",") if item.strip()]
        if any(item not in valid_bots for item in selected):
            raise HTTPException(status_code=400, detail="Bot 路由包含不存在的 Bot")
        settings.bot_routing[plugin_id] = ",".join(selected)
        save_settings(settings)
        selected = settings.bot_routing[plugin_id]
        return {"ok": True, "plugin_id": plugin_id, "bot_id": selected, "bot": selected}

    @router.post("/api/accounts/login/start", dependencies=[Depends(require_admin)])
    async def start_login(body: LoginStartBody):
        logger.info("账号登录开始：%s", body.session)
        try:
            return await accounts.begin_user_login(body.session, body.phone)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConnectionError as exc:
            hint = "请在「系统设置 → 运行环境 → 运行代理」配置可访问 Telegram 的 HTTP/SOCKS 代理，保存并重启后重试"
            raise HTTPException(status_code=502, detail=f"Telegram 连接失败。{hint}") from exc
        except Exception as exc:
            logger.exception("账号登录发送验证码失败：%s", body.session)
            raise HTTPException(status_code=502, detail=f"Telegram 发送验证码失败：{exc}") from exc

    @router.post("/api/accounts/login/send_code", dependencies=[Depends(require_admin)])
    async def start_login_compat(body: LoginStartBody):
        return await start_login(body)

    @router.post("/api/accounts/login/complete", dependencies=[Depends(require_admin)])
    async def complete_login(body: LoginCompleteBody):
        logger.info("账号验证码校验开始：%s", body.session)
        try:
            result = await accounts.complete_user_login(
                body.session, code=body.code, password=body.password,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("账号登录失败：%s", body.session)
            raise HTTPException(status_code=502, detail=f"Telegram 登录失败：{exc}") from exc
        if result.get("authorized") and body.session not in settings.user_sessions:
            settings.user_sessions.append(body.session)
            save_settings(settings)
        if result.get("authorized"):
            await runtime.refresh_telegram_plugins()
            logger.info("账号登录成功：%s", body.session)
        return result

    @router.post("/api/accounts/login/submit_code", dependencies=[Depends(require_admin)])
    async def complete_login_code_compat(body: LoginCompleteBody):
        return await complete_login(body)

    @router.post("/api/accounts/login/submit_password", dependencies=[Depends(require_admin)])
    async def complete_login_password_compat(body: LoginCompleteBody):
        return await complete_login(body)

    @router.post("/api/accounts/login/{session_name}/cancel", dependencies=[Depends(require_admin)])
    async def cancel_login(session_name: str):
        await accounts.cancel_user_login(session_name)
        return {"ok": True}

    @router.post("/api/accounts/{session_name}/disconnect", dependencies=[Depends(require_admin)])
    async def disconnect_account(session_name: str):
        disconnected = await accounts.disconnect_user(session_name)
        if disconnected:
            await runtime.refresh_telegram_plugins()
        return {"ok": True, "disconnected": disconnected}

    @router.post("/api/accounts/{session_name}/offline", dependencies=[Depends(require_admin)])
    async def disconnect_account_compat(session_name: str):
        return await disconnect_account(session_name)

    @router.post("/api/accounts/{session_name}/connect", dependencies=[Depends(require_admin)])
    async def connect_account(session_name: str):
        connected = await accounts.connect_user(session_name)
        if not connected:
            raise HTTPException(status_code=409, detail="会话不存在、已失效或连接失败")
        await runtime.refresh_telegram_plugins()
        return {"ok": True, "connected": True}

    @router.post("/api/accounts/{session_name}/online", dependencies=[Depends(require_admin)])
    async def connect_account_compat(session_name: str):
        return await connect_account(session_name)

    @router.delete("/api/accounts/{session_name}", dependencies=[Depends(require_admin)])
    async def delete_account(session_name: str):
        try:
            removed = await accounts.delete_user(session_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if removed:
            await runtime.refresh_telegram_plugins()
        return {"ok": True, "removed": removed}

    @router.get("/api/accounts/{session_name}/avatar", dependencies=[Depends(require_admin)])
    async def account_avatar_compat(session_name: str):
        if not session_name.replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="会话名称不合法")
        path = DATA_DIR / "avatars" / f"{session_name}.jpg"
        if not path.exists():
            raise HTTPException(status_code=404, detail="头像不存在")
        return FileResponse(path, media_type="image/jpeg")

    @router.post("/api/accounts/{kind}/{account_id}/refresh-profile", dependencies=[Depends(require_admin)])
    async def refresh_account_profile(kind: str, account_id: str):
        if kind not in {"bot", "user"}:
            raise HTTPException(status_code=400, detail="账号类型不合法")
        if not await accounts.refresh_profile(kind, account_id):
            raise HTTPException(status_code=409, detail="账号当前未连接")
        return {"ok": True}
    return router
