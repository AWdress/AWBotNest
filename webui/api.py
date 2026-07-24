"""
webui/api.py
平台 Web API：插件管理 + 账号 + 设置 + 日志。

安全说明：
- 上传 .py 等同于在服务器执行任意代码，相关接口必须经过鉴权依赖（require_auth）。
- 鉴权采用密码登录 + Bearer 令牌（见 webui/auth.py）。
- 本地开发可设环境变量 AWBOTNEST_DEV_NO_AUTH=true 放开。
"""
from __future__ import annotations

import hmac
import os
import sqlite3
import tempfile
import copy
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from core import config, logger
from webui import auth as _authmod
from webui.auth import require_auth as _auth
from webui.auth import require_password_changed as _auth_pwc
from webui.auth import require_api_key as _api_key
from webui.backup import (
    BackupError,
    MAX_ARCHIVE_BYTES,
    create_backup_archive,
    prune_stored_backups,
    stage_restore_archive,
    stored_backup_path,
)
from kernel.registry import registry

app = FastAPI(title="AWBotNest Platform API")

# 从 VERSION 文件读取版本号（单一真值源）
_VERSION_FILE = Path(__file__).parent.parent / "VERSION"
APP_VERSION = _VERSION_FILE.read_text(encoding="utf-8").strip() if _VERSION_FILE.exists() else "unknown"

# 前端构建产物目录（Vue 构建后输出到 webui/static）
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

PLUGINS_DIR = Path("plugins")


# ──────────────────────────────────────────────
# 鉴权：密码登录 + 令牌
# ──────────────────────────────────────────────
@app.get("/api/auth/status")
async def auth_status():
    """前端启动时调用：是否免鉴权"""
    return {"dev_no_auth": _authmod.DEV_NO_AUTH, "username": _authmod.get_username(),
            "version": APP_VERSION, "must_change_password": _authmod.is_default_password()}


@app.post("/api/auth/login")
async def auth_login(body: Dict[str, Any], response: Response):
    """登录（用户名 + 密码），返回令牌；同时种下资源 Cookie（供插件静态资源鉴权）"""
    b = body or {}
    token = _authmod.login(b.get("username", ""), b.get("password", ""))
    _authmod.set_resource_cookie(response)
    return {"status": "success", "token": token}


@app.post("/api/auth/resource_token")
async def issue_resource_token(response: Response, user=Depends(_auth)):
    """（重新）种下资源 Cookie。前端在恢复登录态（localStorage 令牌）后调用，
    确保未走登录接口的会话也有资源 Cookie，能加载 vue 模式插件的前端产物。"""
    _authmod.set_resource_cookie(response)
    return {"ok": True}


@app.post("/api/auth/change_credentials")
async def auth_change_credentials(body: Dict[str, Any], user=Depends(_auth)):
    """修改登录用户名/密码（需已登录 + 当前密码）"""
    b = body or {}
    _authmod.change_credentials(
        b.get("old_password", ""), b.get("new_username", ""), b.get("new_password", ""),
    )
    logger.info("Web 控制台登录凭据已修改")
    return {"status": "success", "username": _authmod.get_username()}


def _get_runtime():
    """获取运行时单例（从内核共享模块读取，避免 __main__ 重复加载问题）"""
    from kernel import state as kernel_state
    if kernel_state.runtime is None:
        raise HTTPException(status_code=503, detail="插件运行时尚未就绪")
    return kernel_state.runtime


def _get_accounts():
    """获取账号管理器单例"""
    from kernel import state as kernel_state
    if kernel_state.accounts is None:
        raise HTTPException(status_code=503, detail="账号管理器尚未就绪")
    return kernel_state.accounts


# ──────────────────────────────────────────────
# 静态前端
# ──────────────────────────────────────────────
@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        # index.html 是固定文件名的应用入口，浏览器缓存后会一直引用旧 hash 的 JS/CSS，
        # 导致新构建加载不进来（前端改动"看不到效果"）。入口禁缓存、每次校验，
        # 带 hash 的 assets 仍长缓存不变（见 plugin_static 的缓存策略）。
        return FileResponse(str(idx), headers={"Cache-Control": "no-cache"})
    return {"message": "AWBotNest 平台运行中。前端尚未构建，请在 webui/frontend 执行 npm run build。"}


@app.get("/favicon.ico")
async def favicon():
    """网站图标（构建时由 logo 生成，输出到 static 根）。"""
    ico = STATIC_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico))
    raise HTTPException(status_code=404, detail="favicon 不存在")


# 根级静态资源（PWA 相关：图标 / manifest / service worker）
_ROOT_STATIC = {
    "apple-touch-icon.png", "manifest.webmanifest", "sw.js",
    "pwa-192.png", "pwa-512.png", "favicon-16.png", "favicon-32.png",
}


@app.get("/{filename}")
async def root_static(filename: str):
    """服务 static 根目录下的 PWA 资源；其它路径交给前端路由（hash 模式不会到这）。"""
    if filename not in _ROOT_STATIC:
        raise HTTPException(status_code=404, detail="not found")
    f = STATIC_DIR / filename
    if f.exists():
        return FileResponse(str(f))
    raise HTTPException(status_code=404, detail="not found")


# ──────────────────────────────────────────────
# 插件管理 API
# ──────────────────────────────────────────────
@app.get("/api/plugins")
async def list_plugins(user=Depends(_auth)):
    """列出所有插件及其状态"""
    runtime = _get_runtime()
    metas = registry.scan()
    out = []
    for m in metas:
        m.loaded = runtime.is_loaded(m.id)
        d = m.to_dict()
        # vue 模式：附带前端构建产物是否就绪，供前端决定「加载组件」还是「提示未构建」
        d["has_frontend"] = registry.has_frontend(m.id) if m.render_mode == "vue" else False
        out.append(d)
    return {"plugins": out}


@app.post("/api/plugins/upload")
async def upload_plugin(file: UploadFile = File(...), user=Depends(_auth_pwc)):
    """
    上传插件 .py 文件。仅落盘 + 静态校验元数据，不自动启用。
    """
    filename = file.filename or ""
    if not filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="只接受 .py 文件")
    if filename.startswith("_") or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="非法文件名")

    content = await file.read()
    dest = PLUGINS_DIR / filename
    dest.write_bytes(content)

    # 静态校验元数据
    meta = registry.parse_meta(dest)
    if meta.error:
        # 保留文件但前端会标红显示错误
        logger.warning("上传的插件元数据有问题 [%s]: %s", meta.id, meta.error)
    logger.info("插件已上传: %s", meta.name)
    return {"status": "success", "plugin": meta.to_dict()}


# ── GitHub 仓库导入 ──
def _safe_target(target: str) -> str:
    """校验下载目标相对路径安全性（防路径穿越）"""
    t = target.replace("\\", "/")
    import os as _os
    if (t.startswith("/") or ".." in t.split("/") or t.startswith("_")
            or _os.path.splitdrive(t)[0] or _os.path.isabs(t)):
        raise HTTPException(status_code=400, detail=f"非法目标路径: {target}")
    return t


@app.post("/api/plugins/github/list")
async def github_list(body: Dict[str, Any], user=Depends(_auth)):
    """
    列出公开 GitHub 来源中的插件。body: {source}
    返回 {source_type, plugins:[{id,name,version,author,description,icon,is_folder,...}]}
    优先读仓库 manifest.json（插件市场），无清单则目录扫描。
    """
    from webui import github_import
    source = (body.get("source") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="请填写 GitHub 仓库地址或链接")
    try:
        result = await github_import.list_plugins(source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"访问 GitHub 失败: {e}")
    if not result.get("plugins"):
        raise HTTPException(status_code=404, detail="该来源未找到插件（应有 manifest.json，或仓库根/plugins/ 下有 .py 或 <id>/__init__.py）")
    return result


@app.post("/api/plugins/github/import")
async def github_import_files(body: Dict[str, Any], user=Depends(_auth_pwc)):
    """
    从公开仓库下载并保存选定插件。body: {plugins: [<list返回的plugin对象>]}
    单文件与文件夹插件都支持；仅落盘 + 静态校验，不自动启用。
    """
    from webui import github_import
    plugins = body.get("plugins") or []
    if not isinstance(plugins, list) or not plugins:
        raise HTTPException(status_code=400, detail="未选择任何插件")

    imported = []
    for plugin in plugins:
        pid = plugin.get("id")
        if not pid or "/" in pid or "\\" in pid or pid.startswith("_"):
            raise HTTPException(status_code=400, detail=f"非法插件 id: {pid}")
        try:
            files = await github_import.resolve_files(plugin)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"解析 {pid} 文件失败: {e}")
        if not files:
            raise HTTPException(status_code=502, detail=f"插件 {pid} 没有可下载文件")

        for f in files:
            target = _safe_target(f["target"])
            try:
                content = await github_import.fetch_file(f["download_url"])
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"下载 {target} 失败: {e}")
            dest = PLUGINS_DIR / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

        meta = registry.get_meta(pid)
        if meta and meta.error:
            logger.warning("GitHub 导入的插件元数据有问题 [%s]: %s", pid, meta.error)
        imported.append(meta.to_dict() if meta else {"id": pid, "error": "导入后未找到入口"})
        logger.info("已从 GitHub 导入插件: %s（%d 个文件）", meta.name if meta else pid, len(files))
    return {"status": "success", "imported": imported}


