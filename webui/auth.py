"""
webui/auth.py
Web 控制台鉴权 —— 用户名 + 密码登录。

- 首次运行自动初始化默认凭据：admin / password（存 data/auth.json）。
- 登录校验用户名+密码 → 签发令牌（HMAC(secret, username:pwd_hash)，
  无状态、重启不失效、改用户名或密码后自动失效）。
- 之后请求带 Authorization: Bearer <token>，由 require_auth 校验。
- 用户名/密码在「系统设置」页修改。

环境变量 AWBOTNEST_DEV_NO_AUTH=true 时全程放行（仅本地开发用）。
"""
from __future__ import annotations

import os
import json
import hmac
import hashlib
import secrets
from pathlib import Path

from libs.log import logger

from fastapi import HTTPException, Header, Cookie, Response

_AUTH_FILE = Path("data") / "auth.json"   # {"username","salt","pwd_hash","secret"}
_PBKDF_ROUNDS = 200_000

DEV_NO_AUTH = os.getenv("AWBOTNEST_DEV_NO_AUTH", "false").lower() == "true"

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "password"


def _hash_pwd(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF_ROUNDS).hex()


def _load() -> dict:
    if _AUTH_FILE.exists():
        try:
            return json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(data: dict) -> None:
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_default() -> dict:
    """首次运行或旧格式缺字段：写入默认 admin/password"""
    data = _load()
    if not data.get("pwd_hash") or not data.get("username") or not data.get("secret"):
        salt = secrets.token_hex(16)
        data = {
            "username": DEFAULT_USERNAME,
            "salt": salt,
            "pwd_hash": _hash_pwd(DEFAULT_PASSWORD, salt),
            "secret": secrets.token_hex(32),
        }
        _save(data)
        logger.warning(
            "已生成默认控制台账号：用户名=%s 密码=%s（请登录后在「系统设置」尽快修改）",
            DEFAULT_USERNAME, DEFAULT_PASSWORD,
        )
    return data


def get_username() -> str:
    return _ensure_default().get("username", DEFAULT_USERNAME)


def _make_token(data: dict) -> str:
    """令牌 = HMAC(secret, username:pwd_hash)。改用户名/密码后自动失效。"""
    msg = f"{data['username']}:{data['pwd_hash']}"
    return hmac.new(data["secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()


def login(username: str, password: str) -> str:
    """校验用户名+密码，返回令牌"""
    data = _ensure_default()
    # 转 bytes 再比：hmac.compare_digest 对含非 ASCII 的 str 会抛 TypeError，
    # 直接比 str 会让「用户名含中文/非 ASCII」变成 500 而非干脆 401。
    user_ok = hmac.compare_digest((username or "").strip().encode("utf-8"),
                                  (data["username"] or "").encode("utf-8"))
    pwd_ok = hmac.compare_digest(_hash_pwd(password or "", data["salt"]), data["pwd_hash"])
    if not (user_ok and pwd_ok):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return _make_token(data)


def change_credentials(old_password: str, new_username: str, new_password: str) -> None:
    """修改用户名/密码（需校验旧密码）。新密码留空则只改用户名。"""
    data = _ensure_default()
    if not hmac.compare_digest(_hash_pwd(old_password or "", data["salt"]), data["pwd_hash"]):
        raise HTTPException(status_code=403, detail="当前密码不正确")
    new_username = (new_username or "").strip() or data["username"]
    if new_password:
        if len(new_password) < 4:
            raise HTTPException(status_code=400, detail="新密码至少 4 位")
        salt = secrets.token_hex(16)
        data["salt"] = salt
        data["pwd_hash"] = _hash_pwd(new_password, salt)
    data["username"] = new_username
    _save(data)


def _verify_token(token: str) -> bool:
    data = _load()
    if not data.get("pwd_hash"):
        return False
    return hmac.compare_digest(token, _make_token(data))


def is_default_password() -> bool:
    """当前密码是否仍是出厂默认 password（用当前 salt 比对默认口令哈希）。"""
    data = _load()
    if not data.get("pwd_hash") or not data.get("salt"):
        return True
    return hmac.compare_digest(_hash_pwd(DEFAULT_PASSWORD, data["salt"]), data["pwd_hash"])


async def require_auth(authorization: str = Header(default="")):
    """FastAPI 依赖：校验 Bearer 令牌。DEV_NO_AUTH 时放行。"""
    if DEV_NO_AUTH:
        return {"dev": True}
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not _verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return {"auth": True}


async def require_password_changed(authorization: str = Header(default="")):
    """更严格的依赖：在 require_auth 基础上，若仍是默认密码则拒绝。
    用于高危写操作（上传/导入/启用/配置/设置），强制首次改密后才能用。
    DEV_NO_AUTH 时仍放行（本地开发）。"""
    if DEV_NO_AUTH:
        return {"dev": True}
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not _verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    if is_default_password():
        # 428 Precondition Required：前端据此引导强制改密
        raise HTTPException(status_code=428, detail="请先修改默认密码后再操作")
    return {"auth": True}


# ──────────────────────────────────────────────
# 资源 Cookie：给「静态资源类」请求鉴权
#
# 插件 Vue 模式的前端产物由浏览器 ESM 动态 import 加载，import 无法附带
# Authorization 头，故沿用 MoviePilot 的思路——登录时下发一个资源 Cookie，
# 同源请求浏览器会自动带上，用它给 /api/plugins/<id>/fe/ 这类静态资源鉴权。
# Cookie 值即登录令牌（改用户名/密码后自动失效），HttpOnly 使 JS 读不到。
# ──────────────────────────────────────────────
RESOURCE_COOKIE = "awbotnest_resource"
# 仅发往插件相关路径，缩小暴露面（/fe/ 静态资源都在此前缀下）
_RESOURCE_COOKIE_PATH = "/api/plugins"


def current_resource_token() -> str:
    """当前有效的资源令牌（= 登录令牌）。"""
    return _make_token(_ensure_default())


def set_resource_cookie(response: Response) -> None:
    """在响应上种下资源 Cookie（会话级：浏览器关闭即失效，刷新仍在）。"""
    response.set_cookie(
        RESOURCE_COOKIE, current_resource_token(),
        httponly=True, samesite="lax", path=_RESOURCE_COOKIE_PATH,
    )


def clear_resource_cookie(response: Response) -> None:
    response.delete_cookie(RESOURCE_COOKIE, path=_RESOURCE_COOKIE_PATH)


async def require_resource_access(
    resource: str = Cookie(default="", alias=RESOURCE_COOKIE),
):
    """FastAPI 依赖：校验资源 Cookie。DEV_NO_AUTH 时放行。
    校验失败返回 403（前端 import 会失败并提示，不做跳登录处理）。"""
    if DEV_NO_AUTH:
        return {"dev": True}
    if not _verify_token(resource):
        raise HTTPException(status_code=403, detail="无权访问插件资源（请重新登录）")
    return {"auth": True}
