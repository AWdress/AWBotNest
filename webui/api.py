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
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from core import config, logger
from webui import auth as _authmod
from webui.auth import require_auth as _auth
from webui.auth import require_password_changed as _auth_pwc
from kernel.registry import registry

app = FastAPI(title="AWBotNest Platform API")
APP_VERSION = "1.0.7"

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
async def auth_login(body: Dict[str, Any]):
    """登录（用户名 + 密码），返回令牌"""
    b = body or {}
    token = _authmod.login(b.get("username", ""), b.get("password", ""))
    return {"status": "success", "token": token}


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
        return FileResponse(str(idx))
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
    for m in metas:
        m.loaded = runtime.is_loaded(m.id)
    return {"plugins": [m.to_dict() for m in metas]}


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
    logger.info("插件已上传: %s", filename)
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
    列出 GitHub 来源中的插件。body: {source, token?}
    返回 {source_type, plugins:[{id,name,version,author,description,icon,is_folder,...}]}
    优先读仓库 manifest.json（插件市场），无清单则目录扫描。
    """
    from webui import github_import
    source = (body.get("source") or "").strip()
    token = body.get("token") or None
    if not source:
        raise HTTPException(status_code=400, detail="请填写 GitHub 仓库地址或链接")
    try:
        result = await github_import.list_plugins(source, token)
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
    下载并保存选定插件。body: {plugins: [<list返回的plugin对象>], token?}
    单文件与文件夹插件都支持；仅落盘 + 静态校验，不自动启用。
    """
    from webui import github_import
    plugins = body.get("plugins") or []
    token = body.get("token") or None
    if not isinstance(plugins, list) or not plugins:
        raise HTTPException(status_code=400, detail="未选择任何插件")

    imported = []
    for plugin in plugins:
        pid = plugin.get("id")
        if not pid or "/" in pid or "\\" in pid or pid.startswith("_"):
            raise HTTPException(status_code=400, detail=f"非法插件 id: {pid}")
        try:
            files = await github_import.resolve_files(plugin, token)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"解析 {pid} 文件失败: {e}")
        if not files:
            raise HTTPException(status_code=502, detail=f"插件 {pid} 没有可下载文件")

        for f in files:
            target = _safe_target(f["target"])
            try:
                content = await github_import.fetch_file(f["download_url"], token)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"下载 {target} 失败: {e}")
            dest = PLUGINS_DIR / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

        meta = registry.get_meta(pid)
        if meta and meta.error:
            logger.warning("GitHub 导入的插件元数据有问题 [%s]: %s", pid, meta.error)
        imported.append(meta.to_dict() if meta else {"id": pid, "error": "导入后未找到入口"})
        logger.info("已从 GitHub 导入插件: %s（%d 个文件）", pid, len(files))
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
    logger.info("插件已删除: %s", plugin_id)
    return {"status": "success"}