@app.post("/api/plugins/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, user=Depends(_auth_pwc)):
    """启用插件（热加载）"""
    runtime = _get_runtime()
    meta = await runtime.enable(plugin_id)
    if meta.error:
        raise HTTPException(status_code=400, detail=meta.error)
    return {"status": "success", "plugin": meta.to_dict()}


@app.post("/api/plugins/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, user=Depends(_auth)):
    """停用插件（热卸载）"""
    runtime = _get_runtime()
    meta = await runtime.disable(plugin_id)
    return {"status": "success", "plugin": meta.to_dict()}


@app.post("/api/plugins/{plugin_id}/reload")
async def reload_plugin(plugin_id: str, user=Depends(_auth_pwc)):
    """重载插件（改文件后刷新）"""
    runtime = _get_runtime()
    meta = await runtime.reload(plugin_id)
    if meta.error:
        raise HTTPException(status_code=400, detail=meta.error)
    return {"status": "success", "plugin": meta.to_dict()}


@app.delete("/api/plugins/{plugin_id}")
async def delete_plugin(plugin_id: str, user=Depends(_auth)):
    """删除插件：先停用，再删文件/目录与状态记录（支持单文件与文件夹两种形态）"""
    import shutil
    runtime = _get_runtime()
    # 删文件后 get_meta 取不到，先把中文名留下来供日志用
    _disp = registry.display_name(plugin_id)
    await runtime.disable(plugin_id)
    # 文件夹插件删整个目录，单文件插件删 .py
    if registry.is_package_plugin(plugin_id):
        pkg_dir = PLUGINS_DIR / plugin_id
        if pkg_dir.is_dir():
            shutil.rmtree(pkg_dir, ignore_errors=True)
    else:
        f = PLUGINS_DIR / f"{plugin_id}.py"
        if f.exists():
            f.unlink()
    registry.remove(plugin_id)
    logger.info("插件已删除: %s", _disp)
    return {"status": "success"}


@app.get("/api/plugins/{plugin_id}/config")
async def get_plugin_config(plugin_id: str, user=Depends(_auth)):
    """读取插件配置（schema + 当前值）"""
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    return {
        "schema": meta.config_schema,
        "values": registry.get_config(plugin_id),
        "render_mode": meta.render_mode,
        "has_frontend": registry.has_frontend(plugin_id) if meta.render_mode == "vue" else False,
    }


@app.put("/api/plugins/{plugin_id}/config")
async def set_plugin_config(plugin_id: str, values: Dict[str, Any], user=Depends(_auth_pwc)):
    """保存插件配置；若插件已加载则重载以生效"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    registry.set_config(plugin_id, values)
    if runtime.is_loaded(plugin_id):
        await runtime.reload(plugin_id)
    return {"status": "success", "values": registry.get_config(plugin_id)}


# ── vue 模式：插件自带前端（联邦组件）静态资源 + 插件 API 分发 ──
@app.get("/api/plugins/{plugin_id}/fe/{path:path}")
async def plugin_frontend_asset(
    plugin_id: str, path: str,
    _: None = Depends(_authmod.require_resource_access),
):
    """
    托管 vue 模式插件的前端构建产物（plugins/<id>/frontend/dist/ 下）。
    浏览器 ESM 动态 import 无法附带 Authorization 头，故用登录时下发的资源 Cookie
    鉴权（同源请求自动带上）——未登录会话拿不到插件前端产物。含路径穿越防护。
    业务数据仍走下方需 Bearer 令牌的 /api 分发。
    """
    import os, mimetypes
    dist = registry.frontend_dist_dir(plugin_id).resolve()

    def _resolve(rel: str):
        """在 dist 下解析 rel，带路径穿越防护；不是 dist 内的文件返回 None。"""
        t = (dist / rel).resolve()
        if t != dist and not str(t).startswith(str(dist) + os.sep):
            raise HTTPException(status_code=400, detail="非法资源路径")
        return t if t.is_file() else None

    # 先按原样在 dist 下找；找不到再回退到 dist/assets/ 下。
    # 兼容两种联邦产物布局：remoteEntry 在 dist 根，或在 dist/assets/ 内。
    # 关键是宿主用「根 URL」加载 remoteEntry，使其内部 ./assets/xxx 相对引用正确解析。
    target = _resolve(path) or _resolve(f"assets/{path}")
    if target is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    # 强制修正 JS/CSS 的 MIME：ESM 动态 import 对 .mjs/.js 的类型敏感，
    # 若被 guess 成 octet-stream 浏览器会拒绝执行模块。
    suffix = target.suffix.lower()
    media = {".js": "application/javascript", ".mjs": "application/javascript",
             ".css": "text/css"}.get(suffix)
    if media is None:
        media = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    resp = FileResponse(str(target), media_type=media)
    # 按文件区分缓存策略：
    # - remoteEntry.js 是固定文件名的联邦入口，浏览器会缓存它；但每次插件更新，
    #   它引用的 hash chunk 名会变、旧 chunk 被删。若缓存旧入口，就会去请求已删除的
    #   hash chunk → 404。故入口文件禁缓存，每次校验新鲜度。
    # - 带内容 hash 的 chunk：内容变则名字变，可安全长缓存。
    if target.name == "remoteEntry.js":
        resp.headers["Cache-Control"] = "no-cache"
    else:
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.api_route(
    "/api/plugins/{plugin_id}/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def plugin_api_call(plugin_id: str, path: str, request: Request, user=Depends(_auth)):
    """
    分发到插件 ctx.on_api 注册的处理器。供 vue 模式自带前端组件调用。
    经管理员登录态鉴权（Bearer 令牌），外部无法直接访问。
    """
    import json as _json
    from kernel.context import WebhookRequest

    runtime = _get_runtime()
    handler = runtime.get_api_handler(plugin_id, request.method, path)
    if handler is None:
        raise HTTPException(status_code=404, detail="插件未启用或未注册该 API")

    query = dict(request.query_params)
    headers = {k.lower(): v for k, v in request.headers.items()}
    body = await request.body()
    json_data = None
    if body:
        try:
            json_data = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            json_data = None
    req = WebhookRequest(request.method, query, headers, body, json_data,
                         path="/" + path.strip("/"))
    try:
        result = await handler(req)
    except Exception as e:  # noqa: BLE001 - 插件处理器异常不外泄堆栈
        logger.exception("插件 API 处理失败: %s /%s", plugin_id, path)
        raise HTTPException(status_code=500, detail="插件 API 处理失败") from e
    return _webhook_result_response(result)


@app.get("/api/plugins/{plugin_id}/accounts")
async def get_plugin_accounts(plugin_id: str, user=Depends(_auth)):
    """读取插件的「应用账号范围」：可选账号列表 + 当前已选（空=全部用户账号）"""
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    accounts = _get_accounts()
    options = [{"session": a["session"], "name": a.get("name") or a["session"]}
               for a in await accounts.list_accounts()]
    return {"accounts": options, "selected": registry.get_account_scope(plugin_id), "scope": meta.scope}


@app.put("/api/plugins/{plugin_id}/accounts")
async def set_plugin_accounts(plugin_id: str, body: Dict[str, Any], user=Depends(_auth_pwc)):
    """设置插件应用到哪些账号（空数组=全部用户账号）；已加载则重载重挂 handler"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    sessions = body.get("sessions") or []
    if not isinstance(sessions, list):
        raise HTTPException(status_code=400, detail="sessions 必须是数组")
    # 只接受真实存在的用户账号 session，剔除不存在的，避免 scope 指向死 session 静默失效
    accounts = _get_accounts()
    valid = {a["session"] for a in await accounts.list_accounts()}
    cleaned = [str(x) for x in sessions if str(x) in valid]
    registry.set_account_scope(plugin_id, cleaned)
    runtime = _get_runtime()
    if runtime.is_loaded(plugin_id):
        await runtime.reload(plugin_id)
    return {"status": "success", "selected": registry.get_account_scope(plugin_id)}


# ──────────────────────────────────────────────
# 会话列表（供 config_schema 的 chat 选择器：让管理员从下拉里挑群/频道，不用手填 ID）
# ──────────────────────────────────────────────
def _chat_type_of(chat) -> str:
    """把 Pyrogram ChatType 归一成简单字符串：private / bot / group / channel。"""
    t = str(getattr(chat, "type", "") or "").lower()
    if "bot" in t:
        return "bot"
    if "channel" in t:
        return "channel"
    if "group" in t:  # group / supergroup 统一为 group
        return "group"
    return "private"


def _chat_title_of(chat) -> str:
    title = getattr(chat, "title", None)
    if title:
        return str(title)
    first = getattr(chat, "first_name", "") or ""
    last = getattr(chat, "last_name", "") or ""
    name = (first + " " + last).strip()
    if name:
        return name
    uname = getattr(chat, "username", None)
    return f"@{uname}" if uname else str(getattr(chat, "id", ""))


@app.get("/api/plugins/{plugin_id}/dialogs")
async def list_plugin_dialogs(plugin_id: str, session: str = "", user=Depends(_auth)):
    """列出某用户账号的会话（群/频道/私聊），供 chat 选择器用。
    session 指定用哪个账号枚举；不传则取本插件应用范围内首个已连接用户账号。最多返回 300 条。"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    accounts = _get_accounts()
    apps = accounts.connected_user_apps
    if session:
        app_client = next((a for a in apps if getattr(a, "name", None) == session), None)
    else:
        scope = registry.get_account_scope(plugin_id)
        scoped = [a for a in apps if getattr(a, "name", None) in scope] if scope else list(apps)
        app_client = scoped[0] if scoped else None
    if app_client is None:
        raise HTTPException(status_code=409, detail="没有可用的已连接用户账号，请先上线账号或手填会话 ID")
    try:
        chats = []
        async for d in app_client.get_dialogs(limit=300):
            chat = getattr(d, "chat", None)
            if chat is None:
                continue
            chats.append({
                "id": chat.id,
                "title": _chat_title_of(chat),
                "type": _chat_type_of(chat),
            })
    except Exception as e:  # noqa: BLE001
        logger.exception("列出会话失败: %s", plugin_id)
        raise HTTPException(status_code=502, detail=f"拉取会话失败：{e}") from e
    return {"chats": chats}


@app.get("/api/chats/{chat_id}")
async def get_chat_info(chat_id: str, session: str = "", user=Depends(_auth)):
    """通过 chat_id 获取群组/频道/私聊的信息（名称、类型）。
    chat_id 可以是数字 ID 或 @username。
    session 指定用哪个账号查询；不传则使用首个已连接用户账号。"""
    accounts = _get_accounts()
    apps = accounts.connected_user_apps
    if session:
        app_client = next((a for a in apps if getattr(a, "name", None) == session), None)
    else:
        app_client = apps[0] if apps else None
    if app_client is None:
        raise HTTPException(status_code=409, detail="没有可用的已连接用户账号")

    # 解析 chat_id：如果是纯数字字符串（含负号），转为 int；否则当作 username
    try:
        cid = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
    except ValueError:
        cid = chat_id

    try:
        chat = await app_client.get_chat(cid)
        return {
            "id": chat.id,
            "title": _chat_title_of(chat),
            "type": _chat_type_of(chat),
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("获取 chat 信息失败: %s", chat_id)
        raise HTTPException(status_code=404, detail=f"获取会话信息失败：{e}") from e


# ──────────────────────────────────────────────
# 插件动作（config_schema 的 action 按钮触发 ctx.action 注册的函数）
# ──────────────────────────────────────────────
@app.post("/api/plugins/{plugin_id}/action/{action}")
async def invoke_plugin_action(plugin_id: str, action: str, user=Depends(_auth_pwc)):
    """触发插件用 ctx.action(name) 注册的动作。插件须已启用并注册了该动作。
    返回 {ok, message}：处理器返回 dict 原样透出，str 作为 message，None 视为成功。"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    handler = runtime.get_action_handler(plugin_id, action)
    if handler is None:
        raise HTTPException(status_code=503, detail="插件未启用或未注册该动作")
    try:
        result = await handler()
    except Exception as e:  # noqa: BLE001
        logger.exception("插件动作执行失败: %s::%s", plugin_id, action)
        raise HTTPException(status_code=500, detail=f"动作执行失败：{e}") from e
    if isinstance(result, dict):
        return {"ok": bool(result.get("ok", True)), **result}
    if isinstance(result, str):
        return {"ok": True, "message": result}
    return {"ok": True, "message": "已执行"}


# ──────────────────────────────────────────────
# 插件 webhook 信息（入站地址见 /api/v1/plugin/<id>/webhook；密钥用平台统一的 WEBHOOK_SECRET）
# ──────────────────────────────────────────────
@app.get("/api/plugins/{plugin_id}/webhook")
async def get_plugin_webhook(plugin_id: str, user=Depends(_auth)):
    """读取插件 webhook 信息：是否声明支持、入站路径、以及平台统一密钥（用于拼完整地址）。
    密钥即「系统设置 → 通知」里的 WEBHOOK_SECRET，所有插件与平台 webhook 共用。"""
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    import config.config as cfg
    secret = str((cfg.load().get("WEBHOOK_SECRET") or "")).strip()
    return {
        "webhook": bool(meta.webhook),
        "secret": secret,
        "path": f"/api/v1/plugin/{plugin_id}/webhook",
    }


# ──────────────────────────────────────────────
# 多 Bot / 通知推送路由（平台集中管理：哪个插件推送到哪个 Bot）
# ──────────────────────────────────────────────
@app.get("/api/bots")
async def list_bots_api(user=Depends(_auth)):
    """列出所有已配置 Bot（默认 Bot + 额外 Bot）及在线状态。"""
    accounts = _get_accounts()
    return {"bots": await accounts.list_bots()}


@app.get("/api/bots/routing")
async def get_bots_routing(user=Depends(_auth)):
    """推送路由总览：可选渠道列表 + 每个插件当前推送到哪个渠道（空=默认渠道）。"""
    accounts = _get_accounts()
    bots = await accounts.list_bots()
    for bot in bots:
        bot["type"] = "telegram"

    # 将所有启用的非 Telegram 通知渠道也加入列表（供前端多选展示）
    import config.config as _cfg_mod
    cfg_data = _cfg_mod.load()
    channels = cfg_data.get("NOTIFICATION_CHANNELS") or []
    default_channel_id = next((
        str(ch.get("id") or "").strip()
        for ch in channels
        if isinstance(ch, dict) and ch.get("enabled") and ch.get("is_default") and ch.get("id")
    ), "")
    if default_channel_id:
        for bot in bots:
            bot["is_default"] = bot.get("id") == default_channel_id

    for ch in channels:
        if not isinstance(ch, dict):
            continue
        if ch.get("type") == "telegram":
            continue   # Telegram 渠道已通过 list_bots() 返回
        if not ch.get("enabled"):
            continue
        ch_id = str(ch.get("id") or "").strip()
        if not ch_id or any(b["id"] == ch_id for b in bots):
            continue
        ch_type = str(ch.get("type") or "").upper()
        bots.append({
            "id": ch_id,
            "name": ch.get("name") or ch_id,
            "online": True,
            "username": f"[{ch_type}]",
            "is_default": ch_id == default_channel_id,
            "is_builtin": False,
            "type": str(ch.get("type") or ""),
        })

    plugins = [
        {"id": m.id, "name": m.name, "scope": m.scope,
         "bot": registry.get_bot_choice(m.id), "error": bool(m.error)}
        for m in registry.scan()
    ]
    return {"bots": bots, "plugins": plugins}


@app.put("/api/bots/routing")
async def set_bots_routing(body: Dict[str, Any], user=Depends(_auth_pwc)):
    """设置某个插件推送到哪个渠道（bot_id 空=跟随默认，支持逗号分隔多渠道）。"""
    plugin_id = str(body.get("plugin_id") or "").strip()
    bot_id = str(body.get("bot_id") or "").strip()
    if not plugin_id:
        raise HTTPException(status_code=400, detail="缺少 plugin_id")
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    # 校验每个渠道 ID：Telegram bot 或 NOTIFICATION_CHANNELS 中的渠道都合法
    if bot_id:
        accounts = _get_accounts()
        valid_bots = {b["id"] for b in await accounts.list_bots()}
        import config.config as _cfg_mod
        cfg_data = _cfg_mod.load()
        valid_channels = {
            str(ch.get("id") or "").strip(): str(ch.get("type") or "").strip()
            for ch in (cfg_data.get("NOTIFICATION_CHANNELS") or [])
            if isinstance(ch, dict) and ch.get("id") and ch.get("enabled")
        }
        channel_types = {bot_id: "telegram" for bot_id in valid_bots}
        channel_types.update(valid_channels)
        valid = set(channel_types)
        # 支持逗号分隔的多渠道
        ids = [i.strip() for i in bot_id.split(",") if i.strip()]
        invalid = [i for i in ids if i not in valid]
        if invalid:
            raise HTTPException(status_code=400, detail=f"渠道不存在：{', '.join(invalid)}")
        if meta.scope in {"bot", "both"}:
            unsupported = [i for i in ids if channel_types.get(i) != "telegram"]
            if unsupported:
                raise HTTPException(status_code=400, detail="机器人插件只能使用 Telegram 通知渠道")
    registry.set_bot_choice(plugin_id, bot_id)
    runtime = _get_runtime()
    if runtime.is_loaded(plugin_id):
        await runtime.reload(plugin_id)
    return {"status": "success", "plugin_id": plugin_id, "bot": registry.get_bot_choice(plugin_id)}



# ──────────────────────────────────────────────
# 账号管理 API
# ──────────────────────────────────────────────
@app.get("/api/accounts")
async def list_accounts(user=Depends(_auth)):
    """列出所有账号及在线状态"""
    accounts = _get_accounts()
    return {"accounts": await accounts.list_accounts()}


@app.post("/api/accounts/{session_name}/online")
async def account_online(session_name: str, user=Depends(_auth)):
    """上线已登录账号，并重挂插件"""
    accounts = _get_accounts()
    runtime = _get_runtime()
    try:
        await accounts.set_online(session_name)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    await runtime.resync()
    return {"status": "success"}


@app.post("/api/accounts/{session_name}/offline")
async def account_offline(session_name: str, user=Depends(_auth)):
    """下线账号"""
    accounts = _get_accounts()
    runtime = _get_runtime()
    await accounts.set_offline(session_name)
    await runtime.resync()
    return {"status": "success"}


@app.delete("/api/accounts/{session_name}")
async def account_delete(session_name: str, user=Depends(_auth)):
    """彻底删除账号（停连接 + 删 session + 移出 config）"""
    accounts = _get_accounts()
    runtime = _get_runtime()
    await accounts.remove_account(session_name)
    await runtime.resync()
    return {"status": "success"}


@app.post("/api/accounts/login/send_code")
async def login_send_code(body: Dict[str, Any], user=Depends(_auth)):
    """登录第一步：发送验证码。body: {session, phone}"""
    accounts = _get_accounts()
    session_name = (body.get("session") or "").strip()
    phone = body.get("phone") or ""
    if not session_name:
        raise HTTPException(status_code=400, detail="缺少 session 名称")
    try:
        return await accounts.login_send_code(session_name, phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"发送验证码失败: {e}")


@app.post("/api/accounts/login/submit_code")
async def login_submit_code(body: Dict[str, Any], user=Depends(_auth)):
    """登录第二步：提交验证码。body: {session, code}"""
    accounts = _get_accounts()
    runtime = _get_runtime()
    session_name = (body.get("session") or "").strip()
    code = body.get("code") or ""
    try:
        result = await accounts.login_submit_code(session_name, code)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"验证码校验失败: {e}")
    if result.get("ok"):
        await runtime.resync()
    return result


@app.post("/api/accounts/login/submit_password")
async def login_submit_password(body: Dict[str, Any], user=Depends(_auth)):
    """登录第三步（可选）：提交两步验证密码。body: {session, password}"""
    accounts = _get_accounts()
    runtime = _get_runtime()
    session_name = (body.get("session") or "").strip()
    password = body.get("password") or ""
    try:
        result = await accounts.login_submit_password(session_name, password)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"两步验证失败: {e}")
    if result.get("ok"):
        await runtime.resync()
    return result


