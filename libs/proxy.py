"""
libs/proxy.py
平台代理的统一出口。

背景：`proxy_set`（系统设置里的代理）过去被各子系统各自读取、各自手动传给 httpx
（Telegram 客户端 / pip / AI / GitHub 导入），插件自己发的 HTTP 请求则完全不走代理。
本模块把「读取代理 URL」收敛成一处，并提供 `export_env()`——启动时导出标准代理环境变量，
让 httpx / requests 自动走代理。aiohttp 默认不读取环境代理，使用它的代码仍需显式开启
`trust_env=True`；平台提供的 Telegram、AI、GitHub、浏览器和依赖下载链路会统一使用此配置。
"""
from __future__ import annotations

import os


_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)
_NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")
_ORIGINAL_PROXY_ENV = {
    key: os.environ.get(key)
    for key in (*_PROXY_ENV_KEYS, *_NO_PROXY_ENV_KEYS)
}


def display_proxy_url(url: str | None) -> str:
    """返回可安全写入日志的代理地址，不包含用户名、密码或路径。"""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}" if parsed.scheme and host else "已配置"
    except (TypeError, ValueError):
        return "已配置"


def proxy_url() -> str | None:
    """读取平台代理，启用时返回 httpx/requests 可用的代理 URL；未启用或未配置返回 None。

    优先用 PROXY_URL 整串，否则按 proxy 子项（scheme/hostname/port/username/password）拼接。
    """
    try:
        import config.config as _cfg
        _cfg.reload()
        ps = getattr(_cfg, "proxy_set", {}) or {}
        if not ps.get("proxy_enable"):
            return None
        url = (ps.get("PROXY_URL") or "").strip()
        if url:
            return url
        px = ps.get("proxy", {}) or {}
        host, port = px.get("hostname"), px.get("port")
        if host and port:
            from urllib.parse import quote
            scheme = px.get("scheme", "http")
            user = px.get("username") or ""
            pwd = px.get("password") or ""
            # 用户名/密码可能含 @ : / # 等，需转义，否则拼进 URL 会破坏解析
            auth = f"{quote(str(user), safe='')}:{quote(str(pwd), safe='')}@" if user else ""
            return f"{scheme}://{auth}{host}:{port}"
    except Exception:  # noqa: BLE001
        pass
    return None


# 本地地址不走代理，避免插件访问平台自身/本机服务时被代理拦截
_NO_PROXY = "localhost,127.0.0.1,::1"


def export_env() -> str | None:
    """把平台代理导出为标准环境变量（大小写各一套，兼容 httpx/requests）。

    启用时设置 HTTP(S)_PROXY / ALL_PROXY / NO_PROXY 并返回代理 URL；
    未启用时恢复容器启动前已有的代理变量。可在启动时或代理设置变更后调用（幂等）。
    """
    url = proxy_url()
    if url:
        for k in _PROXY_ENV_KEYS:
            os.environ[k] = url
        original_no_proxy = next(
            (
                _ORIGINAL_PROXY_ENV[key]
                for key in _NO_PROXY_ENV_KEYS
                if _ORIGINAL_PROXY_ENV[key]
            ),
            "",
        )
        no_proxy = ",".join(
            item for item in (_NO_PROXY, original_no_proxy) if item
        )
        for k in _NO_PROXY_ENV_KEYS:
            os.environ[k] = no_proxy
    else:
        for key, original in _ORIGINAL_PROXY_ENV.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
    return url
