"""
libs/proxy.py
平台代理的统一出口。

背景：`proxy_set`（系统设置里的代理）过去被各子系统各自读取、各自手动传给 httpx
（Telegram 客户端 / pip / AI / GitHub 导入），插件自己发的 HTTP 请求则完全不走代理。
本模块把「读取代理 URL」收敛成一处，并提供 `export_env()`——启动时导出标准代理环境变量，
让 httpx / requests / aiohttp（trust_env 默认开启）自动走代理，插件无需任何改动。
"""
from __future__ import annotations

import os


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
            scheme = px.get("scheme", "http")
            user, pwd = px.get("username", ""), px.get("password", "")
            auth = f"{user}:{pwd}@" if user else ""
            return f"{scheme}://{auth}{host}:{port}"
    except Exception:  # noqa: BLE001
        pass
    return None


# 本地地址不走代理，避免插件访问平台自身/本机服务时被代理拦截
_NO_PROXY = "localhost,127.0.0.1,::1"


def export_env() -> str | None:
    """把平台代理导出为标准环境变量（大小写各一套，兼容 httpx/requests/aiohttp）。

    启用时设置 HTTP(S)_PROXY / ALL_PROXY / NO_PROXY 并返回代理 URL；
    未启用时清除这些变量并返回 None。可在启动时或代理设置变更后调用（幂等）。
    """
    url = proxy_url()
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    if url:
        for k in keys:
            os.environ[k] = url
        os.environ["NO_PROXY"] = _NO_PROXY
        os.environ["no_proxy"] = _NO_PROXY
    else:
        for k in (*keys, "NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)
    return url