# ──────────────────────────────────────────────
# 平台设置（config.json 读写）
# ──────────────────────────────────────────────
# 敏感字段：读取时打码，仅当前端传回非打码值才更新
_SECRET_FIELDS = ("API_HASH", "BOT_TOKEN", "WEBHOOK_SECRET", "API_KEY")
_MASK = "********"
_CHANNEL_SECRET_FIELDS = {
    "telegram": {"token"},
    "wechat": {"secret", "token", "aes_key"},
    "bark": {"device_key"},
}


def _mask(val: str) -> str:
    if not val:
        return ""
    # 敏感字段一律全打码，不回显任何明文片段
    return _MASK


def _clean_notification_channels(value: Any, current: Any,
                                  legacy_settings: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """清洗通知渠道，并把前端传回的打码密钥替换成原值。

    插件与渠道的对应关系只保存在插件路由中，避免两份数据互相覆盖。
    """
    old_channels = {
        str(item.get("id") or ""): item
        for item in (current or []) if isinstance(item, dict)
    }
    cleaned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    has_default = False
    for item in value or []:
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("id") or "").strip()
        channel_type = str(item.get("type") or "").strip()
        if (not channel_id or channel_id in seen_ids
                or channel_type not in _CHANNEL_SECRET_FIELDS):
            continue
        if channel_id == "default" and channel_type != "telegram":
            continue
        seen_ids.add(channel_id)
        raw_config = item.get("config") or {}
        channel_config = dict(raw_config) if isinstance(raw_config, dict) else {}
        old_config = old_channels.get(channel_id, {}).get("config") or {}
        for field in _CHANNEL_SECRET_FIELDS[channel_type]:
            value_now = channel_config.get(field, "")
            if value_now == _MASK or (isinstance(value_now, str) and _MASK in value_now):
                original = old_config.get(field, "")
                # 第一次从旧 Bot 配置迁移时，新渠道尚无原值，需按相同 id 回填旧 Token。
                if not original and field == "token" and channel_type == "telegram" and legacy_settings:
                    if channel_id == "default":
                        original = legacy_settings.get("BOT_TOKEN", "")
                    else:
                        original = next((
                            bot.get("token", "")
                            for bot in legacy_settings.get("BOTS") or []
                            if isinstance(bot, dict) and str(bot.get("id") or "") == channel_id
                        ), "")
                channel_config[field] = original
        enabled = bool(item.get("enabled"))
        is_default = bool(item.get("is_default")) and enabled and not has_default
        has_default = has_default or is_default
        cleaned.append({
            "id": channel_id,
            "name": str(item.get("name") or channel_id).strip() or channel_id,
            "type": channel_type,
            "enabled": enabled,
            "is_default": is_default,
            "config": channel_config,
        })
    return cleaned


