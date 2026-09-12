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
from ..deps import DependencyManager
from ..cloak_proxy import (
    begin_cloak_update, cancel_cloak_update, cloak_session_status,
)
from ..cloak_updates import cloak_update_status, mark_cloakbrowser_updated
from ..logs import memory_logs
from ..market import normalize_repo
from ..routing import WebhookRequest
from .models import *
from .masking import masked_channels as mask_channels, masked_proxy as mask_proxy
from ..services.ai_protocol import API_FORMATS, normalize_api_format

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

    def masked_proxy():
        return mask_proxy(settings)

    def masked_channels():
        return mask_channels(settings)

    @router.get("/api/settings", dependencies=[Depends(require_admin)])
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
                "GITHUB_TOKEN": "********" if settings.github_token else "",
                "BROWSER_ENGINE": settings.browser_engine,
                "CLOAKBROWSER_USE_FREE_KEY": settings.cloakbrowser_use_free_key,
                "CLOAKBROWSER_LICENSE_KEY": "********" if settings.cloakbrowser_license_key else "",
                "DB_INFO": {"dbset": "SQLite", "db_name": "awbotnest"},
                "LOG_CLEANER": dict(settings.log_cleaner),
                "WEBHOOK_SECRET": "********" if settings.webhook_secret else "",
                "API_KEY": "********" if settings.api_key else "",
                "PLUGIN_REPOS": current["plugin_repos"],
            }
        }

    def current_ai_settings() -> dict[str, object]:
        if settings.ai_settings:
            value = json.loads(json.dumps(settings.ai_settings))
            value.setdefault("timeout_seconds", 60)
            value.setdefault("image_timeout_seconds", 300)
            value.setdefault("max_concurrency", 3)
            for provider in value.get("providers", []):
                if isinstance(provider, dict):
                    provider.setdefault("api_format", "auto")
            return value
        provider_id = "default"
        return {
            "providers": [{"id": provider_id, "name": "OpenAI 兼容服务", "enabled": True,
                           "base_url": settings.ai_base_url, "api_key": "********" if settings.ai_api_key else "",
                           "api_format": "auto"}],
            "models": [{"id": "default", "alias": settings.ai_model, "name": settings.ai_model,
                        "enabled": True, "provider_id": provider_id, "model": settings.ai_model,
                        "capabilities": ["text"]}],
            "capabilities": {"text": {"default_model": settings.ai_model}, "vision": {}, "image": {}},
            "plugin_permissions": {},
            "timeout_seconds": 60,
            "image_timeout_seconds": 300,
            "max_concurrency": 3,
        }

    @router.get("/api/ai/settings", dependencies=[Depends(require_admin)])
    async def get_ai_settings():
        value = current_ai_settings()
        safe = json.loads(json.dumps(value))
        for provider in safe.get("providers", []):
            if provider.get("api_key"):
                provider["api_key"] = "********"
        return {"settings": safe, "status": {
            "configured": bool(settings.ai_api_key),
            "usage": runtime.services.ai.usage_snapshot(),
            "detected_protocols": runtime.services.ai.detected_protocols(),
        }}

    @router.put("/api/ai/settings", dependencies=[Depends(require_admin)])
    async def save_ai_settings(request: Request):
        raw = await request.json()
        value = raw.get("settings")
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="AI 设置格式不正确")
        value.setdefault("timeout_seconds", 60)
        value.setdefault("image_timeout_seconds", 300)
        value.setdefault("max_concurrency", 3)
        providers = value.get("providers", [])
        if not isinstance(providers, list):
            raise HTTPException(status_code=400, detail="AI 服务商列表格式不正确")
        for provider in providers:
            if not isinstance(provider, dict):
                raise HTTPException(status_code=400, detail="AI 服务商配置格式不正确")
            requested_format = str(provider.get("api_format") or "auto").strip().lower().replace("-", "_")
            if requested_format not in API_FORMATS:
                raise HTTPException(status_code=400, detail="AI 接口格式不受支持")
            provider["api_format"] = normalize_api_format(requested_format)
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

    @router.get("/api/ai/plugins", dependencies=[Depends(require_admin)])
    async def list_ai_plugins():
        return {"plugins": [
            {"id": item.id, "name": item.name}
            for item in runtime.scan()
            if not item.error and runtime.uses_platform_ai(item.id)
        ]}

    @router.get("/api/ai/status", dependencies=[Depends(require_admin)])
    async def ai_status():
        return {
            "configured": bool(settings.ai_api_key),
            "base_url": settings.ai_base_url,
            "model": settings.ai_model,
            "usage": runtime.services.ai.usage_snapshot(),
            "detected_protocols": runtime.services.ai.detected_protocols(),
        }

    @router.get("/api/ai/usage/recent", dependencies=[Depends(require_admin)])
    async def ai_usage_recent(limit: int = 50):
        return {"items": runtime.services.ai.usage.recent(limit)}

    @router.get("/api/ai/usage/plugins", dependencies=[Depends(require_admin)])
    async def ai_usage_plugins():
        return {"items": runtime.services.ai.usage.plugin_summary()}

    @router.get("/api/ai/usage/overview", dependencies=[Depends(require_admin)],
                summary="AI 调用明细概览")
    async def ai_usage_overview(limit: int = 20):
        return runtime.services.ai.usage.overview(limit)

    @router.delete("/api/ai/usage/recent", dependencies=[Depends(require_admin)])
    async def clear_ai_usage_recent():
        return {"ok": True, "removed": runtime.services.ai.usage.clear_recent()}

    @router.post("/api/ai/test", dependencies=[Depends(require_admin)])
    async def test_ai_capability(request: Request):
        capability = str((await request.json()).get("capability") or "text")
        try:
            if capability == "text":
                result = await runtime.services.ai.chat([{"role": "user", "content": "Reply with OK only."}])
            elif capability == "vision":
                pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                result = await runtime.services.ai.vision("Describe this image in one short sentence.", pixel)
            elif capability == "image":
                generated = await runtime.services.ai.generate_image("A small blue circle on a white background")
                result = "图片生成成功" if generated.get("url") or generated.get("b64_json") else "图片生成完成"
            else:
                raise HTTPException(status_code=400, detail="不支持的 AI 能力")
            return {"ok": True, "capability": capability, "result": str(result)[:200],
                    "message": f"{capability} 能力测试成功"}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AI 测试失败：{exc}") from exc

    @router.post("/api/ai/provider-models", dependencies=[Depends(require_admin)])
    async def ai_provider_models(request: Request):
        raw = await request.json()
        preview = raw.get("provider")
        saved_providers = [item for item in current_ai_settings().get("providers", [])
                           if isinstance(item, dict)]
        if isinstance(preview, dict):
            provider = dict(preview)
            provider_id = str(provider.get("id") or "")
            saved = next((item for item in saved_providers
                          if str(item.get("id") or "") == provider_id), None)
            if provider.get("api_key") == "********":
                provider["api_key"] = str((saved or {}).get("api_key") or settings.ai_api_key)
        else:
            provider_id = str(preview or "")
            provider = next((item for item in saved_providers
                             if str(item.get("id") or "") == provider_id), None)
        if not provider:
            raise HTTPException(status_code=404, detail="AI 服务不存在")
        api_key = str(provider.get("api_key") or settings.ai_api_key)
        base_url = str(provider.get("base_url") or settings.ai_base_url).strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail="AI 服务地址无效")
        if not api_key or api_key == "********":
            raise HTTPException(status_code=400, detail="请填写 API Key")
        try:
            response = await runtime.services.http.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=20,
            )
            response.raise_for_status()
            values = response.json().get("data", [])
            models = [str(item.get("id")) for item in values if isinstance(item, dict) and item.get("id")]
            return {"models": models, "count": len(models)}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取模型失败：{exc}") from exc

    async def cookie_sync_state() -> dict[str, object]:
        from ..cookiecloud import sync_history
        domains = await runtime.services.cookies.domains()
        history = sync_history()
        latest_success = next((item for item in history if item.get("status") == "success"), None)
        latest_error = next((item for item in history if item.get("status") == "error"), None)
        cookie_count = sum(int(item.get("count") or 0) for item in domains)
        last_sync = ""
        if latest_success and latest_success.get("time"):
            last_sync = datetime.fromtimestamp(float(latest_success["time"])).strftime("%Y-%m-%d %H:%M:%S")
        error_message = ""
        if latest_error and float(latest_error.get("time") or 0) > float((latest_success or {}).get("time") or 0):
            error_message = str(latest_error.get("message") or "")
        return {"has_data": cookie_count > 0, "domain_count": len(domains),
                "cookie_count": cookie_count, "last_sync": last_sync,
                "last_error": error_message}

    @router.get("/api/cookies/settings", dependencies=[Depends(require_admin)])
    async def get_cookie_settings():
        from ..cookiecloud import sync_history
        value = dict(settings.cookie_settings)
        for key in ("uuid", "password", "token", "remote_password"):
            if value.get(key):
                value[key] = "********"
        return {"settings": value, "status": await cookie_sync_state(), "history": sync_history(),
                "server_path": "/cookiecloud"}

    @router.put("/api/cookies/settings", dependencies=[Depends(require_admin)])
    async def save_cookie_settings(request: Request):
        raw = await request.json()
        value = raw.get("settings")
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Cookie 设置格式不正确")
        value = {**settings.cookie_settings, **value}
        try:
            interval = max(5, int(value.get("remote_interval_minutes") or 60))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="同步间隔必须为整数分钟") from exc
        for key in ("uuid", "password", "token", "remote_password"):
            if value.get(key) == "********":
                value[key] = settings.cookie_settings.get(key, "")
        settings.cookie_settings = value
        save_settings(settings)
        job_id = "__platform__::远程 CookieCloud 同步"
        if value.get("remote_enabled"):
            scheduler.add_interval("__platform__", "远程 CookieCloud 同步", sync_remote_cookies,
                                   seconds=interval * 60)
        elif scheduler.scheduler.get_job(job_id):
            scheduler.scheduler.remove_job(job_id)
        return await get_cookie_settings()

    @router.post("/api/cookies/credentials", dependencies=[Depends(require_admin)])
    async def generate_cookie_credentials():
        settings.cookie_settings["uuid"] = secrets.token_urlsafe(12)
        settings.cookie_settings["password"] = secrets.token_urlsafe(24)
        save_settings(settings)
        return {"uuid": settings.cookie_settings["uuid"], "password": settings.cookie_settings["password"]}

    @router.post("/api/cookies/check", dependencies=[Depends(require_admin)])
    async def check_cookie_sync():
        from ..cookiecloud import record_sync, sync_history
        domains = await runtime.services.cookies.domains()
        cookie_count = sum(int(item.get("count") or 0) for item in domains)
        message = f"Cookie 存储正常：{len(domains)} 个域名，{cookie_count} 个 Cookie"
        record_sync("check", "success", message, len(domains), cookie_count)
        return {"ok": True, "message": message, "detail": message,
                "status": await cookie_sync_state(), "history": sync_history()}

    @router.post("/api/cookies/remote-sync", dependencies=[Depends(require_admin)])
    async def sync_remote_cookies():
        from ..cookiecloud import CookieCloudError, pull, record_sync, sync_history, filter_domains
        value = settings.cookie_settings
        if not value.get("remote_enabled"):
            raise HTTPException(status_code=409, detail="远程 CookieCloud 同步尚未启用")
        url = str(value.get("remote_url") or "").strip()
        uuid_value = str(value.get("remote_uuid") or "").strip()
        password = str(value.get("remote_password") or "")
        if not all((url, uuid_value, password)):
            raise HTTPException(status_code=409, detail="请填写远程地址、UUID 和端到端加密密码")
        try:
            values = await pull(url, uuid_value, password,
                                str(value.get("remote_crypto_type") or "auto"),
                                settings.proxy_url or None)
            values = filter_domains(values, value.get("remote_domains"))
            await runtime.services.cookies.replace(values)
            count = sum(len(item) for item in values.values())
            record_sync("remote", "success", "远程 CookieCloud 同步完成", len(values), count)
            return {"ok": True, "message": "远程 CookieCloud 同步完成",
                    "domain_count": len(values),
                    "cookie_count": count, "sync_status": await cookie_sync_state(),
                    "history": sync_history()}
        except CookieCloudError as exc:
            record_sync("remote", "error", str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.delete("/api/cookies/data", dependencies=[Depends(require_admin)])
    async def clear_cookie_data():
        from ..cookiecloud import record_sync, sync_history
        path = DATA_DIR / "cookies.json"
        path.unlink(missing_ok=True)
        (DATA_DIR / "cookiecloud_snapshot.json").unlink(missing_ok=True)
        record_sync("clear", "success", "本地 Cookie 数据已清空")
        return {"ok": True, "sync_status": await cookie_sync_state(), "history": sync_history()}

    def check_cookiecloud_rate(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [stamp for stamp in cookiecloud_rate.get(client, []) if now - stamp < 900]
        if len(recent) >= 240:
            raise HTTPException(status_code=429, detail="Cookie 同步请求过于频繁")
        recent.append(now)
        cookiecloud_rate[client] = recent

    def cookiecloud_headers() -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Content-Encoding, X-CookieCloud-Auth",
            "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Max-Age": "86400",
            "Cache-Control": "no-store",
        }

    @router.options("/cookiecloud/{path:path}")
    async def cookiecloud_options(path: str):
        return Response(status_code=204, headers=cookiecloud_headers())

    @router.get("/cookiecloud", operation_id="cookiecloud_root_get")
    @router.post("/cookiecloud", operation_id="cookiecloud_root_post")
    @router.get("/cookiecloud/", include_in_schema=False)
    @router.post("/cookiecloud/", include_in_schema=False)
    async def cookiecloud_root():
        return JSONResponse({"message": "AWBotNest CookieCloud API", "status": "OK"},
                            headers=cookiecloud_headers())

    @router.get("/cookiecloud/health")
    async def cookiecloud_health(request: Request):
        check_cookiecloud_rate(request)
        enabled = bool(settings.cookie_settings.get("enabled"))
        return JSONResponse({"status": "OK" if enabled else "DISABLED"},
                            status_code=200 if enabled else 503,
                            headers=cookiecloud_headers())

    @router.post("/cookiecloud/update")
    async def cookiecloud_update(request: Request):
        from ..cookiecloud import CookieCloudError, decrypt_payload, normalize_cookie_data, record_sync
        if not settings.cookie_settings.get("enabled"):
            raise HTTPException(status_code=404, detail="Cookie 服务未启用")
        check_cookiecloud_rate(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="CookieCloud 请求格式无效") from exc
        encrypted = str(payload.get("encrypted") or "") if isinstance(payload, dict) else ""
        supplied_uuid = str(payload.get("uuid") or "") if isinstance(payload, dict) else ""
        configured_uuid = str(settings.cookie_settings.get("uuid") or "")
        if not token_matches(supplied_uuid, configured_uuid):
            raise HTTPException(status_code=404, detail="Not Found")
        if not encrypted:
            raise HTTPException(status_code=400, detail="缺少加密 Cookie 数据")
        try:
            decoded = decrypt_payload(
                encrypted,
                str(settings.cookie_settings.get("uuid") or ""),
                str(settings.cookie_settings.get("password") or ""),
                str(payload.get("crypto_type") or payload.get("cryptoType") or
                    settings.cookie_settings.get("crypto_type") or "auto"),
            )
            values = normalize_cookie_data(decoded)
            await runtime.services.cookies.replace(values)
            count = sum(len(item) for item in values.values())
            record_sync("browser", "success", "浏览器 CookieCloud 数据已接收", len(values), count)
            snapshot = DATA_DIR / "cookiecloud_snapshot.json"
            temporary = snapshot.with_suffix(".tmp")
            temporary.write_text(json.dumps({"encrypted": encrypted,
                                             "crypto_type": payload.get("crypto_type") or "auto"},
                                            ensure_ascii=False), encoding="utf-8")
            temporary.replace(snapshot)
            return JSONResponse({"action": "done", "domain_count": len(values),
                                 "cookie_count": count},
                                headers=cookiecloud_headers())
        except CookieCloudError as exc:
            record_sync("browser", "error", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/cookiecloud/get/{uuid_value}", operation_id="cookiecloud_snapshot_get")
    @router.post("/cookiecloud/get/{uuid_value}", operation_id="cookiecloud_snapshot_post")
    async def cookiecloud_get(uuid_value: str, request: Request):
        check_cookiecloud_rate(request)
        if not token_matches(uuid_value, str(settings.cookie_settings.get("uuid") or "")):
            raise HTTPException(status_code=404, detail="Not Found")
        snapshot = DATA_DIR / "cookiecloud_snapshot.json"
        if not snapshot.exists():
            raise HTTPException(status_code=404, detail="Not Found")
        return JSONResponse(json.loads(snapshot.read_text(encoding="utf-8")),
                            headers=cookiecloud_headers())

    @router.put("/api/settings", dependencies=[Depends(require_admin)])
    async def update_settings(request: Request):
        raw = await request.json()
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        if isinstance(raw.get("settings"), dict):
            legacy = raw["settings"]
            proxy = legacy.get("proxy_set") or {}
            legacy_cloak_key = legacy.get(
                "CLOAKBROWSER_LICENSE_KEY",
                "********" if settings.cloakbrowser_license_key else "",
            )
            legacy_cloak_key_enabled = (
                settings.cloakbrowser_use_free_key
                if legacy_cloak_key == "********"
                else bool(str(legacy_cloak_key or "").strip())
            )
            raw = {
                "api_id": legacy.get("API_ID", settings.api_id),
                "api_hash": legacy.get("API_HASH", "********" if settings.api_hash else ""),
                "bot_token": legacy.get("BOT_TOKEN", "********" if settings.bot_token else ""),
                "bot_name": legacy.get("BOT_NAME", settings.bot_name),
                "default_bot_id": legacy.get("DEFAULT_BOT_ID", settings.default_bot_id),
                "default_bot_chat_id": legacy.get("DEFAULT_BOT_CHAT_ID", settings.default_bot_chat_id),
                "web_host": settings.web_host,
                "web_port": legacy.get("WEB_UI_PORT", settings.web_port),
                "bots": legacy.get("BOTS", [asdict(bot) for bot in settings.bots]),
                "ai_base_url": settings.ai_base_url,
                "ai_api_key": "********" if settings.ai_api_key else "",
                "ai_model": settings.ai_model,
                "plugin_repos": legacy.get("PLUGIN_REPOS", settings.plugin_repos),
                "notification_channels": legacy.get("NOTIFICATION_CHANNELS", settings.notification_channels),
                "proxy_url": proxy.get("PROXY_URL", settings.proxy_url) if proxy.get("proxy_enable") else "",
                "webhook_secret": legacy.get("WEBHOOK_SECRET", "********" if settings.webhook_secret else ""),
                "api_key": legacy.get("API_KEY", "********" if settings.api_key else ""),
                "pip_index_url": legacy.get("PIP_INDEX_URL", settings.pip_index_url),
                "github_token": legacy.get("GITHUB_TOKEN", "********" if settings.github_token else ""),
                "browser_engine": legacy.get("BROWSER_ENGINE", settings.browser_engine),
                "cloakbrowser_use_free_key": legacy.get(
                    "CLOAKBROWSER_USE_FREE_KEY", legacy_cloak_key_enabled,
                ),
                "cloakbrowser_license_key": legacy_cloak_key,
                "log_cleaner": legacy.get("LOG_CLEANER", settings.log_cleaner),
            }
        try:
            existing = asdict(settings)
            body = SettingsBody.model_validate({**{key: existing[key] for key in SettingsBody.model_fields
                                                   if key in existing}, **raw})
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
        pip_index_url = body.pip_index_url.strip()
        if pip_index_url:
            parsed_index = urlparse(pip_index_url)
            if parsed_index.scheme not in {"http", "https"} or not parsed_index.hostname:
                raise HTTPException(status_code=400, detail="pip 镜像源必须是完整的 http/https URL")
        cleaner = dict(body.log_cleaner or settings.log_cleaner)
        try:
            normalized_cleaner = {
                "enabled": bool(cleaner.get("enabled", True)),
                "keep_lines": max(1, min(int(cleaner.get("keep_lines", 1000)), 1000)),
                "hour": max(0, min(int(cleaner.get("hour", 3)), 23)),
                "minute": max(0, min(int(cleaner.get("minute", 0)), 59)),
            }
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="日志清理设置格式不正确") from exc
        if body.browser_engine == "cloakbrowser":
            try:
                await DependencyManager(body).ensure(
                    ["cloakbrowser>=0.5.10,<0.6"],
                    plugin_name="CloakBrowser 浏览器引擎",
                    target_only=True,
                )
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"CloakBrowser 安装失败：{exc}") from exc
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
        valid_route_bots = {spec.id for spec in settings.bot_specs() if spec.token} | set(channel_ids)
        settings.bot_routing = {
            plugin_id: ",".join(bot_id for bot_id in str(route).split(",")
                                 if bot_id.strip() in valid_route_bots)
            for plugin_id, route in settings.bot_routing.items()
            if any(bot_id.strip() in valid_route_bots for bot_id in str(route).split(","))
        }
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
        settings.pip_index_url = pip_index_url
        if body.github_token != "********":
            settings.github_token = body.github_token.strip()
        settings.browser_engine = body.browser_engine
        if body.cloakbrowser_license_key != "********":
            settings.cloakbrowser_license_key = body.cloakbrowser_license_key.strip()
        settings.cloakbrowser_use_free_key = bool(
            body.cloakbrowser_use_free_key and settings.cloakbrowser_license_key
        )
        settings.log_cleaner = normalized_cleaner
        save_settings(settings)
        market.clear_cache()
        current = (settings.api_id, settings.api_hash, settings.bot_token, settings.proxy_url,
                   settings.default_bot_id, settings.web_host, settings.web_port,
                   [(item.id, item.token) for item in settings.bots], dict(settings.log_cleaner))
        return {"ok": True, "restart_required": current != previous}

    @router.put("/api/settings/notification-channels", dependencies=[Depends(require_admin)])
    async def save_notification_channels(request: Request):
        raw = await request.json()
        channels = raw.get("channels")
        if not isinstance(channels, list):
            raise HTTPException(status_code=400, detail="通知渠道格式不正确")
        normalized = []
        existing_tokens = {item.id: item.token for item in settings.bot_specs()}
        existing_bot_settings = {item.id: item for item in settings.bots}
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
                    if key == "token" and item.get("type") == "telegram":
                        # Telegram Token 存在 Bot 配置中，渠道中只有掩码。
                        item[key] = existing_tokens.get(str(item.get("id") or ""), "") or previous.get(key, previous_nested.get(key, ""))
                    else:
                        item[key] = previous.get(key, previous_nested.get(key, ""))
            if item.get("type") == "wechat":
                item["type"] = "wecom"
            if item.get("type") == "telegram":
                channel_id = str(item.get("id") or "")
                token = str(item.get("token", existing_tokens.get(channel_id, "")) or "")
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
        # 通知渠道页只编辑渠道，不能因为某个 Bot 没有绑定通知渠道就把它的
        # Token 从系统配置中删除。保留未出现在本次渠道提交中的独立 Bot。
        channel_bot_ids = {bot.id for bot in new_bots}
        settings.bots = new_bots + [bot for bot_id, bot in existing_bot_settings.items()
                                    if bot_id not in channel_bot_ids]
        valid_route_bots = {spec.id for spec in settings.bot_specs() if spec.token} | {
            str(item.get("id") or "") for item in normalized}
        settings.bot_routing = {
            plugin_id: ",".join(bot_id for bot_id in str(route).split(",")
                                 if bot_id.strip() in valid_route_bots)
            for plugin_id, route in settings.bot_routing.items()
            if any(bot_id.strip() in valid_route_bots for bot_id in str(route).split(","))
        }
        save_settings(settings)
        return {"ok": True, "channels": masked_channels(), "restart_required": True}

    @router.post("/api/settings/reveal-secret", dependencies=[Depends(require_admin)])
    async def reveal_secret(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        raw = await request.json()
        kind, field, item_id = str(raw.get("kind") or ""), str(raw.get("field") or ""), str(raw.get("id") or "")
        value = ""
        if kind == "system":
            value = {"API_HASH": settings.api_hash, "BOT_TOKEN": settings.bot_token,
                     "GITHUB_TOKEN": settings.github_token,
                     "CLOAKBROWSER_LICENSE_KEY": settings.cloakbrowser_license_key,
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
            if channel and channel.get("type") == "telegram" and field == "token":
                value = next((bot.token for bot in settings.bot_specs() if bot.id == item_id), "") or value
        if not value:
            raise HTTPException(status_code=404, detail="密钥不存在")
        return {"value": value}

    @router.get("/api/browser/status", dependencies=[Depends(require_admin)],
                summary="浏览器仿真状态")
    async def browser_status():
        manager = DependencyManager(settings)
        key_active = bool(
            settings.cloakbrowser_use_free_key and settings.cloakbrowser_license_key
        )
        return {
            "engine": settings.browser_engine,
            "cloakbrowser_installed": bool(manager.target_version("cloakbrowser")),
            "cloakbrowser_version": manager.target_version("cloakbrowser"),
            "key_configured": bool(settings.cloakbrowser_license_key),
            "key_enabled": settings.cloakbrowser_use_free_key,
            "key_active": key_active,
            "binary_mode": "latest" if key_active else "legacy_free",
            "update_check": cloak_update_status(settings),
            **cloak_session_status(),
        }

    @router.post("/api/browser/cloakbrowser/update", dependencies=[Depends(require_admin)],
                 summary="更新 CloakBrowser")
    async def update_cloakbrowser():
        if settings.browser_engine != "cloakbrowser":
            raise HTTPException(status_code=400, detail="请先选择并保存 CloakBrowser")
        try:
            begin_cloak_update()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        manager = DependencyManager(settings)
        try:
            await manager.ensure(
                ["cloakbrowser>=0.5.10,<0.6"],
                plugin_name="CloakBrowser 浏览器引擎",
                target_only=True,
                upgrade=True,
            )
            version = manager.target_version("cloakbrowser")
            mark_cloakbrowser_updated(settings, version)
        except Exception as exc:
            cancel_cloak_update()
            raise HTTPException(status_code=502, detail=f"CloakBrowser 更新失败：{exc}") from exc
        will_restart = restart_event is not None
        key_active = bool(
            settings.cloakbrowser_use_free_key and settings.cloakbrowser_license_key
        )
        if will_restart:
            asyncio.get_running_loop().call_later(0.8, restart_event.set)
        else:
            # 嵌入式调用和 API 单元测试可以不提供平台重启事件。
            # 依赖已经更新完成，此时由宿主在方便时自行重启即可。
            cancel_cloak_update()
        return {
            "ok": True,
            "version": version,
            "restarting": will_restart,
            "message": (
                "CloakBrowser 组件已更新，平台正在重启；"
                if will_restart else "CloakBrowser 组件已更新；重启宿主后生效，"
            ) + (
                "最新版内核将在下次调用时自动检查。"
                if key_active else "当前未启用免费 Key，后续调用仍使用旧版免费内核。"
            ),
        }

    @router.post("/api/settings/test_proxy", dependencies=[Depends(require_admin)])
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

    @router.post("/api/settings/test_db", dependencies=[Depends(require_admin)])
    async def test_db():
        return {"ok": True, "detail": "2.0 使用平台内置 SQLite 存储"}
    return router
