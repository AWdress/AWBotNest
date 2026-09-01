from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

bearer = HTTPBearer(auto_error=False)


def admin_dependency(settings: Settings):
    async def require_admin(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> None:
        supplied = credentials.credentials if credentials else ""
        admin_ok = bool(supplied) and hmac.compare_digest(supplied, settings.admin_token)
        if not admin_ok:
            raise HTTPException(status_code=401, detail="管理令牌无效")

    return require_admin


def api_key_dependency(settings: Settings):
    async def require_api_key(
        x_api_key: str = Header(default="", alias="X-API-Key"),
        api_key: str = Header(default="", alias="Api-Key"),
    ) -> None:
        supplied = x_api_key or api_key
        if not settings.api_key:
            raise HTTPException(status_code=503, detail="开放平台 API Key 尚未配置")
        if not supplied or not hmac.compare_digest(supplied, settings.api_key):
            raise HTTPException(status_code=401, detail="API Key 无效或缺失")

    return require_api_key