@app.get("/api/settings")
async def get_settings_api(user=Depends(_auth)):
    """读取平台设置（敏感字段打码）"""
    import config.config as cfg
    data = cfg.load()
    out = copy.deepcopy(data)
    for f in _SECRET_FIELDS:
        out[f] = _mask(data.get(f, ""))
    # proxy 密码打码
    try:
        if out.get("proxy_set", {}).get("proxy", {}).get("password"):
            out["proxy_set"]["proxy"]["password"] = _MASK
    except Exception:  # noqa: BLE001
        pass
    # 通知渠道中的 Token、Secret 和设备密钥同样不得回显明文。
    for channel in out.get("NOTIFICATION_CHANNELS") or []:
        if not isinstance(channel, dict):
            continue
        channel_type = str(channel.get("type") or "")
        channel_config = channel.get("config") or {}
        if not isinstance(channel_config, dict):
            continue
        for field in _CHANNEL_SECRET_FIELDS.get(channel_type, set()):
            if channel_config.get(field):
                channel_config[field] = _MASK
    # DB 密码打码
    try:
        if out.get("DB_INFO", {}).get("password"):
            out["DB_INFO"]["password"] = _MASK
    except Exception:  # noqa: BLE001
        pass
    # 仓库设置只返回公开地址，清理旧配置中可能残留的私有仓库凭据。
    out["PLUGIN_REPOS"] = [
        {"url": str(r.get("url") or "").strip()}
        for r in (data.get("PLUGIN_REPOS") or [])
        if isinstance(r, dict) and str(r.get("url") or "").strip()
    ]
    # 额外 Bot 各自的 token 打码
    try:
        for b in out.get("BOTS", []) or []:
            if isinstance(b, dict) and b.get("token"):
                b["token"] = _MASK
    except Exception:  # noqa: BLE001
        pass
    from libs.log_cleaner_settings import get_log_cleaner_settings
    out["LOG_CLEANER"] = get_log_cleaner_settings()
    return {"settings": out}


