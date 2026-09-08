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

def create_router(deps, list_plugins) -> APIRouter:
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

    @router.get("/api/health")
    async def health():
        return {"ok": True, "mode": "telegram" if settings.telegram_configured else "standalone"}

    @router.get("/api/self-check", dependencies=[Depends(require_admin)])
    async def self_check():
        return runtime.self_check()

    @router.get("/api/avatars/{filename}", dependencies=[Depends(require_admin)])
    async def account_avatar(filename: str):
        if not filename.endswith(".jpg") or "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="头像文件名不合法")
        path = DATA_DIR / "avatars" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="头像不存在")
        return FileResponse(path, media_type="image/jpeg")

    @router.get("/api/scheduler/jobs", dependencies=[Depends(require_admin)])
    async def scheduler_jobs():
        return {"jobs": scheduler.jobs()}

    @router.get("/api/activity", dependencies=[Depends(require_admin)])
    async def plugin_activity(hours: int = 24):
        return activity.timeline(hours)

    @router.get("/api/logs", dependencies=[Depends(require_admin)])
    async def recent_logs(limit: int = 200):
        return {"logs": memory_logs.recent(limit)}

    @router.get("/api/logs/recent", dependencies=[Depends(require_admin)])
    async def recent_logs_compat(limit: int = 200):
        return {"logs": [_compat_log(item) for item in memory_logs.recent(limit)]}

    def _compat_log(item: dict[str, str]) -> dict[str, str]:
        value = dict(item)
        value["msg"] = value.get("message", "")
        try:
            stamp = datetime.fromisoformat(value.get("timestamp", "")).astimezone()
            value["date"] = stamp.strftime("%Y-%m-%d")
            value["time"] = stamp.strftime("%H:%M:%S")
        except ValueError:
            value.setdefault("date", "")
            value.setdefault("time", "")
        return value

    @router.websocket("/api/logs/ws")
    async def logs_websocket(websocket: WebSocket):
        protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
        auth_protocol = next((item for item in protocols if item.startswith("auth.")), "")
        token = auth_protocol.removeprefix("auth.")
        token_ok = token_matches(token, settings.admin_token)
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

    @router.post("/api/notifications/test", dependencies=[Depends(require_admin)])
    async def test_notification(body: NotificationTestBody):
        try:
            result = await runtime.notifier.send(body.text, channel=body.channel)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"通知发送失败：{exc}") from exc
        return {"ok": True, "result": str(result)[:1000]}

    @router.get("/api/cookies", dependencies=[Depends(require_admin)])
    async def cookie_domains():
        return {"domains": await runtime.services.cookies.domains()}

    @router.put("/api/cookies/{domain}", dependencies=[Depends(require_admin)])
    async def put_cookies(domain: str, body: CookieBody):
        if not domain.strip() or "/" in domain or "\\" in domain:
            raise HTTPException(status_code=400, detail="Cookie 域名不合法")
        await runtime.services.cookies.set(domain, body.values)
        return {"ok": True, "count": len(body.values)}

    @router.delete("/api/cookies/{domain}", dependencies=[Depends(require_admin)])
    async def delete_cookies(domain: str):
        return {"ok": True, "removed": await runtime.services.cookies.delete(domain)}

    @router.post("/api/backups", dependencies=[Depends(require_admin)])
    async def create_backup():
        logger.info("备份导出开始")
        try:
            archive = await asyncio.wait_for(asyncio.to_thread(BackupManager.create), timeout=120)
        except Exception as exc:
            logger.exception("备份导出失败：%s", exc)
            raise HTTPException(status_code=500, detail=f"备份导出失败：{exc}") from exc
        logger.info("备份导出完成：%s", archive.name)
        return {"ok": True, "filename": archive.name}

    @router.post("/api/system/backup", dependencies=[Depends(require_admin)])
    async def system_backup():
        logger.info("备份下载开始")
        try:
            archive = await asyncio.wait_for(asyncio.to_thread(BackupManager.create), timeout=120)
        except Exception as exc:
            logger.exception("备份下载失败：%s", exc)
            raise HTTPException(status_code=500, detail=f"备份生成失败：{exc}") from exc
        logger.info("备份下载完成：%s", archive.name)
        return FileResponse(archive, filename=archive.name, media_type="application/zip")

    @router.get("/api/backups", dependencies=[Depends(require_admin)])
    async def list_backups():
        files = BackupManager.list()
        return {"backups": [{"name": path.name, "size": path.stat().st_size} for path in files]}

    @router.post("/api/backups/restore", dependencies=[Depends(require_admin)])
    async def stage_restore(request: Request):
        try:
            BackupManager.stage(await request.body())
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "restart_required": True}

    @router.post("/api/system/restore", dependencies=[Depends(require_admin)])
    async def system_restore(file: UploadFile = File(...)):
        try:
            BackupManager.stage(await file.read(MAX_BACKUP_SIZE + 1))
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "restart_required": True}

    @router.get("/api/backups/{filename}", dependencies=[Depends(require_admin)])
    async def download_backup(filename: str):
        if not filename.startswith("AWBotNest-") or not filename.endswith(".zip") or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="备份文件名不合法")
        path = DATA_DIR / "backups" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="备份不存在")
        return FileResponse(path, filename=filename)

    @router.get("/api/system/backups/{filename}", dependencies=[Depends(require_admin)])
    async def system_backup_download(filename: str):
        return await download_backup(filename)

    @router.post("/api/system/clean_logs", dependencies=[Depends(require_admin)])
    async def clean_logs():
        keep = int(settings.log_cleaner.get("keep_lines", 1000))
        removed = memory_logs.trim(keep)
        return {"ok": True, "removed": removed, "kept": min(len(memory_logs.records), keep)}

    @router.get("/api/plugins/store", dependencies=[Depends(require_admin)])
    async def plugin_store(refresh: bool = False):
        # 普通页面加载不触发联网；仅显式刷新和后台任务更新缓存。
        return await market.refresh() if refresh else market.cached()

    @router.post("/api/plugins/store/install", dependencies=[Depends(require_admin)])
    async def install_market_plugin(body: MarketInstallBody):
        async with market.install_lock:
            return await _install_market_plugin(body)

    async def _install_market_plugin(body: MarketInstallBody):
        plugin_id = str(body.plugin.get("id") or "")
        was_loaded = plugin_id in runtime.loaded
        action = "更新" if runtime.entry_file(plugin_id) is not None else "安装"
        plugin_name = str(body.plugin.get("name") or runtime.display_name(plugin_id))
        try:
            if was_loaded:
                await runtime.disable(plugin_id, persist=False)
            destination = await market.install(body.plugin)
            runtime.invalidate_scan_cache()
        except ValueError as exc:
            logger.error("%s失败：%s（%s）", action, plugin_name, exc)
            if was_loaded:
                await runtime.enable(plugin_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("%s失败：%s（下载失败：%s）", action, plugin_name, exc)
            if was_loaded:
                await runtime.enable(plugin_id)
            raise HTTPException(status_code=502, detail=f"插件下载失败：{exc}") from exc
        meta = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if meta is None or meta.error:
            error_detail = meta.error if meta else "插件安装后未被识别"
            logger.error("%s失败：%s（安装后校验失败：%s）", action, plugin_name, error_detail)
            market.finish(plugin_id, False)
            runtime.invalidate_scan_cache()
            if was_loaded:
                await runtime.enable(plugin_id)
            raise HTTPException(status_code=409, detail=error_detail)
        if was_loaded:
            meta = await runtime.enable(plugin_id)
            if meta.error:
                market.finish(plugin_id, False)
                runtime.invalidate_scan_cache()
                await runtime.enable(plugin_id)
                logger.error("%s失败：%s（重新加载失败：%s，已回滚）", action, plugin_name, meta.error)
                raise HTTPException(status_code=409, detail=f"更新加载失败，已回滚：{meta.error}")
        market.finish(plugin_id, True)
        await market.record_install(body.plugin, "update" if was_loaded else "install")
        market.clear_cache()
        logger.info("已%s：%s", action, meta.name)
        return {"ok": True, "path": str(destination), "plugin": meta.to_dict()}

    @router.post("/api/plugins/store/download", dependencies=[Depends(require_admin)])
    async def download_market_plugins_legacy(request: Request):
        """V1 批量商店下载接口兼容层。"""
        payload = await request.json()
        plugins = payload.get("plugins") if isinstance(payload, dict) else []
        if not isinstance(plugins, list) or len(plugins) > 100:
            raise HTTPException(status_code=400, detail="plugins 必须是数组且不超过 100 个")
        installed, errors = [], []
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            try:
                result = await install_market_plugin(MarketInstallBody(plugin=plugin))
                installed.append(str(plugin.get("id") or ""))
            except HTTPException as exc:
                errors.append(f"{plugin.get('id', '')}: {exc.detail}")
        return {"result": {"installed": installed, "errors": errors}}

    @router.post("/api/plugins/github/list", dependencies=[Depends(require_admin)])
    async def github_list(request: Request):
        source = str((await request.json()).get("source") or "")
        try:
            return await market.list_repo(source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取 GitHub 仓库失败：{exc}") from exc

    @router.post("/api/plugins/github/import", dependencies=[Depends(require_admin)])
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
                await install_market_plugin(MarketInstallBody(plugin=plugin))
                installed.append(str(plugin.get("id") or ""))
            except Exception as exc:
                errors.append(f"{plugin.get('id') or 'unknown'}: {exc}")
        market.clear_cache()
        return {"result": {"installed": installed, "errors": errors}}
    return router