@app.get("/api/plugins/{plugin_id}/config")
async def get_plugin_config(plugin_id: str, user=Depends(_auth)):
    """读取插件配置（schema + 当前值）"""
    meta = registry.get_meta(plugin_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    return {"schema": meta.config_schema, "values": registry.get_config(plugin_id)}


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
    """推送路由总览：可选 Bot 列表 + 每个插件当前推送到哪个 Bot（空=默认 Bot）。"""
    accounts = _get_accounts()
    bots = await accounts.list_bots()
    plugins = [
        {"id": m.id, "name": m.name, "scope": m.scope,
         "bot": registry.get_bot_choice(m.id), "error": bool(m.error)}
        for m in registry.scan()
    ]
    return {"bots": bots, "plugins": plugins}


@app.put("/api/bots/routing")
async def set_bots_routing(body: Dict[str, Any], user=Depends(_auth_pwc)):
    """设置某个插件推送到哪个 Bot（bot_id 空/"default"=默认 Bot）。
    影响通知推送与 scope=bot/both 插件的 handler 挂载，已加载则重载重挂。"""
    plugin_id = str(body.get("plugin_id") or "").strip()
    bot_id = str(body.get("bot_id") or "").strip()
    if not plugin_id:
        raise HTTPException(status_code=400, detail="缺少 plugin_id")
    if registry.get_meta(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    # 校验 bot_id 真实存在（默认 Bot 恒合法；空=默认）
    if bot_id and bot_id != "default":
        accounts = _get_accounts()
        valid = {b["id"] for b in await accounts.list_bots()}
        if bot_id not in valid:
            raise HTTPException(status_code=400, detail="指定的 Bot 不存在")
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
_SECRET_FIELDS = ("API_HASH", "BOT_TOKEN")
_MASK = "********"


def _mask(val: str) -> str:
    if not val:
        return ""
    # 敏感字段一律全打码，不回显任何明文片段
    return _MASK


@app.get("/api/settings")
async def get_settings_api(user=Depends(_auth)):
    """读取平台设置（敏感字段打码）"""
    import config.config as cfg
    data = cfg.load()
    out = dict(data)
    for f in _SECRET_FIELDS:
        out[f] = _mask(data.get(f, ""))
    # proxy 密码打码
    try:
        if out.get("proxy_set", {}).get("proxy", {}).get("password"):
            out["proxy_set"]["proxy"]["password"] = _MASK
    except Exception:  # noqa: BLE001
        pass
    # DB 密码打码
    try:
        if out.get("DB_INFO", {}).get("password"):
            out["DB_INFO"]["password"] = _MASK
    except Exception:  # noqa: BLE001
        pass
    # 插件仓库各自的 token 打码
    try:
        for r in out.get("PLUGIN_REPOS", []) or []:
            if isinstance(r, dict) and r.get("token"):
                r["token"] = _MASK
    except Exception:  # noqa: BLE001
        pass
    # 额外 Bot 各自的 token 打码
    try:
        for b in out.get("BOTS", []) or []:
            if isinstance(b, dict) and b.get("token"):
                b["token"] = _MASK
    except Exception:  # noqa: BLE001
        pass
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
        # 插件仓库：打码 token 按 url 匹配原值保留
        if k == "PLUGIN_REPOS" and isinstance(v, list):
            old_tokens = {r.get("url"): r.get("token", "")
                          for r in (current.get("PLUGIN_REPOS") or []) if isinstance(r, dict)}
            for r in v:
                if isinstance(r, dict) and (r.get("token") == _MASK or (isinstance(r.get("token"), str) and _MASK in r.get("token", ""))):
                    r["token"] = old_tokens.get(r.get("url"), "")
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

    cfg.save(merged)
    logger.info("平台设置已更新（config.json）")

    # 额外 Bot 列表变更：被删除的 Bot 若有插件推送路由指向它，回退默认 Bot 并重挂已加载插件
    if "BOTS" in incoming:
        try:
            old_ids = {b.get("id") for b in (current.get("BOTS") or []) if isinstance(b, dict)}
            new_ids = {b.get("id") for b in (merged.get("BOTS") or []) if isinstance(b, dict)}
            removed = [bid for bid in old_ids - new_ids if bid]
            affected: set[str] = set()
            for bid in removed:
                affected.update(registry.purge_bot(bid))
            if affected:
                from kernel import state as kernel_state
                runtime = kernel_state.runtime
                if runtime is not None:
                    for pid in affected:
                        if runtime.is_loaded(pid):
                            await runtime.reload(pid)
                logger.info("已删除 %d 个 Bot，%d 个插件推送路由回退默认 Bot", len(removed), len(affected))
        except Exception as e:  # noqa: BLE001
            logger.warning("处理 Bot 删除的推送路由回退失败: %r", e)

    # 代理变更 → 立即刷新进程环境变量，新启动/重载的插件即时生效（长连接旧客户端仍需重启）
    if "proxy_set" in incoming:
        try:
            from libs.proxy import export_env
            export_env()
        except Exception as e:  # noqa: BLE001
            logger.warning("刷新代理环境变量失败: %r", e)

    # 插件仓库相关设置变更 → 重排轮询任务（即时生效，无需重启）
    if any(k in incoming for k in ("PLUGIN_REPO_ENABLE", "PLUGIN_REPOS", "PLUGIN_REPO_INTERVAL")):
        try:
            from webui import repo_sync
            repo_sync.reschedule()
        except Exception as e:  # noqa: BLE001
            logger.warning("重排插件仓库轮询失败: %r", e)

    # 凭据/代理/DB 变更需重启才能完全生效
    return {"status": "success", "restart_required": True}


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
        from schedulers import scheduler as _sched
        for j in _sched.get_jobs():
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
@app.on_event("startup")
async def _mount_static():
    # 记录事件循环并安装日志流 handler
    import asyncio
    from webui import log_stream
    log_stream.set_loop(asyncio.get_running_loop())
    log_stream.install()

    assets = STATIC_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")


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
        logger.error("Web 服务 OSError: %s（端口 %s 可能被占用）", e, port)
        while True:
            await asyncio.sleep(3600)
