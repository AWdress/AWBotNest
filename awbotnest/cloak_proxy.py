from __future__ import annotations

import functools
import importlib
import inspect
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .config import Settings


logger = logging.getLogger("awbotnest.cloak")

_settings: Settings | None = None
_USE_PLATFORM_PROXY = object()
_request_proxy: ContextVar[object | str | dict[str, object] | None] = ContextVar(
    "awbotnest_cloak_request_proxy", default=_USE_PLATFORM_PROXY,
)
_LAUNCH_FUNCTIONS = (
    "launch",
    "launch_async",
    "launch_context",
    "launch_context_async",
    "launch_persistent_context",
    "launch_persistent_context_async",
)
_HTTPX_REQUEST_FUNCTIONS = {
    "delete", "get", "head", "options", "patch", "post", "put", "request", "stream",
}


def requirements_use_cloakbrowser(requirements: Iterable[str]) -> bool:
    """Return whether a validated dependency list declares CloakBrowser."""
    return any(canonicalize_name(Requirement(item).name) == "cloakbrowser" for item in requirements)


def _platform_proxy() -> str | None:
    value = str(_settings.proxy_url if _settings is not None else "").strip()
    return value or None


def _proxy_url(value: str | dict[str, object] | None) -> str | None:
    if not isinstance(value, dict):
        return str(value).strip() if value else None
    server = str(value.get("server") or "").strip()
    if not server:
        return None
    if "://" not in server:
        server = f"http://{server}"
    username = str(value.get("username") or "")
    if not username:
        return server
    parsed = urlsplit(server)
    password = str(value.get("password") or "")
    credentials = quote(username, safe="")
    if password:
        credentials += f":{quote(password, safe='')}"
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{credentials}@{host}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _effective_request_proxy() -> str | None:
    value = _request_proxy.get()
    return _platform_proxy() if value is _USE_PLATFORM_PROXY else _proxy_url(value)


def _inherit_launch_proxy(kwargs: dict[str, Any]) -> tuple[dict[str, Any], str | dict[str, object] | None]:
    resolved = dict(kwargs)
    if "proxy" not in resolved:
        proxy = _platform_proxy()
        if proxy:
            resolved["proxy"] = proxy
    elif resolved["proxy"] is False or resolved["proxy"] == "":
        # CloakBrowser itself accepts None as direct mode. False/empty are also
        # normalized here so plugins have an explicit, readable opt-out.
        resolved["proxy"] = None
    return resolved, resolved.get("proxy")


def _wrap_launch(function: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(function, "__awbotnest_cloak_proxy__", False):
        return function
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved, request_proxy = _inherit_launch_proxy(kwargs)
            token = _request_proxy.set(request_proxy)
            try:
                return await function(*args, **resolved)
            finally:
                _request_proxy.reset(token)

        wrapper = async_wrapper
    else:
        @functools.wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved, request_proxy = _inherit_launch_proxy(kwargs)
            token = _request_proxy.set(request_proxy)
            try:
                return function(*args, **resolved)
            finally:
                _request_proxy.reset(token)

        wrapper = sync_wrapper
    setattr(wrapper, "__awbotnest_cloak_proxy__", True)
    return wrapper


class _ProxyingHttpx:
    """Module facade that adds the live platform proxy to Cloak HTTP calls only."""

    __awbotnest_cloak_httpx__ = True

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._delegate, name)
        if name not in _HTTPX_REQUEST_FUNCTIONS or not callable(value):
            return value

        @functools.wraps(value)
        def request_with_proxy(*args: Any, **kwargs: Any) -> Any:
            if "proxy" not in kwargs:
                proxy = _effective_request_proxy()
                if proxy:
                    kwargs["proxy"] = proxy
            return value(*args, **kwargs)

        return request_with_proxy


def _patch_httpx(module: Any) -> None:
    current = getattr(module, "httpx", None)
    if current is not None and not getattr(current, "__awbotnest_cloak_httpx__", False):
        module.httpx = _ProxyingHttpx(current)


def _patch_geoip_download(module: Any) -> None:
    original = getattr(module, "_download_geoip_db", None)
    if not callable(original) or getattr(original, "__awbotnest_cloak_proxy__", False):
        return

    @functools.wraps(original)
    def download_geoip(dest: Path) -> None:
        proxy = _effective_request_proxy()
        if not proxy:
            original(dest)
            return

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with httpx.stream(
                "GET",
                module.GEOIP_DB_URL,
                follow_redirects=True,
                timeout=300.0,
                proxy=proxy,
            ) as response:
                response.raise_for_status()
                with os.fdopen(file_descriptor, "wb") as stream:
                    file_descriptor = -1
                    for chunk in response.iter_bytes(chunk_size=65_536):
                        stream.write(chunk)
            os.replace(temp_path, dest)
            logger.info("CloakBrowser GeoIP 数据库已通过平台代理下载")
        except BaseException:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            temp_path.unlink(missing_ok=True)
            raise

    setattr(download_geoip, "__awbotnest_cloak_proxy__", True)
    module._download_geoip_db = download_geoip


def configure_cloakbrowser(settings: Settings) -> None:
    """Make plugin-owned CloakBrowser calls inherit the live platform proxy.

    The patch is deliberately limited to CloakBrowser modules. It does not put
    proxy credentials in process-wide environment variables and never retries
    a failed proxied request through a direct connection.
    """
    global _settings
    _settings = settings

    package = importlib.import_module("cloakbrowser")
    browser_module = importlib.import_module("cloakbrowser.browser")
    for module in (package, browser_module):
        for name in _LAUNCH_FUNCTIONS:
            function = getattr(module, name, None)
            if callable(function):
                setattr(module, name, _wrap_launch(function))

    for name in ("cloakbrowser.download", "cloakbrowser.license"):
        _patch_httpx(importlib.import_module(name))
    _patch_geoip_download(importlib.import_module("cloakbrowser.geoip"))

    logger.info("CloakBrowser 已接入平台代理（插件显式 proxy 参数优先）")
