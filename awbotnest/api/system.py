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
from .masking import masked_channels as mask_channels, masked_proxy as mask_proxy

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

    def password_matches(password: str) -> bool:
        if settings.admin_salt and settings.admin_password_hash:
            try:
                value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(settings.admin_salt), 200_000).hex()
                return hmac.compare_digest(value, settings.admin_password_hash)
            except ValueError:
                return False
        return token_matches(password, settings.admin_token)

    def masked_proxy() -> str:
        return mask_proxy(settings)

    def masked_channels() -> list[dict[str, object]]:
        return mask_channels(settings)

    @router.get("/api/status", dependencies=[Depends(require_admin)])
    async def status():
        states = [asdict(item) for item in await accounts.states()]
        metas = runtime.scan()
        activity_24h = activity.timeline(24)
        activity_7d = activity.timeline(168)
        for timeline in (activity_24h, activity_7d):
            timeline["success_totals"] = timeline.pop("successes", {})
            for bucket in timeline.get("buckets", []):
                bucket.setdefault("success_counts", {})
        plugin_names = {meta.id: meta.name for meta in metas}
        system_job_names = {"log-cleaner": "日志自动清理", "log_cleaner": "日志自动清理"}
        jobs = []
        for job in scheduler.jobs():
            owner, separator, short_name = str(job["id"]).partition("::")
            jobs.append({
                **job,
                "name": system_job_names.get(short_name or owner, short_name or owner),
                "plugin_id": None if owner == "__platform__" else owner,
                "plugin": "平台服务" if owner == "__platform__" else plugin_names.get(owner, owner),
                "next_run_at": job.get("next_run"),
                "running": job.get("running", False),
            })
        account_rows = [
            {
                **item,
                "session": item.get("id"),
                "name": item.get("display_name") or item.get("username") or item.get("id"),
                "tgid": item.get("user_id"),
                "online": bool(item.get("connected")),
                "has_session": (SESSIONS_DIR / f"{item.get('id')}.session").exists(),
                "session_exists": (SESSIONS_DIR / f"{item.get('id')}.session").exists(),
                "avatar_id": str((DATA_DIR / "avatars" / f"{item.get('id')}.jpg").stat().st_mtime_ns)
                if (DATA_DIR / "avatars" / f"{item.get('id')}.jpg").exists() else "",
                "is_premium": bool(item.get("premium")),
            }
            for item in states
            if item.get("kind") == "user"
        ]
        return {
            "version": __version__,
            "telegram_configured": settings.telegram_configured,
            "clients": states,
            "accounts": account_rows,
            "user_count": sum(1 for item in account_rows if item["online"] and item.get("kind") == "user"),
            "bot_connected": any(item.get("connected") and item.get("kind") == "bot" for item in states),
            "uptime_seconds": int(time.monotonic() - started_at),
            "resources": resource_sampler.snapshot(),
            "scheduler_jobs": jobs,
            "activity": activity_24h,
            "activity_7d": activity_7d,
            "plugin_names": plugin_names,
            "plugins": {
                "total": len(metas),
                "loaded": len(runtime.loaded),
                "enabled": len(runtime.loaded),
                "error": sum(1 for meta in metas if meta.error),
            },
        }

    @router.get("/api/auth/status")
    async def auth_status():
        return {
            "version": __version__,
            "needs_setup": not bool(settings.admin_salt and settings.admin_password_hash),
            "must_change_password": False,
            "dev_no_auth": False,
        }

    @router.post("/api/auth/login")
    async def auth_login(body: AdminLoginBody, response: Response):
        if body.username.strip() != settings.admin_username or not password_matches(body.password):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        response.set_cookie("awbotnest_resource", settings.admin_token, httponly=True,
                            samesite="lax", path="/api/plugins")
        return {"token": settings.admin_token}

    @router.post("/api/auth/setup")
    async def auth_setup(body: AdminLoginBody, response: Response):
        if settings.admin_salt and settings.admin_password_hash:
            raise HTTPException(status_code=409, detail="管理账户已经初始化")
        username = body.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="用户名不能为空")
        if len(body.password) < 4:
            raise HTTPException(status_code=400, detail="密码至少 4 位")
        settings.admin_username = username
        settings.admin_salt = secrets.token_hex(16)
        settings.admin_password_hash = hashlib.pbkdf2_hmac(
            "sha256", body.password.encode(), bytes.fromhex(settings.admin_salt), 200_000,
        ).hex()
        settings.admin_token = secrets.token_urlsafe(32)
        save_settings(settings)
        response.set_cookie("awbotnest_resource", settings.admin_token, httponly=True,
                            samesite="lax", path="/api/plugins")
        return {"token": settings.admin_token, "username": settings.admin_username}

    @router.post("/api/auth/resource_token", dependencies=[Depends(require_admin)])
    async def auth_resource_token(response: Response):
        response.set_cookie("awbotnest_resource", settings.admin_token, httponly=True,
                            samesite="lax", path="/api/plugins")
        return {"ok": True}

    @router.post("/api/auth/change_credentials", dependencies=[Depends(require_admin)])
    async def change_credentials(body: CredentialBody):
        if not password_matches(body.old_password):
            raise HTTPException(status_code=400, detail="当前密码错误")
        username = body.new_username.strip() or settings.admin_username
        if not username:
            raise HTTPException(status_code=400, detail="用户名不能为空")
        settings.admin_username = username
        if body.new_password:
            settings.admin_salt = secrets.token_hex(16)
            settings.admin_password_hash = hashlib.pbkdf2_hmac(
                "sha256", body.new_password.encode(), bytes.fromhex(settings.admin_salt), 200_000,
            ).hex()
            settings.admin_token = secrets.token_urlsafe(32)
        save_settings(settings)
        return {"ok": True, "username": username, "token": settings.admin_token}

    @router.post("/api/auth/rotate_token", dependencies=[Depends(require_admin)])
    async def rotate_admin_token():
        settings.admin_token = secrets.token_urlsafe(32)
        save_settings(settings)
        return {"ok": True, "token": settings.admin_token}

    @router.get("/api/ui/profile", dependencies=[Depends(require_admin)])
    async def ui_profile():
        avatar = next((DATA_DIR / "avatars" / f"admin{suffix}" for suffix in (".png", ".jpg", ".webp", ".gif")
                       if (DATA_DIR / "avatars" / f"admin{suffix}").exists()), None)
        return {
            "username": settings.admin_username,
            "avatar_url": f"/api/ui/avatar?v={avatar.stat().st_mtime_ns}" if avatar else "",
        }

    @router.get("/api/ui/avatar")
    async def get_ui_avatar():
        for suffix, media_type in ((".png", "image/png"), (".jpg", "image/jpeg"),
                                   (".webp", "image/webp"), (".gif", "image/gif")):
            path = DATA_DIR / "avatars" / f"admin{suffix}"
            if path.exists():
                return FileResponse(path, media_type=media_type)
        raise HTTPException(status_code=404, detail="尚未设置头像")

    @router.post("/api/ui/avatar", dependencies=[Depends(require_admin)])
    async def upload_ui_avatar(file: UploadFile = File(...)):
        content = await file.read(2 * 1024 * 1024 + 1)
        if not content or len(content) > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="头像大小不能超过 2 MB")
        signatures = (
            (".png", "image/png", content.startswith(b"\x89PNG\r\n\x1a\n")),
            (".jpg", "image/jpeg", content.startswith(b"\xff\xd8\xff")),
            (".webp", "image/webp", len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"),
            (".gif", "image/gif", content.startswith((b"GIF87a", b"GIF89a"))),
        )
        match = next(((suffix, media_type) for suffix, media_type, valid in signatures if valid), None)
        if match is None:
            raise HTTPException(status_code=400, detail="头像只支持 PNG、JPG、WebP 或 GIF 图片")
        suffix, _ = match
        avatar_dir = DATA_DIR / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        target = avatar_dir / f"admin{suffix}"
        temporary = avatar_dir / f".admin-upload{suffix}.tmp"
        temporary.write_bytes(content)
        temporary.replace(target)
        for old_suffix in (".png", ".jpg", ".webp", ".gif"):
            old = avatar_dir / f"admin{old_suffix}"
            if old != target:
                old.unlink(missing_ok=True)
        return {"status": "success", "avatar_url": f"/api/ui/avatar?v={target.stat().st_mtime_ns}"}

    @router.get("/api/ui/notifications", dependencies=[Depends(require_admin)])
    async def ui_notifications():
        values = runtime.notifier.history()
        read_at = runtime.notifier.read_at()
        plugin_names = {item.id: item.name for item in runtime.scan()}
        for item in values:
            item["plugin_name"] = plugin_names.get(str(item.get("plugin_id") or "")) or item.get("plugin_name") or "系统"
            item["plugin_icon"] = ""
            item["unread"] = float(item.get("t") or 0) > read_at
        return {"notifications": values, "unread": sum(bool(item["unread"]) for item in values)}

    @router.post("/api/ui/notifications/read", dependencies=[Depends(require_admin)])
    async def read_ui_notifications():
        runtime.notifier.mark_read()
        return {"ok": True, "unread": 0}

    @router.delete("/api/ui/notifications", dependencies=[Depends(require_admin)])
    async def clear_ui_notifications():
        runtime.notifier.clear_history()
        return {"ok": True}

    @router.get("/api/ui/about", dependencies=[Depends(require_admin)])
    async def ui_about():
        return {
            "name": "AWBotNest",
            "version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "uptime_seconds": int(time.monotonic() - started_at),
            "repository": "https://github.com/AWdress/AWBotNest",
            "issues": "https://github.com/AWdress/AWBotNest/issues",
            "docs": "https://github.com/AWdress/AWBotNest#readme",
            "versions": [{"version": __version__, "current": True, "notes": ""}],
            "latest_version": __version__,
            "version_source": "local",
        }

    @router.get("/api/ui/about/versions/{version}", dependencies=[Depends(require_admin)])
    async def ui_about_version(version: str):
        return {"version": version, "current": version.lstrip("v") == __version__.lstrip("v"), "notes": ""}

    @router.get("/api/ui/health", dependencies=[Depends(require_admin)])
    async def ui_health():
        states = [asdict(item) for item in await accounts.states()]
        user_states = [item for item in states if item.get("kind") == "user"]
        bot_online = any(item.get("connected") and item.get("kind") == "bot" for item in states)
        user_online = sum(bool(item.get("connected")) for item in user_states)
        return {"checks": [
            {"id": "platform", "name": "平台服务", "ok": True, "detail": "运行正常"},
            {"id": "scheduler", "name": "定时任务", "ok": scheduler.scheduler.running,
             "detail": f"已注册 {len(scheduler.jobs())} 个任务"},
            {"id": "telegram", "name": "Telegram", "ok": (not settings.telegram_configured) or any(item["connected"] for item in states),
             "detail": "独立模式" if not settings.telegram_configured else
             f"用户账号 {user_online}/{len(user_states)} 在线，Bot {'在线' if bot_online else '离线'}"},
            {"id": "plugins", "name": "插件运行时", "ok": not any(meta.error for meta in runtime.scan()),
             "detail": f"已加载 {len(runtime.loaded)} 个插件"},
        ]}

    @router.post("/api/ui/scheduler/{job_id:path}/run", dependencies=[Depends(require_admin)])
    async def run_scheduler_job(job_id: str):
        try:
            scheduler.run_now(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "job_id": job_id}

    @router.get("/api/ui/network-targets", dependencies=[Depends(require_admin)])
    async def get_network_targets():
        return {"targets": [{"id": key, "name": value[0], "url": value[1]}
                            for key, value in network_targets.items()]}

    @router.post("/api/ui/network-test", dependencies=[Depends(require_admin)])
    async def test_network_target(request: Request):
        target_id = str((await request.json()).get("id") or "")
        target = network_targets.get(target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="网络测试目标不存在")
        started = time.monotonic()
        try:
            response = await runtime.services.http.get(target[1], timeout=12)
            latency = round((time.monotonic() - started) * 1000)
            return {"ok": response.status_code < 500, "latency_ms": latency,
                    "detail": f"HTTP {response.status_code}"}
        except Exception as exc:
            return {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000), "detail": str(exc)}

    @router.post("/api/system/restart", dependencies=[Depends(require_admin)])
    async def restart_platform():
        if restart_event is None:
            raise HTTPException(status_code=503, detail="当前启动方式不支持页面重启")
        restart_event.set()
        return {"ok": True, "restarting": True}
    return router
