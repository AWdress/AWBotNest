from __future__ import annotations

import zipfile
import logging
import json
import hmac
import time
import asyncio
import inspect
import platform
import secrets
import hashlib
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from dataclasses import asdict

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import APP_ROOT, DATA_DIR, PLUGINS_DIR, SESSIONS_DIR, BotSettings, Settings, save_settings
from .plugins import PluginRuntime
from .telegram import TelegramAccounts
from .scheduler import PluginScheduler
from .auth import admin_dependency
from .market import PluginMarket, normalize_repo
from .logs import memory_logs
from .routing import PluginRoutes, WebhookRequest
from .backup import BackupManager
from .activity import activity
from .resources import ResourceSampler
from .open_api import register_open_api

logger = logging.getLogger("awbotnest.api")


class LoginStartBody(BaseModel):
    session: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=3, max_length=32)


class AdminLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class CredentialBody(BaseModel):
    old_password: str = Field(default="", max_length=512)
    new_username: str = Field(default="", max_length=64)
    new_password: str = Field(default="", max_length=512)


class LoginCompleteBody(BaseModel):
    session: str = Field(min_length=1, max_length=64)
    code: str = Field(default="", max_length=16)
    password: str = Field(default="", max_length=256)


class SettingsBody(BaseModel):
    api_id: int = Field(default=0, ge=0)
    api_hash: str = Field(default="", max_length=128)
    bot_token: str = Field(default="", max_length=256)
    bot_name: str = Field(default="主要 Bot", max_length=64)
    default_bot_id: str = Field(default="default", max_length=64)
    default_bot_chat_id: str = Field(default="", max_length=64)
    web_host: str = Field(default="0.0.0.0", max_length=255)
    web_port: int = Field(default=18001, ge=1, le=65535)
    bots: list[dict[str, str]] = Field(default_factory=list)
    ai_base_url: str = Field(default="https://api.openai.com/v1", max_length=512)
    ai_api_key: str = Field(default="", max_length=512)
    ai_model: str = Field(default="gpt-4.1-mini", max_length=128)
    plugin_repos: list[str] = Field(default_factory=list)
    notification_channels: list[dict[str, object]] = Field(default_factory=list)
    proxy_url: str = Field(default="", max_length=512)
    webhook_secret: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=512)
    pip_index_url: str = Field(default="", max_length=1024)
    log_cleaner: dict[str, object] = Field(default_factory=dict)


class PluginConfigBody(BaseModel):
    values: dict[str, object]


class MarketInstallBody(BaseModel):
    plugin: dict[str, object]


class PluginActionBody(BaseModel):
    payload: dict[str, object] = Field(default_factory=dict)


class NotificationTestBody(BaseModel):
    channel: str = Field(min_length=1, max_length=64)
    text: str = Field(default="AWBotNest 通知渠道测试成功", max_length=2000)


class CookieBody(BaseModel):
    values: dict[str, str]


