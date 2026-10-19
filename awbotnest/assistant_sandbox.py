"""Confirmed requests to a separately deployed, untrusted execution broker.

This module never executes code, opens workspace files, copies system files, or
uses the system proxy. A configured broker is a separate trust boundary; the
execution runner must not mount system data, credentials, sessions or the Docker
socket. Only the separately deployed broker holds Docker management access.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import ipaddress
import json
import math
import re
import time
import weakref
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

MAX_RESULT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 20 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_OPERATIONS = {"python", "shell", "read_file", "write_file", "list_files", "browser"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def validate_endpoint(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1000
            or any(ord(char) < 33 or ord(char) > 126 for char in value) or "\\" in value):
        raise ValueError("隔离执行服务地址不正确")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("隔离执行服务地址不正确") from None
    if (parsed.scheme not in {"http", "https"} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or port is not None and not 1 <= port <= 65535):
        raise ValueError("执行服务地址不能包含账号、路径、查询参数或片段")
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback or bool(getattr(address, "ipv4_mapped", None)
                                               and address.ipv4_mapped.is_loopback)
    except ValueError:
        loopback = host.lower() == "localhost"
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise ValueError("隔离执行服务主机名不正确") from None
    if parsed.scheme == "http" and not loopback and host.lower() != "assistant-sandbox":
        raise ValueError("远程执行服务必须使用 HTTPS；HTTP 仅用于本机或 assistant-sandbox")
    return value.rstrip("/")


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value) or len(set(value)) < 10:
        raise ValueError("执行服务 Token 必须是 32 至 256 位随机字母、数字或下划线、短横线")
    return value


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("隔离执行会话或请求编号不正确")
    return value


def _text(value, maximum, label, *, empty=False):
    if (not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum
            or "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError(f"{label}格式不正确或超过长度限制")
    return value


def _path(value, *, directory=False):
    value = _text(value, 240, "工作目录内的相对路径")
    if ("\\" in value or ":" in value or value.startswith("/")
            or len(value.encode("utf-8")) > 240
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("文件路径只能是隔离工作目录内的相对路径")
    if value == "." and directory:
        return value
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("文件路径不能访问工作目录之外的位置")
    return value


def validate_arguments(operation, arguments):
    if not isinstance(operation, str) or operation not in _OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("隔离执行操作或参数不正确")
    result = copy.deepcopy(arguments)
    if operation == "python":
        if set(result) != {"code"}:
            raise ValueError("Python 操作只接受 code")
        result["code"] = _text(result["code"], 12000, "Python 代码")
    elif operation == "shell":
        if set(result) != {"command"}:
            raise ValueError("命令操作只接受 command")
        result["command"] = _text(result["command"], 8000, "Shell 命令")
    elif operation == "read_file":
        if set(result) - {"path", "offset", "limit"} or "path" not in result:
            raise ValueError("读取文件参数不正确")
        result["path"] = _path(result["path"])
        for key, default, low, high in (("offset", 0, 0, 65536), ("limit", 2000, 1, 4000)):
            value = result.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("文件读取位置或长度不正确")
            result[key] = value
    elif operation == "write_file":
        if set(result) != {"path", "content"}:
            raise ValueError("写入文件只接受 path 和 content")
        result["path"] = _path(result["path"])
        result["content"] = _text(result["content"], 65536, "文件内容", empty=True)
        if len(result["content"].encode("utf-8")) > 65536:
            raise ValueError("文件内容不能超过 64 KiB")
    elif operation == "list_files":
        if set(result) - {"path"}:
            raise ValueError("列出文件只接受 path")
        result["path"] = _path(result.get("path", "."), directory=True)
    else:
        fields = {"goto": {"url"}, "snapshot": set(), "screenshot": set(), "click": {"selector"},
                  "fill": {"selector", "text"}, "select": {"selector", "value"},
                  "evaluate": {"script"}, "set_content": {"html"}}
        action = result.get("action")
        if not isinstance(action, str) or action not in fields or set(result) != fields[action] | {"action"}:
            raise ValueError("浏览器操作参数不正确")
        for key in fields[action]:
            maximum = 65536 if key == "html" else 12000 if key == "script" else 4000
            result[key] = _text(result[key], maximum, "浏览器参数", empty=key in {"text", "value", "html"})
            if key == "html" and len(result[key].encode("utf-8")) > 65536:
                raise ValueError("浏览器 HTML 不能超过 64 KiB")
        if action == "goto":
            try:
                target = urlsplit(result["url"])
                valid = (target.scheme in {"http", "https"} and target.hostname
                         and target.username is None and target.password is None
                         and "\\" not in result["url"] and not any(ord(c) <= 32 or ord(c) == 127 for c in result["url"]))
                target.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("浏览器只接受不含账号的 HTTP 或 HTTPS 地址")
    return result


def _json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("non-finite number")
    try:
        result = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeError("隔离执行服务返回了无效响应") from None
    if not isinstance(result, dict):
        raise RuntimeError("隔离执行服务返回了无效响应")
    return result


class AssistantSandbox:
    def __init__(self, deps, store, *, settings, clean, transport=None):
        self.deps, self.store, self.settings, self.clean = deps, store, settings, clean
        self.transport = transport  # Tests may inject MockTransport; never a shared/proxied client.
        self._config_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)
        self._session_locks = weakref.WeakValueDictionary()
        self._tickets, self._used, self._active, self._sessions = {}, {}, {}, {}
        self._artifacts = {}
        self._secrets = []
        self._closed = False

    def known_secrets(self):
        return list(self._secrets)

    def _remember_secret(self, token):
        if token and token not in self._secrets:
            self._secrets = [*self._secrets[-15:], token]

    async def _config(self):
        raw = await self.store.get("sandbox_config", "default")
        config = {"id": "default", "enabled": False, "url": "", "token": "", "revision": ""}
        if raw is not None:
            if not isinstance(raw, dict) or set(raw) - set(config) or raw.get("id") != "default":
                raise ValueError("隔离执行配置不正确，请重新保存")
            config.update(raw)
        if type(config["enabled"]) is not bool or not isinstance(config["revision"], str):
            raise ValueError("隔离执行配置不正确，请重新保存")
        if config["url"]:
            config["url"] = validate_endpoint(config["url"])
        elif not isinstance(config["url"], str):
            raise ValueError("隔离执行服务地址不正确")
        if config["token"]:
            _token(config["token"])
            self._remember_secret(config["token"])
        elif not isinstance(config["token"], str):
            raise ValueError("隔离执行 Token 不正确")
        if config["enabled"] and not (config["url"] and config["token"]):
            raise ValueError("启用隔离执行前，请配置服务地址和随机 Token")
        return config

    @staticmethod
    def _public(config):
        return {"enabled": config["enabled"], "url": config["url"],
                "token_configured": bool(config["token"]), "token": "********" if config["token"] else ""}

    async def status(self):
        return self._public(await self._config())

    async def save_config(self, values):
        if self._closed:
            raise PermissionError("隔离执行服务已停止")
        if not isinstance(values, dict) or set(values) - {"enabled", "url", "token", "clear_token"}:
            raise ValueError("隔离执行配置项不正确")
        if "enabled" in values and type(values["enabled"]) is not bool:
            raise ValueError("隔离执行开关格式不正确")
        if "clear_token" in values and type(values["clear_token"]) is not bool:
            raise ValueError("清除 Token 的开关格式不正确")
        cancelled = False
        async with self._config_lock:
            old = await self._config()
            config = copy.deepcopy(old)
            if "url" in values:
                value = values["url"]
                if not isinstance(value, str):
                    raise ValueError("隔离执行服务地址不正确")
                config["url"] = validate_endpoint(value.strip()) if value.strip() else ""
            if "enabled" in values:
                config["enabled"] = values["enabled"]
            supplied = values.get("token", "")
            if not isinstance(supplied, str):
                raise ValueError("隔离执行 Token 不正确")
            if values.get("clear_token"):
                if supplied not in {"", "********"}:
                    raise ValueError("不能同时保存和清除 Token")
                config["token"] = ""
            elif supplied not in {"", "********"}:
                config["token"] = _token(supplied)
            if config["enabled"] and not (config["url"] and config["token"]):
                raise ValueError("启用隔离执行前，请配置服务地址和随机 Token")
            if config == old:
                return self._public(config)
            config["revision"] = uuid4().hex
            commit = asyncio.create_task(self.store.put("sandbox_config", config))
            while True:
                try:
                    await asyncio.shield(commit)
                    break
                except asyncio.CancelledError:
                    if commit.cancelled():
                        # A shield only protects against cancellation of its
                        # caller. Shutdown may also cancel the commit itself;
                        # waiting on that completed child again would spin.
                        raise
                    cancelled = True
            self._remember_secret(config["token"])
            self._tickets.clear()
            self._artifacts.clear()
            for item in list(self._active.values()):
                if not item["task"].cancelling():
                    item["task"].cancel()
            sessions = list(self._sessions.items())
        await self._cleanup_sessions(sessions)
        if cancelled:
            raise asyncio.CancelledError
        return self._public(config)

    def _authorized(self, config):
        settings = self.settings()
        if (self._closed or not isinstance(settings, dict) or settings.get("enabled") is not True
                or settings.get("allow_actions") is not True or config["enabled"] is not True):
            raise PermissionError("隔离执行已关闭，或助手未获执行权限")

    async def _guard(self, expected, expiry=None, *, execution=True):
        config = await self._config()
        if execution:
            self._authorized(config)
        if self._closed or _digest(config) != expected:
            raise PermissionError("隔离执行设置已变化，请重新确认")
        if expiry is not None and time.time() >= expiry:
            raise PermissionError("本次隔离执行确认已过期")
        return config

    def _safe_text(self, value, limit=100000):
        text = str(value)
        for token in self._secrets:
            text = text.replace(token, "********")
        return self.clean(text, limit)

    async def prepare(self, session_id, operation, arguments):
        session_id = _identity(session_id)
        config = await self._config()
        self._authorized(config)
        arguments = validate_arguments(operation, arguments)
        # Check original strings as well as their JSON representation: JSON
        # escaping must not hide a known credential containing quotes or slashes.
        for value in arguments.values():
            if isinstance(value, str) and self._safe_text(value, len(value) + 1) != value:
                raise PermissionError("操作包含系统凭据或敏感值，不能发送到隔离执行环境")
        payload = {"session_id": session_id, "operation": operation, "arguments": arguments}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded) > 98000:
            raise ValueError("操作预览过长，无法完整确认，请精简内容")
        if self._safe_text(encoded, len(encoded) + 1) != encoded:
            raise PermissionError("操作包含系统凭据或敏感值，不能发送到隔离执行环境")
        now = time.time()
        self._tickets = {key: item for key, item in self._tickets.items() if item["expires"] > now}
        self._used = {key: expiry for key, expiry in self._used.items() if expiry > now}
        if len(self._tickets) >= 256 or len(self._used) >= 1024:
            raise ValueError("待过期的隔离执行确认较多，请稍后再试")
        request_id = uuid4().hex
        fingerprint = _digest({"payload": payload, "config": _digest(config), "request_id": request_id})
        self._tickets[request_id] = {"fingerprint": fingerprint, "config": _digest(config),
                                     "session_id": session_id, "expires": now + 900}
        labels = {"python": "运行 Python", "shell": "运行 Shell 命令", "read_file": "读取隔离文件",
                  "write_file": "写入隔离文件", "list_files": "列出隔离文件", "browser": "操作隔离浏览器"}
        return {"name": "sandbox_execute", "arguments": payload,
                "summary": labels[operation] + "。仅在独立工作目录执行，不访问系统文件；请核对完整参数。",
                "read_only": False, "fingerprint": fingerprint, "_request_id": request_id}

    async def _request(self, config, method, path, *, payload=None, timeout=65, before_send=None):
        if before_send is not None:
            await before_send()
        async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                follow_redirects=False, timeout=httpx.Timeout(timeout, connect=5),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=0)) as client:
            async with client.stream(method, config["url"] + path, json=payload,
                    headers={"Authorization": "Bearer " + config["token"], "Accept": "application/json",
                             "Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise RuntimeError("隔离执行服务请求失败，请检查连接、Token 和执行服务状态")
                if (response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}
                        or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
                    raise RuntimeError("隔离执行服务返回了不支持的响应")
                length = response.headers.get("content-length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,20}", length) or int(length) > MAX_RESPONSE_BYTES):
                    raise RuntimeError("隔离执行服务响应过长，已停止读取")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise RuntimeError("隔离执行服务响应过长，已停止读取")
                    data.extend(chunk)
                return _json(data)

    async def check(self):
        config = await self._config()
        if self._closed or not config["url"] or not config["token"]:
            raise ValueError("请先保存执行服务地址和 Token")
        try:
            async with self._slots:
                async with asyncio.timeout(8):
                    result = await self._request(config, "GET", "/v1/health", timeout=5,
                        before_send=lambda: self._guard(_digest(config), execution=False))
                    await self._guard(_digest(config), execution=False)
            expected = {"ok": True, "service": "awbotnest-sandbox", "protocol": 1,
                        "network": "broker-public-fetch", "max_sessions": 2}
            if (not all(result.get(key) == value for key, value in expected.items())
                    or type(result.get("ok")) is not bool or type(result.get("protocol")) is not int
                    or type(result.get("max_sessions")) is not int):
                raise RuntimeError("隔离执行服务协议不兼容，请更新执行服务")
            return expected
        except (ValueError, PermissionError):
            raise
        except Exception:
            raise RuntimeError("执行服务连接检查失败，请检查地址、Token 和服务状态") from None

    async def _delete(self, config, session_id):
        if not config.get("url") or not config.get("token"):
            return False
        try:
            async with asyncio.timeout(5):
                result = await self._request(config, "DELETE", "/v1/sessions/" + session_id, timeout=4)
            return result.get("session_id") == session_id and result.get("deleted") is True
        except Exception:
            return False

    async def _finish_delete(self, config, session_id):
        # Cleanup is bounded and must finish even if its caller is cancelled
        # again; it never retries an execution request.
        task = asyncio.create_task(self._delete(config, session_id))
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise  # The cleanup child itself cannot be awaited again.
                continue
        if self._sessions.get(session_id) is config:
            self._sessions.pop(session_id, None)
        self._prune_artifacts(session_id)
        return result

    async def execute(self, prepared):
        if not isinstance(prepared, dict) or prepared.get("name") != "sandbox_execute":
            raise ValueError("隔离执行请求不正确")
        expiry = prepared.get("_approval_expires")
        if type(expiry) not in {int, float} or not math.isfinite(expiry) or not time.time() < expiry <= time.time() + 901:
            raise PermissionError("本次隔离执行缺少有效确认")
        request_id = _identity(prepared.get("_request_id"))
        ticket = self._tickets.get(request_id)
        payload = prepared.get("arguments")
        if not isinstance(payload, dict) or set(payload) != {"session_id", "operation", "arguments"}:
            raise ValueError("隔离执行请求参数不正确")
        session_id = _identity(payload["session_id"])
        arguments = validate_arguments(payload["operation"], payload["arguments"])
        if arguments != payload["arguments"]:
            raise ValueError("隔离执行参数已变化，请重新确认")
        if (not ticket or request_id in self._used or ticket["expires"] <= time.time()
                or prepared.get("read_only") is not False
                or prepared.get("fingerprint") != ticket["fingerprint"]
                or _digest({"payload": payload, "config": ticket["config"], "request_id": request_id}) != ticket["fingerprint"]):
            raise PermissionError("本次隔离执行确认无效、已使用或已变化")
        # Claim before waiting on either queue. Failure/cancellation cannot reuse
        # this request ID, even if it is unknown whether the broker accepted it.
        self._tickets.pop(request_id, None)
        self._used[request_id] = min(expiry, ticket["expires"])
        async with self._session_locks.setdefault(session_id, asyncio.Lock()):
            async with self._slots:
                config = await self._guard(ticket["config"], expiry)
                if len(self._sessions) >= 32 and session_id not in self._sessions:
                    raise ValueError("隔离执行会话较多，请先关闭不用的会话")
                self._sessions[session_id] = config
                body = {**payload, "request_id": request_id}
                task = asyncio.create_task(self._request(config, "POST", "/v1/execute", payload=body,
                    before_send=lambda: self._guard(ticket["config"], expiry)))
                self._active[request_id] = {"task": task, "session_id": session_id, "config": config}
                uncertain = True
                try:
                    async with asyncio.timeout(70):
                        response = await task
                    await self._guard(ticket["config"], expiry)
                    if (set(response) - {"session_id", "request_id", "operation", "ok", "result", "error"}
                            or response.get("session_id") != session_id or response.get("request_id") != request_id
                            or response.get("operation") != payload["operation"] or type(response.get("ok")) is not bool
                            or not isinstance(response.get("result"), dict)
                            or "error" in response and not isinstance(response["error"], str)):
                        raise RuntimeError("隔离执行服务返回了不匹配的结果")
                    if not response["ok"]:
                        raise RuntimeError("隔离执行未完成，未自动重试；请检查执行服务状态")
                    if payload["operation"] == "browser" and arguments["action"] == "screenshot":
                        result = self._screenshot(session_id, response["result"])
                    else:
                        result = self._sanitize_result(response["result"])
                    uncertain = False
                    return {"session_id": session_id, "request_id": request_id, "operation": payload["operation"],
                            "ok": True, "result": result, "untrusted": True}
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    raise PermissionError("隔离执行设置已变化，本次操作已停止，未自动重试") from None
                except PermissionError:
                    raise
                except Exception:
                    raise RuntimeError("隔离执行结果未确认，未自动重试；请检查实际结果") from None
                finally:
                    self._active.pop(request_id, None)
                    if uncertain:
                        await self._finish_delete(config, session_id)

    def _sanitize_result(self, result):
        def visit(value, depth=0):
            if depth > 12:
                raise ValueError("隔离执行结果层级过深")
            if isinstance(value, str):
                return self._safe_text(value, MAX_RESULT_BYTES)
            if isinstance(value, dict):
                if len(value) > 128 or any(not isinstance(key, str) or len(key) > 200 for key in value):
                    raise ValueError("隔离执行结果字段过多")
                return {self._safe_text(key, 200): visit(item, depth + 1) for key, item in value.items()}
            if isinstance(value, list):
                if len(value) > 256:
                    raise ValueError("隔离执行结果项目过多")
                return [visit(item, depth + 1) for item in value]
            if value is None or type(value) in {bool, int, float}:
                return value
            raise ValueError("隔离执行结果类型不正确")
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("隔离执行结果超过 16 KiB")
        safe = visit(result)
        if len(json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("隔离执行结果超过 16 KiB")
        return safe

    def _prune_artifacts(self, session_id=None):
        now = time.monotonic()
        self._artifacts = {key: item for key, item in self._artifacts.items()
                           if item["expires"] > now and item["session_id"] != session_id}

    def _screenshot(self, session_id, result):
        encoded = result.get("data_base64")
        if (set(result) != {"mime_type", "data_base64"} or result.get("mime_type") != "image/jpeg"
                or not isinstance(encoded, str) or not 1 <= len(encoded) <= ((11000 + 2) // 3) * 4):
            raise ValueError("浏览器截图响应不正确")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("浏览器截图内容不正确") from None
        if not 3 < len(data) <= 11000 or not data.startswith(b"\xff\xd8\xff"):
            raise ValueError("浏览器截图格式不正确或超过限制")
        self._prune_artifacts()
        while len(self._artifacts) >= 16:
            self._artifacts.pop(next(iter(self._artifacts)))
        artifact_id = uuid4().hex
        self._artifacts[artifact_id] = {"session_id": session_id, "mime_type": "image/jpeg", "data": data,
                                        "expires": time.monotonic() + 900}
        return {"artifact": {"id": artifact_id, "mime_type": "image/jpeg", "title": "浏览器截图"},
                "notice": "截图供管理员查看；不能声称已从图片识别内容"}

    async def artifact(self, artifact_id):
        self._prune_artifacts()
        item = self._artifacts.get(artifact_id) if isinstance(artifact_id, str) else None
        if self._closed or item is None:
            raise KeyError("截图不存在或已过期")
        return item["data"], item["mime_type"]

    async def _close_known(self, session_id, config):
        async with self._session_locks.setdefault(session_id, asyncio.Lock()):
            async with self._slots:
                if self._sessions.get(session_id) is not config:
                    return False
                return await self._finish_delete(config, session_id)

    async def _cleanup_sessions(self, sessions):
        try:
            async with asyncio.timeout(8):
                await asyncio.gather(*(self._close_known(session_id, config) for session_id, config in sessions))
        except TimeoutError:
            pass

    async def close_session(self, session_id):
        # Older assistant records can use IDs that predate the sandbox's
        # stricter protocol. Cleanup is also called for ordinary runs that
        # never used a sandbox, so unrelated IDs must remain a local no-op.
        if isinstance(session_id, str) and not (
                session_id in self._sessions
                or any(item["session_id"] == session_id for item in self._tickets.values())
                or any(item["session_id"] == session_id for item in self._artifacts.values())
                or any(item["session_id"] == session_id for item in self._active.values())):
            return {"session_id": session_id, "deleted": True}
        session_id = _identity(session_id)
        self._prune_artifacts(session_id)
        for request_id, ticket in list(self._tickets.items()):
            if ticket["session_id"] == session_id:
                self._tickets.pop(request_id, None)
        for item in list(self._active.values()):
            if item["session_id"] == session_id and not item["task"].cancelling():
                item["task"].cancel()
        config = self._sessions.get(session_id)
        deleted = await self._close_known(session_id, config) if config else True
        return {"session_id": session_id, "deleted": deleted}

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._tickets.clear()
        self._artifacts.clear()
        for item in list(self._active.values()):
            if not item["task"].cancelling():
                item["task"].cancel()
        await self._cleanup_sessions(list(self._sessions.items()))