@app.put("/api/settings")
async def put_settings_api(body: Dict[str, Any], user=Depends(_auth_pwc)):
    """
    保存平台设置到 config.json。打码值（未改动）保留原值。
    敏感凭据变更需重启平台生效，返回 restart_required。
    """
    import config.config as cfg
    incoming = body.get("settings") or body
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="settings 必须是对象")

    current = cfg.load()
    merged = dict(current)

    for k, v in incoming.items():
        if k not in cfg.ALLOWED_KEYS:
            continue
        # 顶层敏感字段：打码值则跳过（保留原值）
        if k in _SECRET_FIELDS and (v == _MASK or (isinstance(v, str) and _MASK in v)):
            continue
        # proxy/DB 密码：打码则保留原密码
        if k == "proxy_set" and isinstance(v, dict):
            pw = v.get("proxy", {}).get("password")
            if pw == _MASK:
                v.setdefault("proxy", {})["password"] = current.get("proxy_set", {}).get("proxy", {}).get("password", "")
        if k == "DB_INFO" and isinstance(v, dict) and v.get("password") == _MASK:
            v["password"] = current.get("DB_INFO", {}).get("password", "")
        if k == "NOTIFICATION_CHANNELS" and isinstance(v, list):
            v = _clean_notification_channels(
                v, current.get("NOTIFICATION_CHANNELS"), legacy_settings=current,
            )
        # 插件仓库只接受公开地址，忽略旧客户端传来的 token 等额外字段。
        if k == "PLUGIN_REPOS" and isinstance(v, list):
            cleaned_repos = []
            seen_repos = set()
            for r in v:
                if not isinstance(r, dict):
                    continue
                url = cfg.normalize_plugin_repo(r.get("url"))
                key = url.casefold()
                if not url or key in seen_repos:
                    continue
                seen_repos.add(key)
                cleaned_repos.append({"url": url})
            v = cleaned_repos
        # 额外 Bot：打码 token 按 id 匹配原值保留；剔除缺 id/token 的畸形项
        if k == "BOTS" and isinstance(v, list):
            old_bot_tokens = {b.get("id"): b.get("token", "")
                              for b in (current.get("BOTS") or []) if isinstance(b, dict)}
            cleaned_bots = []
            for b in v:
                if not isinstance(b, dict):
                    continue
                bid = str(b.get("id") or "").strip()
                if not bid or bid == "default":
                    continue
                tok = b.get("token", "")
                if tok == _MASK or (isinstance(tok, str) and _MASK in tok):
                    tok = old_bot_tokens.get(bid, "")
                cleaned_bots.append({"id": bid, "name": str(b.get("name") or bid), "token": tok,
                                     "chat_id": str(b.get("chat_id") or "").strip()})
            v = cleaned_bots
        merged[k] = v

    merged["BOT_NAME"] = str(merged.get("BOT_NAME") or "").strip() or "主要通知渠道"
    merged["DEFAULT_BOT_ID"] = cfg.normalize_default_bot_id(
        merged.get("DEFAULT_BOT_ID"), merged.get("BOTS")
    )

    restart_keys = {"API_ID", "API_HASH", "proxy_set", "DB_INFO", "WEB_UI_PORT"}
    restart_required = any(current.get(key) != merged.get(key) for key in restart_keys)

    cfg.save(merged)
    logger.info("平台设置已更新（config.json）")

    if "LOG_CLEANER" in incoming:
        from libs.log_cleaner_settings import get_log_cleaner_settings
        log_cleaner_changed = incoming["LOG_CLEANER"] != get_log_cleaner_settings()
        if log_cleaner_changed:
            from libs.log_cleaner_settings import save_log_cleaner_settings
            from schedulers.universal.log_cleaner import start_log_cleaner
            save_log_cleaner_settings(incoming["LOG_CLEANER"])
            await start_log_cleaner()
            logger.info("日志清理设置已更新")

    bot_sync = None
    # 通知渠道中的 Bark/企业微信配置不影响 Telegram Bot 连接。
    # Telegram 渠道已由前端同步进 BOT_TOKEN/BOTS 等兼容字段。
    bot_keys = {"BOT_TOKEN", "BOT_NAME", "DEFAULT_BOT_ID", "BOTS"}
    bot_changed = any(current.get(key) != merged.get(key) for key in bot_keys)
    connection_base_changed = any(current.get(key) != merged.get(key) for key in ("API_ID", "API_HASH", "proxy_set"))

    # Bot 名称、Token、默认项和列表都支持热更新；基础连接参数变化时仍随平台重启生效。
    if bot_changed and not connection_base_changed:
        try:
            accounts = _get_accounts()
            bot_sync = await accounts.sync_bots(current, merged)
            if merged.get("DEFAULT_BOT_ID") != bot_sync["default_id"]:
                merged["DEFAULT_BOT_ID"] = bot_sync["default_id"]
            failed_ids = {item["id"] for item in bot_sync.get("failed", [])}
            if "default" in failed_ids and current.get("BOT_TOKEN"):
                merged["BOT_TOKEN"] = current["BOT_TOKEN"]
            old_tokens = {
                str(bot.get("id") or ""): bot.get("token", "")
                for bot in current.get("BOTS") or [] if isinstance(bot, dict)
            }
            for bot in merged.get("BOTS") or []:
                bot_id = str(bot.get("id") or "") if isinstance(bot, dict) else ""
                if bot_id in failed_ids and bot_id in old_tokens:
                    bot["token"] = old_tokens[bot_id]
            if failed_ids or merged.get("DEFAULT_BOT_ID") != current.get("DEFAULT_BOT_ID"):
                cfg.save(merged)
            logger.info("Bot 设置已热更新")
        except Exception as e:  # noqa: BLE001
            restart_required = True
            bot_sync = {"failed": [], "message": "Bot 即时更新失败，重启平台后生效"}
            logger.warning("Bot 设置热更新失败，将在重启后生效: %r", e)

    # 被删除或停用的通知渠道必须立即从插件路由中移除。
    routing_changed = False
    if "NOTIFICATION_CHANNELS" in incoming:
        old_channel_ids = {
            str(channel.get("id") or "").strip()
            for channel in current.get("NOTIFICATION_CHANNELS") or []
            if isinstance(channel, dict) and channel.get("id")
        }
        new_channel_ids = {
            str(channel.get("id") or "").strip()
            for channel in merged.get("NOTIFICATION_CHANNELS") or []
            if isinstance(channel, dict) and channel.get("enabled") and channel.get("id")
        }
        unavailable_ids = old_channel_ids - new_channel_ids
        affected: set[str] = set()
        for channel_id in unavailable_ids:
            affected.update(registry.purge_bot(channel_id))
        if affected:
            routing_changed = True
            logger.info("%d 个通知渠道已停用或删除，已同步更新 %d 个插件路由",
                        len(unavailable_ids), len(affected))

    # 兼容旧 Bot 配置：被删除的 Bot 若有插件路由指向它，同样移除该项。
    if "BOTS" in incoming:
        try:
            old_ids = {b.get("id") for b in (current.get("BOTS") or []) if isinstance(b, dict)}
            new_ids = {b.get("id") for b in (merged.get("BOTS") or []) if isinstance(b, dict)}
            removed = [bid for bid in old_ids - new_ids if bid]
            affected: set[str] = set()
            for bid in removed:
                affected.update(registry.purge_bot(bid))
            if affected:
                routing_changed = True
                logger.info("已删除 %d 个 Bot，%d 个插件推送路由回退默认 Bot", len(removed), len(affected))
        except Exception as e:  # noqa: BLE001
            logger.warning("处理 Bot 删除的推送路由回退失败: %r", e)

    if routing_changed or (bot_sync and bot_sync.get("needs_resync")):
        try:
            await _get_runtime().resync()
        except Exception as e:  # noqa: BLE001
            logger.warning("Bot 更新后重新挂载插件失败: %r", e)

    # 代理变更 → 立即刷新进程环境变量，新启动/重载的插件即时生效（长连接旧客户端仍需重启）
    if "proxy_set" in incoming:
        try:
            from libs.proxy import export_env
            export_env()
        except Exception as e:  # noqa: BLE001
            logger.warning("刷新代理环境变量失败: %r", e)

    # 插件仓库相关设置变更 → 重排轮询任务（即时生效，无需重启）
    repo_keys = {"PLUGIN_REPO_ENABLE", "PLUGIN_REPOS", "PLUGIN_REPO_INTERVAL"}
    if any(current.get(k) != merged.get(k) for k in repo_keys):
        try:
            from webui import repo_sync
            repo_sync.reschedule()
        except Exception as e:  # noqa: BLE001
            logger.warning("重排插件仓库轮询失败: %r", e)

    # 只有凭据、代理、数据库和监听端口等运行参数发生变化时才需要重启。
    return {"status": "success", "restart_required": restart_required, "bot_sync": bot_sync}


@app.post("/api/system/restart")
async def restart_platform(user=Depends(_auth_pwc)):
    """重启平台。容器(restart:always)下进程退出会被自动拉起；
    裸跑需外部进程守护(systemd/supervisor)才能自动重启。"""
    import asyncio as _aio
    import os as _os

    async def _delayed_exit():
        await _aio.sleep(0.5)   # 让本次 HTTP 响应先返回
        logger.info("收到重启请求，进程即将退出由守护进程拉起…")
        _os._exit(0)

    _aio.create_task(_delayed_exit())
    return {"status": "success", "message": "平台正在重启，请稍候刷新页面"}


