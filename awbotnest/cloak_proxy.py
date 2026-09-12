from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import logging
import os
import sys
import tempfile
import threading
import time
from collections import deque
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
_launch_depth: ContextVar[int] = ContextVar("awbotnest_cloak_launch_depth", default=0)
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


class _SessionLease:
    def __init__(self, gate: "_FreeSessionGate") -> None:
        self.gate = gate
        self.released = False
        self._lock = threading.Lock()

    def release(self, *, abnormal: bool = False) -> None:
        with self._lock:
            if self.released:
                return
            self.released = True
        self.gate.release(abnormal=abnormal)


class _FreeSessionGate:
    """Serialize keyed CloakBrowser sessions across all plugins in this process."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queue: deque[object] = deque()
        self._active = False
        self._maintenance = False
        self._cooldown_until = 0.0
        self._recovery_running = False

    def acquire(self, cancel: threading.Event | None = None,
                timeout: float = 1800.0) -> _SessionLease | None:
        started = time.monotonic()
        ticket = object()
        with self._condition:
            if self._maintenance:
                raise RuntimeError("CloakBrowser 正在更新，请等待平台重启")
            if not _platform_license_key():
                return None
            self._queue.append(ticket)
            while True:
                if cancel is not None and cancel.is_set():
                    self._remove(ticket)
                    return None
                if self._maintenance:
                    self._remove(ticket)
                    raise RuntimeError("CloakBrowser 正在更新，请等待平台重启")
                now = time.monotonic()
                if self._queue and self._queue[0] is ticket and not self._active \
                        and now >= self._cooldown_until:
                    self._queue.popleft()
                    self._active = True
                    waited = now - started
                    if waited >= 0.1:
                        logger.info("CloakBrowser 免费会话排队完成（等待 %.1f 秒）", waited)
                    return _SessionLease(self)
                remaining = timeout - (now - started)
                if remaining <= 0:
                    self._remove(ticket)
                    raise TimeoutError("等待 CloakBrowser 免费会话超时")
                cooldown = max(0.0, self._cooldown_until - now)
                self._condition.wait(timeout=min(0.25, remaining, cooldown or 0.25))

    def _remove(self, ticket: object) -> None:
        try:
            self._queue.remove(ticket)
        except ValueError:
            pass
        self._condition.notify_all()

    def release(self, *, abnormal: bool = False) -> None:
        key = _platform_license_key()
        with self._condition:
            self._active = False
            if abnormal and key:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + 300)
                if not self._recovery_running:
                    self._recovery_running = True
                    threading.Thread(
                        target=self._recover_remote_seat,
                        args=(key,),
                        name="cloak-seat-recovery",
                        daemon=True,
                    ).start()
                logger.warning("CloakBrowser 会话异常结束，等待服务端释放免费席位")
            self._condition.notify_all()

    def _recover_remote_seat(self, key: str) -> None:
        try:
            for delay in (3, 5, 10, 15, 30, 45, 60, 90):
                time.sleep(delay)
                try:
                    from cloakbrowser.license import get_session_seats
                    seats = get_session_seats(key)
                    if seats.state == "ok" and seats.active == 0:
                        with self._condition:
                            self._cooldown_until = 0.0
                            self._condition.notify_all()
                        logger.info("CloakBrowser 服务端免费席位已释放")
                        return
                except Exception:
                    continue
        finally:
            with self._condition:
                self._recovery_running = False
                self._condition.notify_all()

    def begin_maintenance(self) -> None:
        with self._condition:
            if self._active or self._queue:
                raise RuntimeError("仍有 CloakBrowser 会话正在运行或排队，请稍后重试")
            self._maintenance = True
            self._condition.notify_all()

    def cancel_maintenance(self) -> None:
        with self._condition:
            self._maintenance = False
            self._condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "queue_enabled": bool(_platform_license_key()),
                "active": self._active,
                "waiting": len(self._queue),
                "maintenance": self._maintenance,
                "cooldown_seconds": max(0, round(self._cooldown_until - time.monotonic())),
            }


_SESSION_GATE = _FreeSessionGate()


def requirements_use_cloakbrowser(requirements: Iterable[str]) -> bool:
    """Return whether a validated dependency list declares CloakBrowser."""
    return any(canonicalize_name(Requirement(item).name) == "cloakbrowser" for item in requirements)


def bind_cloakbrowser_settings(settings: Settings) -> None:
    """Bind live settings without importing the optional CloakBrowser package."""
    global _settings
    _settings = settings


def cloak_session_status() -> dict[str, object]:
    return _SESSION_GATE.snapshot()


def begin_cloak_update() -> None:
    _SESSION_GATE.begin_maintenance()


def cancel_cloak_update() -> None:
    _SESSION_GATE.cancel_maintenance()


def reset_cloakbrowser_runtime() -> None:
    """Finish a hot reload and force the next import to use updated files."""
    cancel_cloak_update()
    unload_cloakbrowser_modules()


def unload_cloakbrowser_modules() -> None:
    """Discard imported CloakBrowser modules without changing maintenance state."""
    for name in tuple(sys.modules):
        if name == "cloakbrowser" or name.startswith("cloakbrowser."):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _platform_proxy() -> str | None:
    value = str(_settings.proxy_url if _settings is not None else "").strip()
    return value or None


def _platform_license_key() -> str | None:
    enabled = bool(
        getattr(_settings, "cloakbrowser_use_free_key", False)
        if _settings is not None else False
    )
    if not enabled:
        return None
    value = str(getattr(_settings, "cloakbrowser_license_key", "") if _settings is not None else "").strip()
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
    if "license_key" not in resolved:
        license_key = _platform_license_key()
        if license_key:
            resolved["license_key"] = license_key
    if "proxy" not in resolved:
        proxy = _platform_proxy()
        if proxy:
            resolved["proxy"] = proxy
    elif resolved["proxy"] is False or resolved["proxy"] == "":
        # CloakBrowser itself accepts None as direct mode. False/empty are also
        # normalized here so plugins have an explicit, readable opt-out.
        resolved["proxy"] = None
    return resolved, resolved.get("proxy")


async def _acquire_session_async() -> _SessionLease | None:
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_SESSION_GATE.acquire, cancel))
    try:
        return await task
    except asyncio.CancelledError:
        cancel.set()
        lease = await asyncio.shield(task)
        if lease is not None:
            lease.release()
        raise


def _attach_session_lease(target: Any, lease: _SessionLease | None) -> Any:
    if lease is None:
        return target
    close = getattr(target, "close", None)
    if not callable(close):
        lease.release(abnormal=True)
        raise RuntimeError("CloakBrowser 返回对象缺少 close()，无法管理免费会话")
    state = {"closing": False}
    if inspect.iscoroutinefunction(close):
        @functools.wraps(close)
        async def async_close(*args: Any, **kwargs: Any) -> Any:
            state["closing"] = True
            try:
                result = await close(*args, **kwargs)
            except BaseException:
                lease.release(abnormal=True)
                raise
            else:
                lease.release()
                return result

        target.close = async_close
    else:
        @functools.wraps(close)
        def sync_close(*args: Any, **kwargs: Any) -> Any:
            state["closing"] = True
            try:
                result = close(*args, **kwargs)
            except BaseException:
                lease.release(abnormal=True)
                raise
            else:
                lease.release()
                return result

        target.close = sync_close

    on = getattr(target, "on", None)
    if callable(on):
        event = "close" if "context" in type(target).__name__.lower() else "disconnected"
        try:
            on(event, lambda *_: None if state["closing"] else lease.release(abnormal=True))
        except Exception:
            pass
    return target


def _launch_failure_may_hold_seat(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return ("license" in name or "targetclosed" in name or
            "session limit" in message or "browser has been closed" in message)


def _wrap_launch(function: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(function, "__awbotnest_cloak_proxy__", False):
        return function
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved, request_proxy = _inherit_launch_proxy(kwargs)
            outermost = _launch_depth.get() == 0
            lease = await _acquire_session_async() if outermost else None
            proxy_token = _request_proxy.set(request_proxy)
            depth_token = _launch_depth.set(_launch_depth.get() + 1)
            try:
                result = await function(*args, **resolved)
                return _attach_session_lease(result, lease)
            except BaseException as exc:
                if lease is not None:
                    lease.release(abnormal=_launch_failure_may_hold_seat(exc))
                raise
            finally:
                _launch_depth.reset(depth_token)
                _request_proxy.reset(proxy_token)

        wrapper = async_wrapper
    else:
        @functools.wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved, request_proxy = _inherit_launch_proxy(kwargs)
            outermost = _launch_depth.get() == 0
            lease = _SESSION_GATE.acquire() if outermost else None
            proxy_token = _request_proxy.set(request_proxy)
            depth_token = _launch_depth.set(_launch_depth.get() + 1)
            try:
                result = function(*args, **resolved)
                return _attach_session_lease(result, lease)
            except BaseException as exc:
                if lease is not None:
                    lease.release(abnormal=_launch_failure_may_hold_seat(exc))
                raise
            finally:
                _launch_depth.reset(depth_token)
                _request_proxy.reset(proxy_token)

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
    bind_cloakbrowser_settings(settings)

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

    logger.info("CloakBrowser 已接入平台浏览器设置（插件显式参数优先）")