def create_app(settings: Settings, accounts: TelegramAccounts,
               runtime: PluginRuntime, scheduler: PluginScheduler,
               routes: PluginRoutes,
               restart_event: asyncio.Event | None = None,
               market: PluginMarket | None = None) -> FastAPI:
    app = FastAPI(title="AWBotNest API", version=__version__)
    require_admin = admin_dependency(settings)
    market = market or PluginMarket(settings)
    started_at = time.monotonic()
    resource_sampler = ResourceSampler()
    register_open_api(app, settings, accounts, runtime)

    def password_matches(password: str) -> bool:
        if settings.admin_salt and settings.admin_password_hash:
            try:
                value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(settings.admin_salt), 200_000).hex()
                return hmac.compare_digest(value, settings.admin_password_hash)
            except ValueError:
                return False
        return hmac.compare_digest(password, settings.admin_token)

    def masked_proxy() -> str:
        if not settings.proxy_url:
            return ""
        value = urlparse(settings.proxy_url)
        if not value.password:
            return settings.proxy_url
        host = value.hostname or ""
        if value.port:
            host += f":{value.port}"
        auth = f"{value.username or ''}:********@"
        return urlunparse((value.scheme, auth + host, value.path, "", value.query, ""))

    def masked_channels() -> list[dict[str, object]]:
        result = []
        bot_tokens = {item.id: item.token for item in settings.bot_specs()}
        metadata = {"id", "name", "type", "enabled", "is_default", "plugins", "config"}
        for source in settings.notification_channels:
            item = dict(source)
            nested = dict(item.get("config") or {}) if isinstance(item.get("config"), dict) else {}
            config = {**{key: value for key, value in item.items() if key not in metadata}, **nested}
            channel_type = "wechat" if item.get("type") == "wecom" else str(item.get("type") or "telegram")
            channel_id = str(item.get("id") or "")
            if channel_type == "telegram" and bot_tokens.get(channel_id):
                config["token"] = "********"
            for key in ("url", "webhook", "server", "token", "password", "secret", "device_key"):
                if config.get(key):
                    config[key] = "********"
            result.append({
                "id": channel_id, "name": str(item.get("name") or channel_id), "type": channel_type,
                "enabled": item.get("enabled", True), "is_default": bool(item.get("is_default")),
                "config": config,
            })
        return result

    @app.get("/api/status", dependencies=[Depends(require_admin)])
    async def status():
        states = [asdict(item) for item in await accounts.states()]
        activity_24h = activity.timeline(24)
        activity_7d = activity.timeline(168)
        for timeline in (activity_24h, activity_7d):
            timeline["success_totals"] = timeline.pop("successes", {})
            for bucket in timeline.get("buckets", []):
                bucket.setdefault("success_counts", {})
        plugin_names = {meta.id: meta.name for meta in runtime.scan()}
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
                "running": False,
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
                "total": len(runtime.scan()),
                "loaded": len(runtime.loaded),
                "enabled": len(runtime.loaded),
                "error": sum(1 for meta in runtime.scan() if meta.error),
            },
        }

    @app.get("/api/auth/status")
    async def auth_status():
        return {
            "version": __version__,
            "needs_setup": not bool(settings.admin_salt and settings.admin_password_hash),
            "must_change_password": False,
            "dev_no_auth": False,
        }

    @app.post("/api/auth/login")
    async def auth_login(body: AdminLoginBody, response: Response):
        if body.username.strip() != settings.admin_username or not password_matches(body.password):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        response.set_cookie("awbotnest_resource", settings.admin_token, httponly=True,
                            samesite="lax", path="/api/plugins")
        return {"token": settings.admin_token}

    @app.post("/api/auth/setup")
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

    @app.post("/api/auth/resource_token", dependencies=[Depends(require_admin)])
    async def auth_resource_token(response: Response):
        response.set_cookie("awbotnest_resource", settings.admin_token, httponly=True,
                            samesite="lax", path="/api/plugins")
        return {"ok": True}

    @app.post("/api/auth/change_credentials", dependencies=[Depends(require_admin)])
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

    @app.post("/api/auth/rotate_token", dependencies=[Depends(require_admin)])
    async def rotate_admin_token():
        settings.admin_token = secrets.token_urlsafe(32)
        save_settings(settings)
        return {"ok": True, "token": settings.admin_token}

    @app.get("/api/ui/profile", dependencies=[Depends(require_admin)])
    async def ui_profile():
        avatar = next((DATA_DIR / "avatars" / f"admin{suffix}" for suffix in (".png", ".jpg", ".webp", ".gif")
                       if (DATA_DIR / "avatars" / f"admin{suffix}").exists()), None)
        return {
            "username": settings.admin_username,
            "avatar_url": f"/api/ui/avatar?v={avatar.stat().st_mtime_ns}" if avatar else "",
        }

    @app.get("/api/ui/avatar")
    async def get_ui_avatar():
        for suffix, media_type in ((".png", "image/png"), (".jpg", "image/jpeg"),
                                   (".webp", "image/webp"), (".gif", "image/gif")):
            path = DATA_DIR / "avatars" / f"admin{suffix}"
            if path.exists():
                return FileResponse(path, media_type=media_type)
        raise HTTPException(status_code=404, detail="尚未设置头像")

    @app.post("/api/ui/avatar", dependencies=[Depends(require_admin)])
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

    @app.get("/api/ui/notifications", dependencies=[Depends(require_admin)])
    async def ui_notifications():
        values = runtime.notifier.history()
        read_at = runtime.notifier.read_at()
        plugin_names = {item.id: item.name for item in runtime.scan()}
        for item in values:
            item["plugin_name"] = item.get("plugin_name") or plugin_names.get(str(item.get("plugin_id") or ""), "系统")
            item["plugin_icon"] = ""
            item["unread"] = float(item.get("t") or 0) > read_at
        return {"notifications": values, "unread": sum(bool(item["unread"]) for item in values)}

    @app.post("/api/ui/notifications/read", dependencies=[Depends(require_admin)])
    async def read_ui_notifications():
        runtime.notifier.mark_read()
        return {"ok": True, "unread": 0}

    @app.delete("/api/ui/notifications", dependencies=[Depends(require_admin)])
    async def clear_ui_notifications():
        runtime.notifier.clear_history()
        return {"ok": True}

    @app.get("/api/ui/about", dependencies=[Depends(require_admin)])
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

    @app.get("/api/ui/about/versions/{version}", dependencies=[Depends(require_admin)])
    async def ui_about_version(version: str):
        return {"version": version, "current": version.lstrip("v") == __version__.lstrip("v"), "notes": ""}

    @app.get("/api/ui/health", dependencies=[Depends(require_admin)])
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

    @app.post("/api/ui/scheduler/{job_id:path}/run", dependencies=[Depends(require_admin)])
    async def run_scheduler_job(job_id: str):
        job = scheduler.scheduler.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="定时任务不存在")

        async def execute_job():
            try:
                result = job.func(*job.args, **job.kwargs)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("手动运行定时任务失败：%s", job_id)

        asyncio.create_task(execute_job())
        return {"ok": True, "job_id": job_id}

    network_targets = {
        "telegram": ("Telegram API", "https://api.telegram.org"),
        "github": ("GitHub API", "https://api.github.com"),
        "plugin_store": ("插件市场", "https://raw.githubusercontent.com"),
    }

    @app.get("/api/ui/network-targets", dependencies=[Depends(require_admin)])
    async def get_network_targets():
        return {"targets": [{"id": key, "name": value[0], "url": value[1]}
                            for key, value in network_targets.items()]}

    @app.post("/api/ui/network-test", dependencies=[Depends(require_admin)])
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

    @app.post("/api/system/restart", dependencies=[Depends(require_admin)])
    async def restart_platform():
        if restart_event is None:
            raise HTTPException(status_code=503, detail="当前启动方式不支持页面重启")
        restart_event.set()
        return {"ok": True, "restarting": True}

    @app.get("/api/plugins", dependencies=[Depends(require_admin)])
    async def plugins():
        market_data = await market.list_all()
        market_items = {
            item["id"]: item for item in market_data.get("plugins", [])
        }
        official_ids = set(market_data.get("official_ids") or [])
        values = []
        for meta in runtime.scan():
            item = meta.to_dict()
            market_item = market_items.get(meta.id, {})
            item["icon"] = item.get("icon") or market_item.get("icon") or ""
            item["official"] = meta.id in official_ids or bool(market_item.get("official"))
            item["install_count"] = max(0, int(market_item.get("install_count") or 0))
            item.update(routes.describe(meta.id))
            values.append(item)
        order = [item for item in settings.plugin_order if any(meta["id"] == item for meta in values)]
        rank = {plugin_id: index for index, plugin_id in enumerate(order)}
        values.sort(key=lambda item: (rank.get(item["id"], len(rank)), item.get("name") or item["id"]))
        return {"plugins": values, "official_ids": list(official_ids), "custom_order": bool(order)}

    @app.put("/api/plugins/order", dependencies=[Depends(require_admin)])
    async def save_plugin_order(request: Request):
        raw = await request.json()
        order = raw.get("order")
        known = {meta.id for meta in runtime.scan()}
        if not isinstance(order, list) or set(map(str, order)) != known or len(order) != len(known):
            raise HTTPException(status_code=400, detail="插件顺序与当前插件不一致")
        settings.plugin_order = [str(item) for item in order]
        save_settings(settings)
        return {"ok": True, "order": settings.plugin_order}

    # Dynamic plugin routes support several verbs through one dispatcher. Keeping
    # them out of OpenAPI avoids duplicate operation IDs; plugin-specific schemas
    # cannot be represented accurately by the platform document anyway.
    @app.api_route("/api/plugin/{plugin_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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
            logger.exception("插件 Webhook 执行失败：%s/%s", plugin_id, path)
            raise HTTPException(status_code=502, detail="插件 Webhook 执行失败") from exc
        if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            return JSONResponse(content=result)
        return result

    @app.api_route("/api/v1/webhook", methods=["GET", "POST"], include_in_schema=False)
    async def platform_webhook(request: Request):
        """共享密钥鉴权后，把外部内容送入平台通知服务。"""
        secret = settings.webhook_secret.strip()
        if not secret:
            raise HTTPException(status_code=404, detail="Webhook 未开启")
        supplied = str(request.query_params.get("apikey") or "")
        if not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8")):
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
            logger.exception("平台 Webhook 通知失败")
            raise HTTPException(status_code=502, detail="Webhook 通知投递失败") from exc
        return {"ok": True}

    @app.post("/api/plugins/{plugin_id}/actions/{name}", dependencies=[Depends(require_admin)])
    async def plugin_action(plugin_id: str, name: str, body: PluginActionBody):
        try:
            result = await routes.dispatch_action(plugin_id, name, body.payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"插件动作执行失败：{exc}") from exc
        return {"ok": True, "result": result}

    @app.get("/api/settings", dependencies=[Depends(require_admin)])
    async def get_settings():
        current = {
            "api_id": settings.api_id,
            "api_hash": "********" if settings.api_hash else "",
            "bot_token": "********" if settings.bot_token else "",
            "bot_name": settings.bot_name,
            "default_bot_id": settings.default_bot_id,
            "default_bot_chat_id": settings.default_bot_chat_id,
            "bots": [
                {"id": bot.id, "name": bot.name, "token": "********" if bot.token else ""}
                for bot in settings.bots
            ],
            "web_host": settings.web_host,
            "web_port": settings.web_port,
            "ai_base_url": settings.ai_base_url,
            "ai_api_key": "********" if settings.ai_api_key else "",
            "ai_model": settings.ai_model,
            "plugin_repos": settings.plugin_repos,
            "notification_channels": masked_channels(),
            "proxy_url": masked_proxy(),
        }
        return {
            "settings": {
                "API_ID": current["api_id"],
                "API_HASH": current["api_hash"],
                "BOT_TOKEN": current["bot_token"],
                "BOT_NAME": current["bot_name"],
                "DEFAULT_BOT_ID": current["default_bot_id"],
                "DEFAULT_BOT_CHAT_ID": current["default_bot_chat_id"],
                "BOTS": current["bots"],
                "WEB_UI_PORT": current["web_port"],
                "WEB_UI_URL": current["web_host"],
                "ACCOUNTS": [],
                "NOTIFICATION_CHANNELS": current["notification_channels"],
                "proxy_set": {"proxy_enable": bool(settings.proxy_url), "PROXY_URL": current["proxy_url"], "proxy": {}},
                "PIP_INDEX_URL": settings.pip_index_url,
                "DB_INFO": {"dbset": "SQLite", "db_name": "awbotnest"},
                "LOG_CLEANER": dict(settings.log_cleaner),
                "WEBHOOK_SECRET": "********" if settings.webhook_secret else "",
                "API_KEY": "********" if settings.api_key else "",
                "PLUGIN_REPOS": current["plugin_repos"],
            }
        }

    def current_ai_settings() -> dict[str, object]:
        if settings.ai_settings:
            return dict(settings.ai_settings)
        provider_id = "default"
        return {
            "providers": [{"id": provider_id, "name": "OpenAI 兼容服务", "enabled": True,
                           "base_url": settings.ai_base_url, "api_key": "********" if settings.ai_api_key else ""}],
            "models": [{"id": "default", "alias": settings.ai_model, "name": settings.ai_model,
                        "enabled": True, "provider_id": provider_id, "model": settings.ai_model,
                        "capabilities": ["text"]}],
            "capabilities": {"text": {"default_model": settings.ai_model}, "vision": {}, "image": {}},
            "plugin_permissions": {},
        }

    @app.get("/api/ai/settings", dependencies=[Depends(require_admin)])
    async def get_ai_settings():
        value = current_ai_settings()
        safe = json.loads(json.dumps(value))
        for provider in safe.get("providers", []):
            if provider.get("api_key"):
                provider["api_key"] = "********"
        return {"settings": safe, "status": {"configured": bool(settings.ai_api_key)}}

    @app.put("/api/ai/settings", dependencies=[Depends(require_admin)])
    async def save_ai_settings(request: Request):
        raw = await request.json()
        value = raw.get("settings")
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="AI 设置格式不正确")
        old_keys = {str(item.get("id")): str(item.get("api_key") or "")
                    for item in settings.ai_settings.get("providers", []) if isinstance(item, dict)}
        for provider in value.get("providers", []):
            if isinstance(provider, dict) and provider.get("api_key") == "********":
                provider["api_key"] = old_keys.get(str(provider.get("id")), settings.ai_api_key)
        settings.ai_settings = value
        provider = next((item for item in value.get("providers", []) if isinstance(item, dict) and item.get("enabled", True)), None)
        if provider:
            settings.ai_base_url = str(provider.get("base_url") or settings.ai_base_url)
            settings.ai_api_key = str(provider.get("api_key") or "")
        model = next((item for item in value.get("models", []) if isinstance(item, dict) and item.get("enabled", True)), None)
        if model:
            settings.ai_model = str(model.get("model") or model.get("alias") or settings.ai_model)
        save_settings(settings)
        return await get_ai_settings()

    @app.get("/api/ai/plugins", dependencies=[Depends(require_admin)])
    async def list_ai_plugins():
        return {"plugins": [{"id": item.id, "name": item.name} for item in runtime.scan()]}

    @app.get("/api/ai/status", dependencies=[Depends(require_admin)])
    async def ai_status():
        return {"configured": bool(settings.ai_api_key), "base_url": settings.ai_base_url, "model": settings.ai_model}

    @app.post("/api/ai/test", dependencies=[Depends(require_admin)])
    async def test_ai_capability(request: Request):
        capability = str((await request.json()).get("capability") or "text")
        if capability != "text":
            raise HTTPException(status_code=409, detail=f"当前 AI 服务暂未配置 {capability} 能力")
        try:
            result = await runtime.services.ai.chat([{"role": "user", "content": "Reply with OK only."}])
            return {"ok": True, "capability": capability, "result": result[:200]}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AI 测试失败：{exc}") from exc

    @app.post("/api/ai/provider-models", dependencies=[Depends(require_admin)])
    async def ai_provider_models(request: Request):
        raw = await request.json()
        provider_id = str(raw.get("provider") or "")
        provider = next((item for item in current_ai_settings().get("providers", [])
                         if isinstance(item, dict) and str(item.get("id")) == provider_id), None)
        if not provider:
            raise HTTPException(status_code=404, detail="AI 服务不存在")
        api_key = str(provider.get("api_key") or settings.ai_api_key)
        try:
            response = await runtime.services.http.get(
                f"{str(provider.get('base_url') or settings.ai_base_url).rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=20,
            )
            response.raise_for_status()
            values = response.json().get("data", [])
            return {"models": [str(item.get("id")) for item in values if isinstance(item, dict) and item.get("id")]}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取模型失败：{exc}") from exc

    @app.get("/api/cookies/settings", dependencies=[Depends(require_admin)])
    async def get_cookie_settings():
        value = dict(settings.cookie_settings)
        for key in ("uuid", "password", "token"):
            if value.get(key):
                value[key] = "********"
        domains = await runtime.services.cookies.domains()
        return {"settings": value, "status": {"domain_count": len(domains)}, "history": [],
                "server_path": "/cookiecloud"}

    @app.put("/api/cookies/settings", dependencies=[Depends(require_admin)])
    async def save_cookie_settings(request: Request):
        raw = await request.json()
        value = raw.get("settings")
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Cookie 设置格式不正确")
        for key in ("uuid", "password", "token"):
            if value.get(key) == "********":
                value[key] = settings.cookie_settings.get(key, "")
        settings.cookie_settings = value
        save_settings(settings)
        return await get_cookie_settings()

    @app.post("/api/cookies/credentials", dependencies=[Depends(require_admin)])
    async def generate_cookie_credentials():
        settings.cookie_settings["uuid"] = secrets.token_urlsafe(12)
        settings.cookie_settings["password"] = secrets.token_urlsafe(24)
        save_settings(settings)
        return {"uuid": settings.cookie_settings["uuid"], "password": settings.cookie_settings["password"]}

    @app.post("/api/cookies/check", dependencies=[Depends(require_admin)])
    async def check_cookie_sync():
        domains = await runtime.services.cookies.domains()
        return {"ok": True, "domain_count": len(domains), "detail": "Cookie 存储可用"}

    @app.post("/api/cookies/remote-sync", dependencies=[Depends(require_admin)])
    async def sync_remote_cookies():
        raise HTTPException(status_code=409, detail="2.0 使用本机 Cookie 存储；远程 CookieCloud 拉取尚未配置")

    @app.delete("/api/cookies/data", dependencies=[Depends(require_admin)])
    async def clear_cookie_data():
        path = DATA_DIR / "cookies.json"
        path.unlink(missing_ok=True)
        return {"ok": True}

    @app.put("/api/settings", dependencies=[Depends(require_admin)])
    async def update_settings(request: Request):
        raw = await request.json()
        if isinstance(raw.get("settings"), dict):
            legacy = raw["settings"]
            proxy = legacy.get("proxy_set") or {}
            raw = {
                "api_id": legacy.get("API_ID", settings.api_id),
                "api_hash": legacy.get("API_HASH", "********" if settings.api_hash else ""),
                "bot_token": legacy.get("BOT_TOKEN", "********" if settings.bot_token else ""),
                "bot_name": legacy.get("BOT_NAME", settings.bot_name),
                "default_bot_id": legacy.get("DEFAULT_BOT_ID", settings.default_bot_id),
                "default_bot_chat_id": legacy.get("DEFAULT_BOT_CHAT_ID", settings.default_bot_chat_id),
                "web_host": legacy.get("WEB_UI_URL", settings.web_host),
                "web_port": legacy.get("WEB_UI_PORT", settings.web_port),
                "bots": legacy.get("BOTS", []),
                "ai_base_url": settings.ai_base_url,
                "ai_api_key": "********" if settings.ai_api_key else "",
                "ai_model": settings.ai_model,
                "plugin_repos": legacy.get("PLUGIN_REPOS", settings.plugin_repos),
                "notification_channels": legacy.get("NOTIFICATION_CHANNELS", settings.notification_channels),
                "proxy_url": proxy.get("PROXY_URL", settings.proxy_url) if proxy.get("proxy_enable") else "",
                "webhook_secret": legacy.get("WEBHOOK_SECRET", "********" if settings.webhook_secret else ""),
                "api_key": legacy.get("API_KEY", "********" if settings.api_key else ""),
                "pip_index_url": legacy.get("PIP_INDEX_URL", settings.pip_index_url),
                "log_cleaner": legacy.get("LOG_CLEANER", settings.log_cleaner),
            }
        try:
            body = SettingsBody.model_validate(raw)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"设置格式不正确：{exc}") from exc
        previous = (settings.api_id, settings.api_hash, settings.bot_token, settings.proxy_url,
                    settings.default_bot_id, settings.web_host, settings.web_port,
                    [(item.id, item.token) for item in settings.bots], dict(settings.log_cleaner))
        existing_tokens = {item.id: item.token for item in settings.bots}
        new_bots = [BotSettings(
            id=str(item.get("id") or "").strip(),
            name=str(item.get("name") or item.get("id") or "Bot").strip(),
            token=(existing_tokens.get(str(item.get("id") or ""), "")
                   if item.get("token") == "********" else str(item.get("token") or "").strip()),
        ) for item in body.bots if str(item.get("id") or "").strip() and item.get("id") != "default"]
        bot_ids = [item.id for item in new_bots]
        if len(bot_ids) != len(set(bot_ids)) or any(not item.replace("_", "").replace("-", "").isalnum() for item in bot_ids):
            raise HTTPException(status_code=400, detail="Bot ID 必须唯一，且只能包含字母、数字、横线和下划线")
        if (body.default_bot_id.strip() or "default") not in {"default", *bot_ids}:
            raise HTTPException(status_code=400, detail="默认 Bot 不存在")
        try:
            new_repos = [normalize_repo(item) for item in body.plugin_repos if item.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        current_channels = {str(item.get("id") or ""): item for item in settings.notification_channels}
        channels = []
        for raw_channel in body.notification_channels:
            nested = raw_channel.get("config") if isinstance(raw_channel.get("config"), dict) else {}
            item = {**nested, **raw_channel}
            item.pop("config", None)
            previous_channel = current_channels.get(str(item.get("id") or ""), {})
            previous_nested = (previous_channel.get("config")
                               if isinstance(previous_channel.get("config"), dict) else {})
            for key in ("url", "webhook", "server", "token", "password", "secret", "device_key"):
                if item.get(key) == "********":
                    item[key] = previous_channel.get(key, previous_nested.get(key, ""))
            if item.get("type") == "wechat":
                item["type"] = "wecom"
            if item.get("type") == "telegram":
                # Telegram tokens are owned by the Bot settings above. Keeping
                # a masked duplicate in the channel would corrupt later saves.
                item.pop("token", None)
                item["bot_id"] = str(item.get("id") or "")
            channels.append(item)
        channel_ids = [str(item.get("id") or "") for item in channels]
        allowed_channels = {"telegram", "bark", "wecom", "wechat", "webhook"}
        if any(not item for item in channel_ids) or len(channel_ids) != len(set(channel_ids)):
            raise HTTPException(status_code=400, detail="通知渠道 ID 不能为空且必须唯一")
        if any(str(item.get("type") or "") not in allowed_channels for item in channels):
            raise HTTPException(status_code=400, detail="通知渠道类型不受支持")
        proxy_url = body.proxy_url.strip()
        if "********" in proxy_url:
            proxy_url = settings.proxy_url
        if proxy_url:
            parsed_proxy = urlparse(proxy_url)
            if parsed_proxy.scheme not in {"http", "socks4", "socks5"} or not parsed_proxy.hostname or not parsed_proxy.port:
                raise HTTPException(status_code=400, detail="代理地址必须是完整的 http/socks4/socks5 URL")
        settings.api_id = body.api_id
        if body.api_hash != "********":
            settings.api_hash = body.api_hash.strip()
        if body.bot_token != "********":
            settings.bot_token = body.bot_token.strip()
        settings.bot_name = body.bot_name.strip() or "主要 Bot"
        settings.default_bot_id = body.default_bot_id.strip() or "default"
        settings.default_bot_chat_id = body.default_bot_chat_id.strip()
        settings.web_host = body.web_host.strip() or "0.0.0.0"
        settings.web_port = body.web_port
        settings.bots = new_bots
        settings.ai_base_url = body.ai_base_url.strip() or "https://api.openai.com/v1"
        if body.ai_api_key != "********":
            settings.ai_api_key = body.ai_api_key.strip()
        settings.ai_model = body.ai_model.strip() or "gpt-4.1-mini"
        settings.plugin_repos = new_repos
        settings.notification_channels = channels
        settings.proxy_url = proxy_url
        if body.webhook_secret != "********":
            settings.webhook_secret = body.webhook_secret.strip()
        if body.api_key != "********":
            settings.api_key = body.api_key.strip()
        pip_index_url = body.pip_index_url.strip()
        if pip_index_url:
            parsed_index = urlparse(pip_index_url)
            if parsed_index.scheme not in {"http", "https"} or not parsed_index.hostname:
                raise HTTPException(status_code=400, detail="pip 镜像源必须是完整的 http/https URL")
        settings.pip_index_url = pip_index_url
        cleaner = dict(body.log_cleaner or settings.log_cleaner)
        try:
            settings.log_cleaner = {
                "enabled": bool(cleaner.get("enabled", True)),
                "keep_lines": max(1, min(int(cleaner.get("keep_lines", 1000)), 1000)),
                "hour": max(0, min(int(cleaner.get("hour", 3)), 23)),
                "minute": max(0, min(int(cleaner.get("minute", 0)), 59)),
            }
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="日志清理设置格式不正确") from exc
        save_settings(settings)
        market.clear_cache()
        current = (settings.api_id, settings.api_hash, settings.bot_token, settings.proxy_url,
                   settings.default_bot_id, settings.web_host, settings.web_port,
                   [(item.id, item.token) for item in settings.bots], dict(settings.log_cleaner))
        return {"ok": True, "restart_required": current != previous}

    @app.put("/api/settings/notification-channels", dependencies=[Depends(require_admin)])
    async def save_notification_channels(request: Request):
        raw = await request.json()
        channels = raw.get("channels")
        if not isinstance(channels, list):
            raise HTTPException(status_code=400, detail="通知渠道格式不正确")
        normalized = []
        existing_tokens = {item.id: item.token for item in settings.bot_specs()}
        existing_channels = {str(item.get("id") or ""): dict(item) for item in settings.notification_channels}
        new_bots: list[BotSettings] = []
        default_token = settings.bot_token
        default_name = settings.bot_name
        default_chat_id = settings.default_bot_chat_id
        default_id = settings.default_bot_id
        for source in channels:
            if not isinstance(source, dict):
                continue
            nested = source.get("config") if isinstance(source.get("config"), dict) else {}
            item = {**nested, **source}
            item.pop("config", None)
            previous = existing_channels.get(str(item.get("id") or ""), {})
            previous_nested = previous.get("config") if isinstance(previous.get("config"), dict) else {}
            for key in ("url", "webhook", "server", "token", "password", "secret", "device_key"):
                if item.get(key) == "********":
                    item[key] = previous.get(key, previous_nested.get(key, ""))
            if item.get("type") == "wechat":
                item["type"] = "wecom"
            if item.get("type") == "telegram":
                channel_id = str(item.get("id") or "")
                token = str(item.get("token") or "")
                if token == "********":
                    token = existing_tokens.get(channel_id, "")
                item.pop("token", None)
                if channel_id == "default":
                    default_token = token
                    default_name = str(item.get("name") or default_name)
                    default_chat_id = str(item.get("chat_id") or "")
                elif channel_id:
                    new_bots.append(BotSettings(channel_id, str(item.get("name") or channel_id), token))
                if item.get("is_default") and item.get("enabled", True):
                    default_id = channel_id
                item["bot_id"] = channel_id
            normalized.append(item)
        settings.notification_channels = normalized
        settings.bot_token = default_token
        settings.bot_name = default_name
        settings.default_bot_chat_id = default_chat_id
        settings.default_bot_id = default_id if default_id in {"default", *(bot.id for bot in new_bots)} else "default"
        settings.bots = new_bots
        save_settings(settings)
        return {"ok": True, "channels": masked_channels(), "restart_required": True}

    @app.post("/api/settings/reveal-secret", dependencies=[Depends(require_admin)])
    async def reveal_secret(request: Request):
        raw = await request.json()
        kind, field, item_id = str(raw.get("kind") or ""), str(raw.get("field") or ""), str(raw.get("id") or "")
        value = ""
        if kind == "system":
            value = {"API_HASH": settings.api_hash, "BOT_TOKEN": settings.bot_token,
                     "API_KEY": settings.api_key,
                     "WEBHOOK_SECRET": settings.webhook_secret}.get(field, "")
        elif kind == "ai":
            provider = next((item for item in current_ai_settings().get("providers", [])
                             if isinstance(item, dict) and str(item.get("id")) == item_id), None)
            value = str((provider or {}).get(field) or "")
        elif kind == "cookie":
            value = str(settings.cookie_settings.get(field) or "")
        elif kind == "channel":
            channel = next((item for item in settings.notification_channels if str(item.get("id")) == item_id), None)
            nested = channel.get("config") if isinstance((channel or {}).get("config"), dict) else {}
            value = str((channel or {}).get(field, nested.get(field, "")) or "")
        if not value:
            raise HTTPException(status_code=404, detail="密钥不存在")
        return {"value": value}

    @app.post("/api/settings/test_proxy", dependencies=[Depends(require_admin)])
    async def test_proxy(request: Request):
        raw = await request.json()
        proxy_set = raw.get("proxy_set") or {}
        proxy_url = str(proxy_set.get("PROXY_URL") or "") if proxy_set.get("proxy_enable") else ""
        started = time.monotonic()
        try:
            import httpx
            async with httpx.AsyncClient(proxy=proxy_url or None, timeout=10) as client:
                response = await client.get("https://api.telegram.org")
            return {"ok": response.status_code < 500, "latency_ms": round((time.monotonic() - started) * 1000)}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    @app.post("/api/settings/test_db", dependencies=[Depends(require_admin)])
    async def test_db():
        return {"ok": True, "detail": "2.0 使用平台内置 SQLite 存储"}

    @app.get("/api/accounts", dependencies=[Depends(require_admin)])
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

    @app.get("/api/bots", dependencies=[Depends(require_admin)])
    async def list_bots():
        states = {item.id: item for item in await accounts.states() if item.kind == "bot"}
        return {"bots": [
            {"id": spec.id, "name": spec.name, "online": bool(states.get(spec.id) and states[spec.id].connected)}
            for spec in settings.bot_specs() if spec.token
        ]}

    @app.get("/api/bots/routing", dependencies=[Depends(require_admin)])
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

    @app.put("/api/bots/routing", dependencies=[Depends(require_admin)])
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

    @app.post("/api/accounts/login/start", dependencies=[Depends(require_admin)])
    async def start_login(body: LoginStartBody):
        try:
            return await accounts.begin_user_login(body.session, body.phone)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConnectionError as exc:
            hint = "请在「系统设置 → 运行环境 → 运行代理」配置可访问 Telegram 的 HTTP/SOCKS 代理，保存并重启后重试"
            raise HTTPException(status_code=502, detail=f"Telegram 连接失败。{hint}") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Telegram 发送验证码失败：{exc}") from exc

    @app.post("/api/accounts/login/send_code", dependencies=[Depends(require_admin)])
    async def start_login_compat(body: LoginStartBody):
        return await start_login(body)

    @app.post("/api/accounts/login/complete", dependencies=[Depends(require_admin)])
    async def complete_login(body: LoginCompleteBody):
        try:
            result = await accounts.complete_user_login(
                body.session, code=body.code, password=body.password,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Telegram 登录失败：{exc}") from exc
        if result.get("authorized") and body.session not in settings.user_sessions:
            settings.user_sessions.append(body.session)
            save_settings(settings)
        if result.get("authorized"):
            await runtime.refresh_telegram_plugins()
        return result

    @app.post("/api/accounts/login/submit_code", dependencies=[Depends(require_admin)])
    async def complete_login_code_compat(body: LoginCompleteBody):
        return await complete_login(body)

    @app.post("/api/accounts/login/submit_password", dependencies=[Depends(require_admin)])
    async def complete_login_password_compat(body: LoginCompleteBody):
        return await complete_login(body)

    @app.post("/api/accounts/login/{session_name}/cancel", dependencies=[Depends(require_admin)])
    async def cancel_login(session_name: str):
        await accounts.cancel_user_login(session_name)
        return {"ok": True}

    @app.post("/api/accounts/{session_name}/disconnect", dependencies=[Depends(require_admin)])
    async def disconnect_account(session_name: str):
        disconnected = await accounts.disconnect_user(session_name)
        if disconnected:
            await runtime.refresh_telegram_plugins()
        return {"ok": True, "disconnected": disconnected}

    @app.post("/api/accounts/{session_name}/offline", dependencies=[Depends(require_admin)])
    async def disconnect_account_compat(session_name: str):
        return await disconnect_account(session_name)

    @app.post("/api/accounts/{session_name}/connect", dependencies=[Depends(require_admin)])
    async def connect_account(session_name: str):
        connected = await accounts.connect_user(session_name)
        if not connected:
            raise HTTPException(status_code=409, detail="会话不存在、已失效或连接失败")
        await runtime.refresh_telegram_plugins()
        return {"ok": True, "connected": True}

    @app.post("/api/accounts/{session_name}/online", dependencies=[Depends(require_admin)])
    async def connect_account_compat(session_name: str):
        return await connect_account(session_name)

    @app.delete("/api/accounts/{session_name}", dependencies=[Depends(require_admin)])
    async def delete_account(session_name: str):
        try:
            removed = await accounts.delete_user(session_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if removed:
            await runtime.refresh_telegram_plugins()
        return {"ok": True, "removed": removed}

    @app.get("/api/accounts/{session_name}/avatar", dependencies=[Depends(require_admin)])
    async def account_avatar_compat(session_name: str):
        if not session_name.replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="会话名称不合法")
        path = DATA_DIR / "avatars" / f"{session_name}.jpg"
        if not path.exists():
            raise HTTPException(status_code=404, detail="头像不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/accounts/{kind}/{account_id}/refresh-profile", dependencies=[Depends(require_admin)])
    async def refresh_account_profile(kind: str, account_id: str):
        if kind not in {"bot", "user"}:
            raise HTTPException(status_code=400, detail="账号类型不合法")
        if not await accounts.refresh_profile(kind, account_id):
            raise HTTPException(status_code=409, detail="账号当前未连接")
        return {"ok": True}

    @app.post("/api/plugins/{plugin_id}/enable", dependencies=[Depends(require_admin)])
    async def enable_plugin(plugin_id: str):
        try:
            meta = await runtime.enable(plugin_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if meta.error:
            raise HTTPException(status_code=409, detail=meta.error)
        value = meta.to_dict()
        return {**value, "plugin": value}

    @app.post("/api/plugins/{plugin_id}/disable", dependencies=[Depends(require_admin)])
    async def disable_plugin(plugin_id: str):
        await runtime.disable(plugin_id)
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        value = meta.to_dict() if meta else {"id": plugin_id, "enabled": False, "loaded": False}
        value["enabled"] = False
        value["loaded"] = False
        return {"ok": True, "plugin": value}

    @app.post("/api/plugins/{plugin_id}/reload", dependencies=[Depends(require_admin)])
    async def reload_plugin(plugin_id: str):
        await runtime.disable(plugin_id)
        meta = await runtime.enable(plugin_id)
        if meta.error:
            raise HTTPException(status_code=409, detail=meta.error)
        return {"ok": True, "plugin": meta.to_dict()}

    @app.post("/api/plugins/{plugin_id}/self-check", dependencies=[Depends(require_admin)])
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

    @app.get("/api/plugins/dependencies", dependencies=[Depends(require_admin)])
    async def plugin_dependencies():
        nodes = []
        for meta in runtime.scan():
            nodes.append({"id": meta.id, "name": meta.name, "scope": meta.scope,
                          "requirements": meta.requirements or [], "enabled": meta.enabled})
        return {"nodes": nodes, "edges": []}

    @app.get("/api/plugins/{plugin_id}/accounts", dependencies=[Depends(require_admin)])
    async def get_plugin_accounts(plugin_id: str):
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        options = [
            {"session": item.id, "name": item.display_name or item.username or item.id}
            for item in await accounts.states() if item.kind == "user"
        ]
        return {"accounts": options, "selected": settings.plugin_accounts.get(plugin_id, []), "scope": meta.scope}

    @app.put("/api/plugins/{plugin_id}/accounts", dependencies=[Depends(require_admin)])
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
        return {"ok": True, "selected": settings.plugin_accounts[plugin_id]}

    @app.get("/api/plugins/{plugin_id}/webhook", dependencies=[Depends(require_admin)])
    async def get_plugin_webhook(plugin_id: str):
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        described = routes.describe(plugin_id)
        return {"webhooks": described.get("webhooks", []), "actions": described.get("actions", []),
                "base_url": f"/api/plugin/{plugin_id}/"}

    @app.get("/api/plugins/{plugin_id}/runtime", dependencies=[Depends(require_admin)])
    async def plugin_runtime_status(plugin_id: str):
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        loaded = runtime.loaded.get(plugin_id)
        return {"id": plugin_id, "enabled": meta.enabled, "loaded": bool(loaded), "error": meta.error,
                "handlers": len(loaded.context._handlers) if loaded else 0,
                "background_tasks": len(loaded.context._tasks) if loaded else 0, "events": []}

    @app.post("/api/plugins/{plugin_id}/events/{event_id}/replay", dependencies=[Depends(require_admin)])
    async def replay_plugin_event(plugin_id: str, event_id: str):
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        raise HTTPException(status_code=404, detail="该事件不存在或未声明为可回放")

    @app.get("/api/plugins/{plugin_id}/dialogs", dependencies=[Depends(require_admin)])
    async def list_plugin_dialogs(plugin_id: str, session: str = ""):
        if not any(item.id == plugin_id for item in runtime.scan()):
            raise HTTPException(status_code=404, detail="插件不存在")
        client = accounts.users.get(session) if session else next(iter(accounts.connected_users), None)
        if client is None:
            return {"dialogs": []}
        values = []
        async for dialog in client.iter_dialogs(limit=200):
            values.append({"id": str(dialog.id), "title": dialog.name or str(dialog.id)})
        return {"dialogs": values}

    @app.get("/api/plugins/repo/status", dependencies=[Depends(require_admin)])
    async def plugin_repo_status():
        return {"repos": [{"url": item, "enabled": True} for item in settings.plugin_repos],
                "manifest": "manifest_v2.json"}

    @app.post("/api/plugins/upload", dependencies=[Depends(require_admin)])
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
            target.unlink(missing_ok=True)
            if backup is not None:
                target.write_bytes(backup)
            raise HTTPException(status_code=400, detail=f"插件校验失败：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return {"ok": True, "plugin": meta.to_dict()}

    @app.delete("/api/plugins/{plugin_id}", dependencies=[Depends(require_admin)])
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
        return {"ok": True}

    @app.get("/api/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
    async def plugin_config(plugin_id: str):
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        schema = meta.config_schema or {}
        values = dict(settings.plugin_config.get(plugin_id, {}))
        for key, spec in schema.items():
            if isinstance(spec, dict) and spec.get("secret") and values.get(key):
                values[key] = "********"
        return {
            "values": values,
            "schema": schema,
            "render_mode": meta.render_mode,
            "has_frontend": runtime.has_frontend(plugin_id),
        }

    @app.get("/api/plugins/{plugin_id}/fe/{path:path}")
    async def plugin_frontend_asset(plugin_id: str, path: str, request: Request):
        resource_token = request.cookies.get("awbotnest_resource", "")
        if not resource_token or not hmac.compare_digest(resource_token, settings.admin_token):
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

    @app.put("/api/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
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
            if isinstance(spec, dict) and spec.get("secret") and values.get(key) == "********":
                values[key] = current_values.get(key, "")
        try:
            runtime.validate_config(plugin.config_schema or {}, values)
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
            if isinstance(spec, dict) and spec.get("secret") and safe_values.get(key):
                safe_values[key] = "********"
        return {"ok": True, "values": safe_values}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "mode": "telegram" if settings.telegram_configured else "standalone"}

    @app.get("/api/self-check", dependencies=[Depends(require_admin)])
    async def self_check():
        return runtime.self_check()

    @app.get("/api/avatars/{filename}", dependencies=[Depends(require_admin)])
    async def account_avatar(filename: str):
        if not filename.endswith(".jpg") or "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="头像文件名不合法")
        path = DATA_DIR / "avatars" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="头像不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/scheduler/jobs", dependencies=[Depends(require_admin)])
    async def scheduler_jobs():
        return {"jobs": scheduler.jobs()}

    @app.get("/api/activity", dependencies=[Depends(require_admin)])
    async def plugin_activity(hours: int = 24):
        return activity.timeline(hours)

    @app.get("/api/logs", dependencies=[Depends(require_admin)])
    async def recent_logs(limit: int = 200):
        return {"logs": memory_logs.recent(limit)}

    @app.get("/api/logs/recent", dependencies=[Depends(require_admin)])
    async def recent_logs_compat(limit: int = 200):
        return {"logs": [_compat_log(item) for item in memory_logs.recent(limit)]}

    def _compat_log(item: dict[str, str]) -> dict[str, str]:
        value = dict(item)
        value["msg"] = value.get("message", "")
        try:
            stamp = datetime.fromisoformat(value.get("timestamp", ""))
            value["date"] = stamp.strftime("%Y-%m-%d")
            value["time"] = stamp.strftime("%H:%M:%S")
        except ValueError:
            value.setdefault("date", "")
            value.setdefault("time", "")
        return value

    @app.websocket("/api/logs/ws")
    async def logs_websocket(websocket: WebSocket):
        protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
        auth_protocol = next((item for item in protocols if item.startswith("auth.")), "")
        token = auth_protocol.removeprefix("auth.")
        token_ok = bool(token) and hmac.compare_digest(token, settings.admin_token)
        if not token_ok:
            await websocket.close(code=4401)
            return
        await websocket.accept(subprotocol="awbotnest")
        try:
            initial = [_compat_log(item) for item in memory_logs.recent(1000)]
            await websocket.send_json({"type": "history", "logs": initial})
            seen = {
                (item.get("timestamp"), item.get("level"), item.get("source"), item.get("message"))
                for item in memory_logs.recent(1000)
            }
            while True:
                await asyncio.sleep(0.5)
                current = memory_logs.recent(1000)
                fresh = []
                for item in reversed(current):
                    key = (item.get("timestamp"), item.get("level"), item.get("source"), item.get("message"))
                    if key not in seen:
                        fresh.append(item)
                        seen.add(key)
                for item in fresh:
                    await websocket.send_json(_compat_log(item))
                if len(seen) > 2000:
                    seen = {
                        (item.get("timestamp"), item.get("level"), item.get("source"), item.get("message"))
                        for item in current
                    }
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.post("/api/notifications/test", dependencies=[Depends(require_admin)])
    async def test_notification(body: NotificationTestBody):
        try:
            result = await runtime.notifier.send(body.text, channel=body.channel)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"通知发送失败：{exc}") from exc
        return {"ok": True, "result": str(result)[:1000]}

    @app.get("/api/cookies", dependencies=[Depends(require_admin)])
    async def cookie_domains():
        return {"domains": await runtime.services.cookies.domains()}

    @app.put("/api/cookies/{domain}", dependencies=[Depends(require_admin)])
    async def put_cookies(domain: str, body: CookieBody):
        if not domain.strip() or "/" in domain or "\\" in domain:
            raise HTTPException(status_code=400, detail="Cookie 域名不合法")
        await runtime.services.cookies.set(domain, body.values)
        return {"ok": True, "count": len(body.values)}

    @app.delete("/api/cookies/{domain}", dependencies=[Depends(require_admin)])
    async def delete_cookies(domain: str):
        return {"ok": True, "removed": await runtime.services.cookies.delete(domain)}

    @app.post("/api/backups", dependencies=[Depends(require_admin)])
    async def create_backup():
        archive = BackupManager.create()
        return {"ok": True, "filename": archive.name}

    @app.post("/api/system/backup", dependencies=[Depends(require_admin)])
    async def system_backup():
        archive = BackupManager.create()
        return FileResponse(archive, filename=archive.name, media_type="application/zip")

    @app.get("/api/backups", dependencies=[Depends(require_admin)])
    async def list_backups():
        files = BackupManager.list()
        return {"backups": [{"name": path.name, "size": path.stat().st_size} for path in files]}

    @app.post("/api/backups/restore", dependencies=[Depends(require_admin)])
    async def stage_restore(request: Request):
        try:
            BackupManager.stage(await request.body())
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "restart_required": True}

    @app.post("/api/system/restore", dependencies=[Depends(require_admin)])
    async def system_restore(file: UploadFile = File(...)):
        try:
            BackupManager.stage(await file.read(256 * 1024 * 1024 + 1))
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "restart_required": True}

    @app.get("/api/backups/{filename}", dependencies=[Depends(require_admin)])
    async def download_backup(filename: str):
        if not filename.startswith("AWBotNest-") or not filename.endswith(".zip") or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="备份文件名不合法")
        path = DATA_DIR / "backups" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="备份不存在")
        return FileResponse(path, filename=filename)

    @app.get("/api/system/backups/{filename}", dependencies=[Depends(require_admin)])
    async def system_backup_download(filename: str):
        return await download_backup(filename)

    @app.post("/api/system/clean_logs", dependencies=[Depends(require_admin)])
    async def clean_logs():
        keep = int(settings.log_cleaner.get("keep_lines", 1000))
        removed = memory_logs.trim(keep)
        return {"ok": True, "removed": removed, "kept": min(len(memory_logs.records), keep)}

    @app.get("/api/plugins/store", dependencies=[Depends(require_admin)])
    async def plugin_store():
        return await market.list_all()

    @app.post("/api/plugins/store/install", dependencies=[Depends(require_admin)])
    async def install_market_plugin(body: MarketInstallBody):
        plugin_id = str(body.plugin.get("id") or "")
        was_loaded = plugin_id in runtime.loaded
        try:
            if was_loaded:
                await runtime.disable(plugin_id)
            destination = await market.install(body.plugin)
        except ValueError as exc:
            if was_loaded:
                await runtime.enable(plugin_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            if was_loaded:
                await runtime.enable(plugin_id)
            raise HTTPException(status_code=502, detail=f"插件下载失败：{exc}") from exc
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None or meta.error:
            market.finish(plugin_id, False)
            raise HTTPException(status_code=409, detail=meta.error if meta else "插件安装后未被识别")
        if was_loaded:
            meta = await runtime.enable(plugin_id)
            if meta.error:
                market.finish(plugin_id, False)
                await runtime.enable(plugin_id)
                raise HTTPException(status_code=409, detail=f"更新加载失败，已回滚：{meta.error}")
        market.finish(plugin_id, True)
        market.clear_cache()
        return {"ok": True, "path": str(destination), "plugin": meta.to_dict()}

    @app.post("/api/plugins/github/list", dependencies=[Depends(require_admin)])
    async def github_list(request: Request):
        source = str((await request.json()).get("source") or "")
        try:
            return await market.list_repo(source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取 GitHub 仓库失败：{exc}") from exc

    @app.post("/api/plugins/github/import", dependencies=[Depends(require_admin)])
    async def github_import(request: Request):
        raw = await request.json()
        plugins = raw.get("plugins")
        if not isinstance(plugins, list) or len(plugins) > 100:
            raise HTTPException(status_code=400, detail="插件导入列表格式不正确")
        installed, errors = [], []
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            try:
                await market.install(plugin)
                installed.append(str(plugin.get("id") or ""))
            except Exception as exc:
                errors.append(f"{plugin.get('id') or 'unknown'}: {exc}")
        market.clear_cache()
        return {"result": {"installed": installed, "errors": errors}}

    static_dir = APP_ROOT / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="webui")

    return app