# ──────────────────────────────────────────────
# 连接测试（代理 / 数据库）——用「当前表单值」测，打码密码回落已保存值
# ──────────────────────────────────────────────
@app.post("/api/system/backup")
async def create_system_backup(user=Depends(_auth_pwc)):
    """导出平台备份压缩包。"""
    try:
        archive_path, filename = create_backup_archive(APP_VERSION)
    except (BackupError, OSError, sqlite3.Error) as e:
        raise HTTPException(status_code=500, detail=f"生成备份失败: {e}") from e
    logger.info("平台备份已生成: %s", archive_path)
    return FileResponse(
        str(archive_path),
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.get("/api/system/backups/{filename}")
async def download_stored_backup(filename: str, user=Depends(_auth_pwc)):
    """下载恢复前快照；传输完成后删除服务器副本。"""
    try:
        archive_path = stored_backup_path(filename)
    except BackupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return FileResponse(
        str(archive_path),
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.post("/api/system/restore")
async def restore_system_backup(file: UploadFile = File(...), user=Depends(_auth_pwc)):
    """校验并暂存备份包，等待平台重启后安全恢复。"""
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="仅支持导入 .zip 备份包")

    Path("data").mkdir(parents=True, exist_ok=True)
    fd, upload_name = tempfile.mkstemp(prefix=".restore-upload-", suffix=".zip", dir="data")
    upload_path = Path(upload_name)
    try:
        total = 0
        with os.fdopen(fd, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise HTTPException(status_code=413, detail="备份包超过 1 GiB 上传限制")
                output.write(chunk)
        inspection, pre_restore_name = stage_restore_archive(upload_path, APP_VERSION)
        upload_path = None
        try:
            prune_stored_backups()
        except OSError as prune_error:
            logger.warning("清理旧恢复快照失败: %s", prune_error)
    except BackupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"暂存恢复失败: {e}") from e
    finally:
        await file.close()
        if upload_path is not None:
            upload_path.unlink(missing_ok=True)

    logger.info("平台恢复包已校验并暂存: %s，文件 %d 个", filename, inspection.file_count)
    return {
        "status": "success",
        "staged_files": inspection.file_count,
        "restore_pending": True,
        "restart_required": True,
        "pre_restore_backup": pre_restore_name,
    }


@app.post("/api/system/clean_logs")
async def clean_logs_now(user=Depends(_auth)):
    """立即执行日志清理任务"""
    try:
        from schedulers.universal.log_cleaner import log_cleaner_action
        await log_cleaner_action()
        logger.info("用户手动触发日志清理完成")
        return {"ok": True, "message": "日志清理完成"}
    except Exception as e:
        logger.exception("手动清理日志失败")
        raise HTTPException(status_code=500, detail=f"清理失败: {e}") from e


@app.post("/api/settings/test_proxy")
async def test_proxy(body: Dict[str, Any], user=Depends(_auth)):
    """用提交的 proxy_set 试连一次外网。返回 {ok, message}，不抛异常（失败也是 200）。"""
    import config.config as cfg
    ps = (body or {}).get("proxy_set") or body or {}
    cur = cfg.load()
    px = dict(ps.get("proxy") or {})
    # 打码密码回落已保存值
    if px.get("password") == _MASK:
        px["password"] = cur.get("proxy_set", {}).get("proxy", {}).get("password", "")

    url = (ps.get("PROXY_URL") or "").strip()
    if not url:
        host, port = px.get("hostname"), px.get("port")
        if not (host and port):
            return {"ok": False, "message": "未填写代理主机/端口"}
        from urllib.parse import quote
        scheme = px.get("scheme", "http")
        uname, pwd = px.get("username", ""), px.get("password", "")
        # 用户名/密码可能含特殊字符，转义后再拼进 URL
        auth = f"{quote(str(uname), safe='')}:{quote(str(pwd), safe='')}@" if uname else ""
        url = f"{scheme}://{auth}{host}:{port}"

    import httpx
    try:
        async with httpx.AsyncClient(proxy=url, timeout=8, trust_env=False) as client:
            r = await client.get("https://api.telegram.org")
        return {"ok": True, "message": f"代理可用（可达 api.telegram.org，HTTP {r.status_code}）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"代理连接失败：{e.__class__.__name__}: {e}"}


@app.post("/api/settings/test_db")
async def test_db(body: Dict[str, Any], user=Depends(_auth)):
    """用提交的 DB_INFO 试连一次数据库。返回 {ok, message}，不抛异常（失败也是 200）。"""
    import asyncio as _aio
    from urllib.parse import quote_plus
    import config.config as cfg
    db = (body or {}).get("DB_INFO") or body or {}
    cur = cfg.load()
    db = dict(db)
    if db.get("password") == _MASK:
        db["password"] = cur.get("DB_INFO", {}).get("password", "")

    dbset = db.get("dbset", "SQLite")
    if dbset == "SQLite":
        return {"ok": True, "message": "SQLite 为本地文件，无需测试连接"}

    pwd = quote_plus(str(db.get("password", "")))
    user_, addr, port, name = db.get("user"), db.get("address"), db.get("port"), db.get("db_name")
    if dbset == "mySQL":
        dsn = f"mysql+aiomysql://{user_}:{pwd}@{addr}:{port}/{name}"
    elif dbset == "PostgreSQL":
        dsn = f"postgresql+asyncpg://{user_}:{pwd}@{addr}:{port}/{name}"
    else:
        return {"ok": False, "message": f"未知数据库类型：{dbset}"}

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text as _text
    engine = create_async_engine(dsn)
    try:
        async def _probe():
            async with engine.connect() as conn:
                await conn.execute(_text("SELECT 1"))
        await _aio.wait_for(_probe(), timeout=8)
        return {"ok": True, "message": "数据库连接成功"}
    except _aio.TimeoutError:
        return {"ok": False, "message": "连接超时（检查地址/端口/网络）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"连接失败：{e.__class__.__name__}: {e}"}
    finally:
        await engine.dispose()


# ──────────────────────────────────────────────
# Webhook 入站端点（公开，靠 apikey 查询串鉴权，不走登录令牌）
#   平台级：/api/v1/webhook?apikey=<WEBHOOK_SECRET>          → 推送给管理员
#   插件级：/api/v1/plugin/<id>/webhook?apikey=<WEBHOOK_SECRET> → 交插件处理器
#   两者共用同一个平台密钥（不为插件单独生成）。
# ──────────────────────────────────────────────
def _apikey_ok(given: str, secret: str) -> bool:
    """恒定时间比对 apikey。转 bytes 再比：hmac.compare_digest 对含非 ASCII 的 str
    会抛 TypeError，直接比 str 会让「?apikey=中文/乱码」变成 500 而非干脆 401。"""
    return hmac.compare_digest((given or "").encode("utf-8"), (secret or "").encode("utf-8"))


async def _build_webhook_request(request: Request):
    """把 FastAPI Request 转成给插件的轻量 WebhookRequest（剔除 apikey）。"""
    from kernel.context import WebhookRequest
    import json as _json

    query = {k: v for k, v in request.query_params.items() if k != "apikey"}
    headers = {k.lower(): v for k, v in request.headers.items()}
    body = await request.body()
    json_data = None
    if body:
        try:
            json_data = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            json_data = None
    return WebhookRequest(request.method, query, headers, body, json_data)


def _webhook_result_response(result: Any):
    """把插件处理器返回值转成 HTTP 响应：dict→JSON / str→文本 / None→{"ok":true}。"""
    if result is None:
        return {"ok": True}
    if isinstance(result, (dict, list)):
        return JSONResponse(result)
    return PlainTextResponse(str(result))


@app.api_route("/api/v1/plugin/{plugin_id}/webhook", methods=["GET", "POST"])
async def plugin_webhook(plugin_id: str, request: Request):
    """插件 webhook 入站：校验平台统一密钥 → 交给插件 ctx.on_webhook 注册的处理器。
    apikey 用「系统设置 → 通知」的 WEBHOOK_SECRET（与平台 webhook 共用，不单独生成）。"""
    import config.config as cfg
    secret = str((cfg.load().get("WEBHOOK_SECRET") or "")).strip()
    if not secret:
        # 未设平台密钥即视为 webhook 未开启，不泄露插件是否存在
        raise HTTPException(status_code=404, detail="webhook 未开启（请先在系统设置生成密钥）")
    given = request.query_params.get("apikey", "")
    if not _apikey_ok(given, secret):
        raise HTTPException(status_code=401, detail="apikey 无效")

    runtime = _get_runtime()
    handler = runtime.get_webhook_handler(plugin_id)
    if handler is None:
        raise HTTPException(status_code=503, detail="插件未启用或未注册 webhook 处理器")

    wreq = await _build_webhook_request(request)
    try:
        result = await handler(wreq)
    except Exception as e:  # noqa: BLE001 - 插件处理器异常不外泄堆栈
        logger.exception("插件 webhook 处理失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail="webhook 处理失败") from e
    return _webhook_result_response(result)


@app.api_route("/api/v1/webhook", methods=["GET", "POST"])
async def platform_webhook(request: Request):
    """平台 webhook 入站：校验 WEBHOOK_SECRET → 把内容推送给平台管理员。
    JSON 里若带 text/message/content 字段用其内容，否则推整段文本/JSON。
    可选 title/category 字段作为标题分类。"""
    import config.config as cfg
    secret = str((cfg.load().get("WEBHOOK_SECRET") or "")).strip()
    if not secret:
        raise HTTPException(status_code=404, detail="平台 webhook 未开启")
    given = request.query_params.get("apikey", "")
    if not _apikey_ok(given, secret):
        raise HTTPException(status_code=401, detail="apikey 无效")

    wreq = await _build_webhook_request(request)
    data = wreq.json if isinstance(wreq.json, dict) else None
    if data:
        text = str(data.get("text") or data.get("message") or data.get("content") or "").strip()
        if not text:
            import json as _json
            text = _json.dumps(data, ensure_ascii=False, indent=2)
        title = str(data.get("title") or "").strip()
        category = str(data.get("category") or "").strip() or None
        body_text = f"{title}\n{text}" if title else text
    else:
        body_text = wreq.text.strip() or "(空内容)"
        category = None

    from kernel import notifier
    accounts = _get_accounts()
    try:
        await notifier.submit(
            accounts, "__platform_webhook__", "平台 Webhook", body_text,
            level="info", category=category,
        )
    except RuntimeError as e:
        # 无可用账号投递（Bot 未连接且无在线用户账号）
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 - 投递失败（多为 Chat ID 配置错误 / Bot 无权限）
        logger.warning("平台 webhook 投递失败: %r", e)
        raise HTTPException(
            status_code=502,
            detail=f"通知投递失败：{e.__class__.__name__}（请检查通知 Chat ID 是否正确、Bot 是否有权限）",
        ) from e
    return {"ok": True}


# ──────────────────────────────────────────────
# 插件商店（多仓库聚合）
# ──────────────────────────────────────────────
@app.get("/api/plugins/store")
async def plugin_store(refresh: bool = True, user=Depends(_auth)):
    """聚合所有已配置仓库的插件列表，标记 installed。refresh=false 走缓存。"""
    from webui import repo_sync
    return await repo_sync.list_store(refresh=refresh)


@app.post("/api/plugins/store/download")
async def plugin_store_download(body: Dict[str, Any], user=Depends(_auth_pwc)):
    """下载选定插件到本地（不启用）。body: {plugins: [<商店列表里的插件对象>]}。"""
    from webui import repo_sync
    plugins = body.get("plugins") or []
    if not isinstance(plugins, list) or not plugins:
        raise HTTPException(status_code=400, detail="未选择任何插件")
    result = await repo_sync.download_plugins(plugins)
    return {"status": "success", "result": result}


@app.get("/api/plugins/repo/status")
async def repo_sync_status(user=Depends(_auth)):
    """读取轮询/商店状态摘要。"""
    from webui import repo_sync
    return {"status": "success", **repo_sync.get_store_status()}


# ──────────────────────────────────────────────
# 系统状态
# ──────────────────────────────────────────────
@app.get("/api/status")
async def system_status(user=Depends(_auth)):
    import time as _time
    import sys as _sys
    import platform as _platform
    from kernel import state as kernel_state
    acc = kernel_state.accounts
    runtime = kernel_state.runtime

    # 插件统计
    metas = registry.scan()
    plugin_total = len(metas)
    plugin_enabled = sum(1 for m in metas if m.enabled)
    plugin_error = sum(1 for m in metas if m.error)

    # 账号列表
    accounts_info = []
    if acc:
        try:
            accounts_info = await acc.list_accounts()
        except Exception:  # noqa: BLE001
            accounts_info = []

    # 插件 id → 名称（图例 / 定时任务归属用）
    plugin_names = {m.id: m.name for m in metas}

    # 调度任务
    sched_jobs = []
    try:
        # 状态接口只读取已经启动的调度器，不能为了展示数据加载整套任务模块。
        sched_module = _sys.modules.get("schedulers")
        active_scheduler = getattr(sched_module, "scheduler", None)
        for j in active_scheduler.get_jobs() if active_scheduler else ():
            nxt = getattr(j, "next_run_time", None)
            # job id 形如 "<插件id>::<名称>"，据此归属到插件
            jid = str(j.id)
            owner_id, _, short = jid.partition("::")
            if short:
                plugin_id = owner_id
                plugin_label = plugin_names.get(owner_id, owner_id)
                job_label = short
            else:
                plugin_id = None
                plugin_label = "平台"
                job_label = jid
            sched_jobs.append({
                "id": jid,
                "name": job_label,
                "plugin_id": plugin_id,
                "plugin": plugin_label,
                "trigger": str(getattr(j, "trigger", "")),
                "next": nxt.strftime("%m-%d %H:%M:%S") if nxt else None,
            })
    except Exception:  # noqa: BLE001
        pass

    # 运行时长
    started = getattr(kernel_state, "started_at", None)
    uptime = int(_time.time() - started) if started else 0

    # 插件活跃时间线
    try:
        from kernel import activity as _activity
        activity_data = _activity.timeline()
    except Exception:  # noqa: BLE001
        activity_data = {"bucket_seconds": 3600, "buckets": [], "totals": {}}

    return {
        "version": APP_VERSION,
        "uptime_seconds": uptime,
        "python": _sys.version.split()[0],
        "platform": f"{_platform.system()} {_platform.release()}",
        "bot_connected": bool(acc and acc.bot_app and acc.bot_app.is_connected) if acc else False,
        "user_connected": bool(acc and acc.primary_user_app) if acc else False,
        "user_count": len(acc.connected_user_apps) if acc else 0,
        "accounts": accounts_info,
        "plugins": {"total": plugin_total, "enabled": plugin_enabled, "error": plugin_error,
                     "loaded": len(runtime.loaded_ids) if runtime else 0},
        "scheduler_jobs": sched_jobs,
        "web_port": config.telegram.web_ui_port,
        "activity": activity_data,
        "plugin_names": plugin_names,
    }


# ──────────────────────────────────────────────
# 运行日志
# ──────────────────────────────────────────────
@app.get("/api/logs/recent")
async def recent_logs(user=Depends(_auth)):
    """返回最近若干条历史日志"""
    from webui import log_stream
    return {"logs": log_stream.recent_logs()}


# ──────────────────────────────────────────────
# 开放平台 API（/api/v1）—— 使用 API Key 鉴权
# ──────────────────────────────────────────────

# 1. 插件管理
@app.get("/api/v1/plugins")
async def api_list_plugins(user=Depends(_api_key)):
    """列出所有插件（包含元数据、状态、配置 schema）"""
    from kernel import state as kernel_state

    plugins = []
    for meta in registry.scan():
        enabled = registry.is_enabled(meta.id)
        # 平台未就绪时，loaded 状态无法获取
        loaded = kernel_state.runtime.is_loaded(meta.id) if kernel_state.runtime else False
        plugins.append({
            "id": meta.id,
            "name": meta.name,
            "version": meta.version,
            "author": meta.author,
            "description": meta.description,
            "scope": meta.scope,
            "enabled": enabled,
            "loaded": loaded,
            "has_config": bool(meta.config_schema),
            "webhook": bool(meta.webhook),
        })
    return {"plugins": plugins, "platform_ready": kernel_state.runtime is not None}


@app.get("/api/v1/plugins/{plugin_id}")
async def api_get_plugin(plugin_id: str, user=Depends(_api_key)):
    """获取插件详情（元数据+状态+配置 schema）"""
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    enabled = registry.is_enabled(plugin_id)
    loaded = runtime.is_loaded(plugin_id)

    return {
        "id": meta.id,
        "name": meta.name,
        "version": meta.version,
        "author": meta.author,
        "description": meta.description,
        "changelog": meta.changelog,
        "scope": meta.scope,
        "default_enabled": meta.default_enabled,
        "enabled": enabled,
        "loaded": loaded,
        "config_schema": meta.config_schema or {},
        "webhook": bool(meta.webhook),
        "render_mode": meta.render_mode,
    }


@app.get("/api/v1/plugins/{plugin_id}/source")
async def api_get_plugin_source(plugin_id: str, user=Depends(_api_key)):
    """读取插件源代码"""
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")

    source_path = PLUGINS_DIR / f"{plugin_id}.py"
    if not source_path.exists():
        # 尝试目录包形式
        source_path = PLUGINS_DIR / plugin_id / "__init__.py"

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="插件源代码文件不存在")

    try:
        source = source_path.read_text(encoding="utf-8")
        return {
            "plugin_id": plugin_id,
            "path": str(source_path.relative_to(Path.cwd())),
            "source": source,
            "is_package": not source_path.name.endswith(f"{plugin_id}.py"),
        }
    except Exception as e:
        logger.exception("读取插件源代码失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail=f"读取失败：{e}") from e


@app.put("/api/v1/plugins/{plugin_id}/source")
async def api_update_plugin_source(plugin_id: str, body: Dict[str, Any], user=Depends(_api_key)):
    """更新插件源代码并自动重载"""
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")

    source = body.get("source", "")
    if not source:
        raise HTTPException(status_code=400, detail="source 字段不能为空")

    source_path = PLUGINS_DIR / f"{plugin_id}.py"
    if not source_path.exists():
        source_path = PLUGINS_DIR / plugin_id / "__init__.py"

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="插件源代码文件不存在")

    try:
        # 写入新代码
        source_path.write_text(source, encoding="utf-8")

        # 如果插件已加载，重载它
        runtime = _get_runtime()
        if runtime.is_loaded(plugin_id):
            await runtime.reload(plugin_id)
            logger.info("插件代码已更新并重载: %s", plugin_id)
            return {"ok": True, "message": "插件代码已更新并重载", "reloaded": True}
        else:
            logger.info("插件代码已更新（未加载，未重载）: %s", plugin_id)
            return {"ok": True, "message": "插件代码已更新（未加载）", "reloaded": False}
    except Exception as e:
        logger.exception("更新插件源代码失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail=f"更新失败：{e}") from e


@app.post("/api/v1/plugins/{plugin_id}/enable")
async def api_enable_plugin(plugin_id: str, user=Depends(_api_key)):
    """启用插件"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    try:
        await runtime.enable(plugin_id)
        logger.info("插件已启用: %s", plugin_id)
        return {"ok": True, "message": "插件已启用"}
    except Exception as e:
        logger.exception("启用插件失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail=f"启用失败：{e}") from e


@app.post("/api/v1/plugins/{plugin_id}/disable")
async def api_disable_plugin(plugin_id: str, user=Depends(_api_key)):
    """停用插件"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    try:
        await runtime.disable(plugin_id)
        logger.info("插件已停用: %s", plugin_id)
        return {"ok": True, "message": "插件已停用"}
    except Exception as e:
        logger.exception("停用插件失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail=f"停用失败：{e}") from e


@app.post("/api/v1/plugins/{plugin_id}/reload")
async def api_reload_plugin(plugin_id: str, user=Depends(_api_key)):
    """重载插件（仅当已加载时生效）"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    if not runtime.is_loaded(plugin_id):
        raise HTTPException(status_code=400, detail="插件未加载，无法重载")
    try:
        await runtime.reload(plugin_id)
        logger.info("插件已重载: %s", plugin_id)
        return {"ok": True, "message": "插件已重载"}
    except Exception as e:
        logger.exception("重载插件失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail=f"重载失败：{e}") from e


# 2. 插件配置
@app.get("/api/v1/plugins/{plugin_id}/config")
async def api_get_plugin_config(plugin_id: str, user=Depends(_api_key)):
    """读取插件配置"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    config_data = registry.get_config(plugin_id)
    return {"plugin_id": plugin_id, "config": config_data}


@app.put("/api/v1/plugins/{plugin_id}/config")
async def api_update_plugin_config(plugin_id: str, body: Dict[str, Any], user=Depends(_api_key)):
    """修改插件配置（完整替换）并重载"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    config_values = body.get("config", {})
    try:
        registry.set_config(plugin_id, config_values)
        runtime = _get_runtime()
        if runtime.is_loaded(plugin_id):
            await runtime.reload(plugin_id)
            logger.info("插件配置已更新并重载: %s", plugin_id)
            return {"ok": True, "message": "配置已更新并重载", "reloaded": True}
        else:
            logger.info("插件配置已更新（未加载）: %s", plugin_id)
            return {"ok": True, "message": "配置已更新（插件未加载）", "reloaded": False}
    except Exception as e:
        logger.exception("更新插件配置失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail=f"更新失败：{e}") from e


# 3. 插件 KV 存储
@app.get("/api/v1/plugins/{plugin_id}/kv")
async def api_list_plugin_kv(plugin_id: str, user=Depends(_api_key)):
    """列出插件 kv 存储的所有键"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    loaded = runtime._loaded.get(plugin_id)
    if loaded is None:
        raise HTTPException(status_code=503, detail="插件未加载，无法访问 kv 存储")
    try:
        keys = list(loaded.ctx.kv.keys())
        return {"plugin_id": plugin_id, "keys": keys}
    except Exception as e:
        logger.exception("读取插件 kv 键列表失败: %s", plugin_id)
        raise HTTPException(status_code=500, detail=f"读取失败：{e}") from e


@app.get("/api/v1/plugins/{plugin_id}/kv/{key}")
async def api_get_plugin_kv(plugin_id: str, key: str, user=Depends(_api_key)):
    """读取插件 kv 存储的某个键"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    loaded = runtime._loaded.get(plugin_id)
    if loaded is None:
        raise HTTPException(status_code=503, detail="插件未加载，无法访问 kv 存储")
    try:
        value = loaded.ctx.kv.get(key, None)
        if value is None:
            raise HTTPException(status_code=404, detail="键不存在")
        return {"plugin_id": plugin_id, "key": key, "value": value}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("读取插件 kv 键失败: %s/%s", plugin_id, key)
        raise HTTPException(status_code=500, detail=f"读取失败：{e}") from e


@app.put("/api/v1/plugins/{plugin_id}/kv/{key}")
async def api_set_plugin_kv(plugin_id: str, key: str, body: Dict[str, Any], user=Depends(_api_key)):
    """写入插件 kv 存储的某个键"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    loaded = runtime._loaded.get(plugin_id)
    if loaded is None:
        raise HTTPException(status_code=503, detail="插件未加载，无法访问 kv 存储")

    if "value" not in body:
        raise HTTPException(status_code=400, detail="请求体必须包含 value 字段")

    try:
        loaded.ctx.kv.set(key, body["value"])
        logger.info("插件 kv 键已设置: %s/%s", plugin_id, key)
        return {"ok": True, "message": "键值已设置"}
    except Exception as e:
        logger.exception("设置插件 kv 键失败: %s/%s", plugin_id, key)
        raise HTTPException(status_code=500, detail=f"设置失败：{e}") from e


@app.delete("/api/v1/plugins/{plugin_id}/kv/{key}")
async def api_delete_plugin_kv(plugin_id: str, key: str, user=Depends(_api_key)):
    """删除插件 kv 存储的某个键"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    runtime = _get_runtime()
    loaded = runtime._loaded.get(plugin_id)
    if loaded is None:
        raise HTTPException(status_code=503, detail="插件未加载，无法访问 kv 存储")

    try:
        loaded.ctx.kv.delete(key)
        logger.info("插件 kv 键已删除: %s/%s", plugin_id, key)
        return {"ok": True, "message": "键已删除"}
    except Exception as e:
        logger.exception("删除插件 kv 键失败: %s/%s", plugin_id, key)
        raise HTTPException(status_code=500, detail=f"删除失败：{e}") from e


# 4. 消息发送
@app.post("/api/v1/messages/send")
async def api_send_message(body: Dict[str, Any], user=Depends(_api_key)):
    """通过 bot 或 user 发送消息"""
    chat_id = body.get("chat_id")
    text = body.get("text", "")
    sender = body.get("sender", "bot")  # "bot" 或 "user"
    parse_mode = body.get("parse_mode")  # "HTML" / "Markdown" / None

    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id 不能为空")
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    accounts = _get_accounts()

    try:
        if sender == "bot":
            bot = accounts.bot_app
            if not bot:
                raise HTTPException(status_code=503, detail="Bot 未连接")
            msg = await bot.send_message(chat_id, text, parse_mode=parse_mode)
        elif sender == "user":
            apps = accounts.connected_user_apps
            if not apps:
                raise HTTPException(status_code=503, detail="没有已连接的用户账号")
            user_app = apps[0]
            msg = await user_app.send_message(chat_id, text, parse_mode=parse_mode)
        else:
            raise HTTPException(status_code=400, detail="sender 必须是 'bot' 或 'user'")

        return {
            "ok": True,
            "message_id": msg.id,
            "chat_id": msg.chat.id if msg.chat else chat_id,
            "date": msg.date.isoformat() if msg.date else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("发送消息失败: chat_id=%s", chat_id)
        raise HTTPException(status_code=500, detail=f"发送失败：{e}") from e


# 5. 会话信息（复用已有逻辑，添加 v1 版本）
@app.get("/api/v1/chats/{chat_id}")
async def api_get_chat_info(chat_id: str, session: str = "", user=Depends(_api_key)):
    """通过 chat_id 获取群组/频道/私聊的信息（使用 API Key 鉴权）"""
    accounts = _get_accounts()
    apps = accounts.connected_user_apps
    if session:
        app_client = next((a for a in apps if getattr(a, "name", None) == session), None)
    else:
        app_client = apps[0] if apps else None
    if app_client is None:
        raise HTTPException(status_code=409, detail="没有可用的已连接用户账号")

    try:
        cid = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
    except ValueError:
        cid = chat_id

    try:
        chat = await app_client.get_chat(cid)
        return {
            "id": chat.id,
            "title": _chat_title_of(chat),
            "type": _chat_type_of(chat),
        }
    except Exception as e:
        logger.exception("获取 chat 信息失败: %s", chat_id)
        raise HTTPException(status_code=404, detail=f"获取会话信息失败：{e}") from e


# 6. 账号状态
@app.get("/api/v1/accounts")
async def api_list_accounts(user=Depends(_api_key)):
    """列出所有账号及在线状态"""
    from kernel import state as kernel_state

    # 平台未就绪时返回空列表
    if kernel_state.accounts is None:
        return {"accounts": [], "platform_ready": False}

    accounts = _get_accounts()
    result = []

    # Bot 账号
    if accounts.bot_app:
        bot = accounts.bot_app
        result.append({
            "type": "bot",
            "session": "default",
            "name": getattr(bot, "name", "Bot"),
            "connected": getattr(bot, "is_connected", False),
        })

    # 用户账号
    for app in accounts.user_apps:
        result.append({
            "type": "user",
            "session": getattr(app, "name", ""),
            "name": getattr(app, "name", ""),
            "connected": getattr(app, "is_connected", False),
        })

    return {"accounts": result, "platform_ready": True}


# 7. 日志查询
@app.get("/api/v1/logs")
async def api_get_logs(limit: int = 100, user=Depends(_api_key)):
    """获取平台最近日志"""
    from webui import log_stream
    logs = log_stream.recent_logs()
    if limit > 0:
        logs = logs[-limit:]
    return {"logs": logs}


@app.get("/api/v1/logs/plugins/{plugin_id}")
async def api_get_plugin_logs(plugin_id: str, limit: int = 100, user=Depends(_api_key)):
    """获取插件专属日志"""
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")

    from webui import log_stream
    all_logs = log_stream.recent_logs()

    # 过滤出包含插件 id 的日志
    plugin_logs = [
        log for log in all_logs
        if f"[{plugin_id}]" in log.get("message", "")
    ]

    if limit > 0:
        plugin_logs = plugin_logs[-limit:]

    return {"plugin_id": plugin_id, "logs": plugin_logs}


# 8. 平台状态
@app.get("/api/v1/status")
async def api_get_platform_status(user=Depends(_api_key)):
    """获取平台运行状态"""
    from kernel import state as kernel_state

    # 平台未完全启动时返回部分状态
    if kernel_state.accounts is None:
        return {
            "version": APP_VERSION,
            "bot_connected": False,
            "user_accounts_count": 0,
            "total_plugins": len(registry.scan()),
            "enabled_plugins": 0,
            "enabled_plugin_ids": [],
            "platform_ready": False,
        }

    accounts = _get_accounts()
    enabled_plugins = [meta.id for meta in registry.scan() if registry.is_enabled(meta.id)]

    return {
        "version": APP_VERSION,
        "bot_connected": accounts.bot_app is not None and getattr(accounts.bot_app, "is_connected", False),
        "user_accounts_count": len(accounts.connected_user_apps),
        "total_plugins": len(registry.scan()),
        "enabled_plugins": len(enabled_plugins),
        "enabled_plugin_ids": enabled_plugins,
        "platform_ready": True,
    }


@app.websocket("/api/logs/ws")
async def logs_ws(ws: WebSocket):
    """实时日志 WebSocket。先推历史，再持续推送新日志。需令牌鉴权。"""
    # 鉴权：WebSocket 无法用 Header 依赖，令牌经查询串传入（?token=xxx）。
    # 日志可能含敏感信息（手机号/异常堆栈等），公网映射场景必须校验。
    from webui import log_stream
    from webui.auth import DEV_NO_AUTH, _verify_token
    token = ws.query_params.get("token", "")
    if not DEV_NO_AUTH and not _verify_token(token):
        await ws.close(code=1008)  # Policy Violation
        return
    await ws.accept()
    # 先取历史快照，再订阅：避免「快照」与「新增队列」之间的窗口造成重复推送
    history = log_stream.recent_logs()
    q = log_stream.subscribe()
    try:
        for item in history:
            await ws.send_json(item)
        # 再持续推送
        while True:
            item = await q.get()
            await ws.send_json(item)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        log_stream.unsubscribe(q)


# 挂载静态资源（前端构建产物）
class ImmutableStaticFiles(StaticFiles):
    """构建资源带内容指纹，可以安全使用长期缓存。"""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


@app.on_event("startup")
async def _mount_static():
    # 记录事件循环并安装日志流 handler
    import asyncio
    from webui import log_stream
    log_stream.set_loop(asyncio.get_running_loop())
    log_stream.install()

    assets = STATIC_DIR / "assets"
    if assets.exists():
        app.mount("/assets", ImmutableStaticFiles(directory=str(assets)), name="assets")


async def start_web_ui(host: str = "0.0.0.0", port: int = 8000):
    """启动 Web UI 服务"""
    import asyncio
    import uvicorn

    try:
        cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(cfg)
        logger.info("本地 Web 服务启动于 http://%s:%s", host, port)
        await server.serve()
        if not getattr(server, "started", False):
            logger.error("Web 服务启动失败: 端口 %s 可能被占用", port)
            while True:
                await asyncio.sleep(360)
    except OSError as e:
        logger.error("Web 服务启动失败（端口 %s 可能被占用）：%s", port, e)
        while True:
            await asyncio.sleep(3600)
