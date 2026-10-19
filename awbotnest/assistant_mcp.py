"""Administrator-configured Streamable HTTP MCP, with no process/file transport.

Remote annotations never grant read access. Only an administrator's explicit
read-only list can remove confirmation; every other tool is single-use approval.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import math
import re
import socket
import time
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

import httpcore
import httpx

from .assistant_tools import validate_tool_arguments, validate_tool_schema
from .logs import redact_secrets

MAX_SERVERS = 4
MAX_TOOLS = 32
MAX_RESPONSE = 128 * 1024
MAX_CATALOG_PAGE = 7000
PROTOCOL = "2025-03-26"
_ID = re.compile(r"[a-f0-9]{32}\Z")
_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def validate_endpoint(value, token=""):
    if not isinstance(value, str) or len(value) > 1000 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise ValueError("MCP 地址格式不正确")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError("MCP 地址格式不正确") from None
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or "\\" in value
            or parsed.path.startswith("//") or port is not None and not 1 <= port <= 65535):
        raise ValueError("MCP 仅支持不含账号、查询参数的公网 HTTP 地址")
    if token and parsed.scheme != "https":
        raise ValueError("填写 Token 时，MCP 地址必须使用 HTTPS")
    return value


async def public_endpoint(value):
    """Validate all DNS answers and pin this connection, including with a proxy.

    The Host header and TLS SNI retain the configured hostname. A second DNS
    lookup cannot turn a validated public endpoint into a private connection.
    """
    parsed = urlsplit(validate_endpoint(value))
    host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) or host.endswith("."):
            raise ValueError("MCP 主机名不正确") from None
        try:
            async with asyncio.timeout(5):
                answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
        except Exception:
            raise ValueError("MCP 地址解析失败") from None
    if (not addresses or len(addresses) > 32 or any(not item.is_global or item.is_multicast
            or isinstance(item, ipaddress.IPv6Address) and item.ipv4_mapped is not None
            and not item.ipv4_mapped.is_global for item in addresses)):
        raise ValueError("MCP 地址必须解析到公网地址，不能访问本机或内网")
    address = str(addresses[0])
    authority = f"[{address}]" if ":" in address else address
    authority += f":{port}"
    pinned = urlunsplit((parsed.scheme, authority, parsed.path or "/", "", ""))
    original_authority = f"[{host}]" if ":" in host else host
    if port != (443 if parsed.scheme == "https" else 80):
        original_authority += f":{port}"
    return pinned, original_authority, host


class _SNIStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, target_ip, hostname, proxy_tls=False):
        self.stream, self.target_ip, self.hostname, self.proxy_tls = stream, target_ip, hostname, proxy_tls

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        return await self.stream.write(buffer, timeout)

    async def aclose(self):
        await self.stream.aclose()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # httpcore's CONNECT tunnel does not honor the sni_hostname extension.
        # Change only the target TLS name, never certificate verification or the
        # proxy's own outer TLS connection. CONNECT still targets the pinned IP.
        if self.proxy_tls:
            self.proxy_tls = False
        elif server_hostname == self.target_ip:
            server_hostname = self.hostname
        stream = await self.stream.start_tls(ssl_context, server_hostname=server_hostname, timeout=timeout)
        return _SNIStream(stream, self.target_ip, self.hostname)


class _SNIBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend, target_ip, hostname, *, proxy_tls=False):
        self.backend, self.target_ip, self.hostname, self.proxy_tls = backend, target_ip, hostname, proxy_tls

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self.backend.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                               socket_options=socket_options)
        return _SNIStream(stream, self.target_ip, self.hostname, self.proxy_tls)

    async def connect_unix_socket(self, *args, **kwargs):
        raise ValueError("MCP 不允许 Unix socket")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class MCPHttpTransport:
    def __init__(self, http, *, resolver=public_endpoint, stream=None):
        self.http, self.resolver, self.stream = http, resolver, stream

    @asynccontextmanager
    async def _pinned_stream(self, method, url, *, hostname, **kwargs):
        proxy = getattr(getattr(self.http, "settings", None), "proxy_url", "") or None
        transport = httpx.AsyncHTTPTransport(proxy=proxy, retries=0, trust_env=False)
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            await transport.aclose()
            raise RuntimeError("当前 HTTP 组件无法安全固定 MCP 连接")
        pool._network_backend = _SNIBackend(backend, urlsplit(url).hostname, hostname,
                                           proxy_tls=bool(proxy and urlsplit(proxy).scheme == "https"))
        # A dedicated bounded client is necessary here: the shared service
        # cannot supply a per-connection SNI backend. Use its exact configured
        # proxy; never bypass it or fall back to environment proxy variables.
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            async with client.stream(method, url, **kwargs) as response:
                yield response

    async def request(self, server, payload, session="", *, authorize=None):
        url, host, sni = await self.resolver(server["url"])
        if authorize is not None:
            await authorize()
        headers = {"Host": host, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "Accept-Encoding": "identity",
                   "MCP-Protocol-Version": PROTOCOL}
        if server.get("token"):
            headers["Authorization"] = "Bearer " + server["token"]
        if session:
            headers["Mcp-Session-Id"] = session
        notification = "id" not in payload
        stream = self.stream or self._pinned_stream
        stream_options = {} if self.stream else {"hostname": sni}
        async with asyncio.timeout(30):
            async with stream("POST", url, timeout=25, follow_redirects=False, headers=headers,
                    json=payload, extensions={"sni_hostname": sni}, **stream_options) as response:
                if notification and response.status_code in {200, 202, 204}:
                    return None, session
                if response.status_code != 200:
                    raise ValueError("MCP 服务请求失败，请检查地址和权限")
                new_session = response.headers.get("mcp-session-id", session)
                if (not isinstance(new_session, str) or len(new_session) > 200
                        or any(ord(ch) < 33 or ord(ch) > 126 for ch in new_session)):
                    raise ValueError("MCP 会话响应不正确")
                if response.headers.get("content-encoding", "identity").lower().strip() not in {"identity", ""}:
                    raise ValueError("MCP 响应压缩格式不支持")
                kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if kind not in {"application/json", "text/event-stream"}:
                    raise ValueError("MCP 响应不是 JSON 或事件流")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    if len(body) + len(chunk) > MAX_RESPONSE:
                        raise ValueError("MCP 响应过长，已停止读取")
                    body.extend(chunk)
                    if kind == "text/event-stream":
                        normalized = bytes(body).replace(b"\r\n", b"\n")
                        while b"\n\n" in normalized:
                            event, normalized = normalized.split(b"\n\n", 1)
                            data = b"\n".join(line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:"))
                            if data:
                                result = json.loads(data)
                                if isinstance(result, dict) and result.get("id") == payload.get("id"):
                                    return result, new_session
                        # Keep partial events only, but retain a total-byte bound.
                        # The main body stays bounded even with notification spam.
                if kind == "application/json":
                    return json.loads(body), new_session
                raise ValueError("MCP 事件流没有返回当前请求结果")


class AssistantMCP:
    def __init__(self, deps, store, *, settings, clean, transport=None):
        self.deps, self.store, self.settings, self.clean = deps, store, settings, clean
        self.transport = transport
        self._lock = asyncio.Lock()
        self._servers_locks = {}
        self._catalogs, self._sessions, self._tickets = {}, {}, {}

    def _enabled(self):
        config = self.settings()
        if not isinstance(config, dict) or config.get("enabled") is not True or config.get("mcp_enabled") is not True:
            raise PermissionError("MCP 已关闭")
        return config

    async def _server(self, server_id, *, enabled=True):
        if not isinstance(server_id, str) or not _ID.fullmatch(server_id):
            raise ValueError("MCP 服务编号不正确")
        record = await self.store.get("mcp_server", server_id)
        if record is None:
            raise ValueError("MCP 服务不存在")
        if enabled:
            self._enabled()
            if record.get("enabled") is not True:
                raise PermissionError("此 MCP 服务已关闭")
        validate_endpoint(record["url"], record.get("token", ""))
        return record

    @staticmethod
    def _public(server):
        return {key: copy.deepcopy(value) for key, value in server.items() if key != "token"} | {
            "token_configured": bool(server.get("token"))}

    async def list_servers(self):
        return [self._public(row) for row in await self.store.list("mcp_server", MAX_SERVERS)]

    async def save_server(self, values, server_id=None):
        if (not isinstance(values, dict) or values.keys() - {"name", "url", "token", "enabled", "read_only_tools"}
                or not isinstance(values.get("name"), str) or not 1 <= len(values["name"].strip()) <= 80
                or type(values.get("enabled", False)) is not bool):
            raise ValueError("MCP 服务设置不正确")
        async with self._lock:
            previous = await self._server(server_id, enabled=False) if server_id is not None else {}
            token = values.get("token", previous.get("token", ""))
            if token == "********":
                token = previous.get("token", "")
            if not isinstance(token, str) or len(token) > 4096 or any(ord(ch) < 33 or ord(ch) > 126 for ch in token):
                raise ValueError("MCP Token 格式不正确")
            url = validate_endpoint(values.get("url", previous.get("url")), token)
            decoded_url = url
            for _ in range(3):
                if token and token in decoded_url or self.clean(decoded_url, limit=3000) != decoded_url:
                    raise ValueError("不要把凭据写在 MCP 地址里")
                decoded_url = unquote(decoded_url)
            read_only = values.get("read_only_tools", previous.get("read_only_tools", []))
            if (not isinstance(read_only, list) or len(read_only) > MAX_TOOLS or
                    any(not isinstance(name, str) or not _TOOL.fullmatch(name) for name in read_only)):
                raise ValueError("MCP 只读工具名称不正确")
            if not previous and len(await self.store.list("mcp_server", MAX_SERVERS + 1)) >= MAX_SERVERS:
                raise ValueError("最多配置 4 个 MCP 服务")
            name = values["name"].strip().replace(token, "********") if token else values["name"].strip()
            item = {"id": server_id or uuid4().hex, "name": self.clean(name, limit=80),
                    "url": url, "token": token, "enabled": values.get("enabled", False),
                    "read_only_tools": sorted(set(read_only)), "generation": uuid4().hex}
            await self.store.put("mcp_server", item)
            self._catalogs.pop(item["id"], None)
            self._sessions.pop(item["id"], None)
            return self._public(item)

    async def delete_server(self, server_id):
        async with self._lock:
            await self._server(server_id, enabled=False)
            await self.store.delete("mcp_server", server_id)
            self._catalogs.pop(server_id, None)
            self._sessions.pop(server_id, None)
            self._servers_locks.pop(server_id, None)
        return {"deleted": True}

    def _transport(self):
        if self.transport is not None:
            return self.transport
        http = getattr(getattr(getattr(self.deps, "runtime", None), "services", None), "http", None)
        if http is None or not callable(getattr(http, "stream", None)):
            raise RuntimeError("系统 HTTP 服务不可用")
        return MCPHttpTransport(http)

    async def _rpc(self, server, method, params, *, notification=False, authorize=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = uuid4().hex
        cached = self._sessions.get(server["id"], {})
        session = cached.get("session", "") if cached.get("generation") == server["generation"] else ""
        async def guard():
            current = await self._server(server["id"])
            if _digest(current) != _digest(server):
                raise PermissionError("MCP 服务设置已变化，未发送请求")
            if authorize is not None:
                await authorize()
        try:
            reply, session = await self._transport().request(server, payload, session, authorize=guard)
            # Saving a replacement server revokes its cached session while an
            # old request may still be in flight. Do not restore that old
            # session or return its result after the authority has changed.
            await guard()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("MCP 请求未完成；未自动重试") from None
        if notification:
            return None
        if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0" or reply.get("id") != payload["id"]
                or "error" in reply or not isinstance(reply.get("result"), dict)):
            raise ValueError("MCP 服务返回了错误或无效响应，未自动重试")
        self._sessions[server["id"]] = {"session": session, "generation": server["generation"]}
        return reply["result"]

    async def _initialize(self, server):
        if self._sessions.get(server["id"], {}).get("generation") == server["generation"]:
            return
        result = await self._rpc(server, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "AWBotNest", "version": "1"}})
        if result.get("protocolVersion") != PROTOCOL:
            self._sessions.pop(server["id"], None)
            raise ValueError("MCP 服务不支持当前 HTTP 协议版本")
        try:
            await self._rpc(server, "notifications/initialized", {}, notification=True)
        except BaseException:
            self._sessions.pop(server["id"], None)
            raise

    def _safe(self, value, server, limit=12000):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False) if not isinstance(value, str) else value
        cached = self._sessions.get(server["id"], {})
        session = cached.get("session", "") if cached.get("generation") == server["generation"] else ""
        for credential in (server.get("token"), session):
            if credential:
                text = text.replace(credential, "********")
                escaped = json.dumps(credential, ensure_ascii=False)[1:-1]
                text = text.replace(escaped, "********")
        return self.clean(redact_secrets(text), limit=limit)

    def _contains_sensitive(self, value, server):
        # Check raw strings before JSON escaping can hide quotes/backslashes in
        # a known credential. The encoded check still catches labelled values.
        if isinstance(value, str):
            return self._safe(value, server, len(value) + 1) != value
        if isinstance(value, dict):
            return any(self._contains_sensitive(key, server) or self._contains_sensitive(item, server)
                       for key, item in value.items())
        if isinstance(value, list):
            return any(self._contains_sensitive(item, server) for item in value)
        return False

    async def discover(self, server_id):
        server = await self._server(server_id)
        lock = self._servers_locks.setdefault(server_id, asyncio.Lock())
        async with asyncio.timeout(45), lock:
            current = await self._server(server_id)
            await self._initialize(current)
            tools, cursor, seen, skipped = [], None, set(), 0
            for _ in range(4):
                params = {"cursor": cursor} if cursor else {}
                result = await self._rpc(current, "tools/list", params)
                raw = result.get("tools")
                if not isinstance(raw, list) or len(raw) > 128:
                    raise ValueError("MCP 工具列表格式不正确或过长")
                for item in raw:
                    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not _TOOL.fullmatch(item["name"]):
                        skipped += 1
                        continue
                    name = item["name"]
                    if name in seen:
                        raise ValueError("MCP 工具名称重复")
                    seen.add(name)
                    try:
                        if not isinstance(item.get("inputSchema"), dict):
                            raise ValueError("missing input schema")
                        schema = validate_tool_schema(item.get("inputSchema"))
                        encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False)
                        if (self._contains_sensitive({"name": name, "schema": schema}, current)
                                or self._safe(encoded, current, MAX_RESPONSE) != encoded):
                            raise ValueError("sensitive schema")
                    except ValueError:
                        skipped += 1
                        continue
                    tools.append({"name": name, "description": self._safe(str(item.get("description", name)), current, 400),
                                  "parameters": schema, "read_only": name in current["read_only_tools"]})
                    if len(tools) > MAX_TOOLS:
                        raise ValueError("MCP 工具过多，请在服务端缩小开放范围")
                cursor = result.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or len(cursor) > 1000:
                    raise ValueError("MCP 分页格式不正确")
            else:
                if cursor:
                    raise ValueError("MCP 工具列表页数过多")
            fresh = await self._server(server_id)
            if fresh["generation"] != current["generation"]:
                raise PermissionError("MCP 服务设置已变化，请重新读取工具")
            catalog = {"generation": current["generation"], "tools": tools, "skipped": skipped,
                       "expires": time.time() + 300}
            self._catalogs[server_id] = catalog
            return {"server_id": server_id, "name": current["name"], "tools": copy.deepcopy(tools),
                    "skipped": skipped, "untrusted": True}

    @staticmethod
    def _tool_detail(server, item):
        """A tool's entire schema must fit a single ordinary model result.

        Never authorize a schema which can only be shown as a chopped JSON
        string: the caller must be able to inspect every declared parameter.
        """
        result = {"server_id": server["id"], "name": server["name"],
                  "tool": copy.deepcopy(item), "untrusted": True}
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > MAX_CATALOG_PAGE:
            raise ValueError("此 MCP 工具的参数定义过长，无法完整展示，未开放调用；请在服务端缩小定义")
        return result

    async def list_tools(self, server_id, *, tool=None, offset=0):
        """Bounded directory pages or one complete schema, never a JSON excerpt."""
        if type(offset) is not int or not 0 <= offset <= MAX_TOOLS:
            raise ValueError("MCP 工具列表位置不正确")
        if tool is not None and (not isinstance(tool, str) or not _TOOL.fullmatch(tool) or offset):
            raise ValueError("MCP 工具名称不正确，读取单个定义时不能同时分页")
        server = await self._server(server_id)
        catalog = self._catalogs.get(server_id)
        if not catalog or catalog["generation"] != server["generation"] or catalog["expires"] <= time.time():
            await self.discover(server_id)
            catalog = self._catalogs[server_id]
        # Recheck even a cache hit; disabling or changing a server must revoke
        # access immediately rather than waiting for its five-minute TTL.
        server = await self._server(server_id)
        if catalog["generation"] != server["generation"]:
            raise PermissionError("MCP 服务设置已变化，请重新读取工具")
        if tool is not None:
            item = next((row for row in catalog["tools"] if row["name"] == tool), None)
            if item is None:
                raise ValueError("MCP 工具不存在或参数规范暂不支持")
            return self._tool_detail(server, item)
        total = len(catalog["tools"])
        result = {"server_id": server_id, "name": server["name"], "tools": [], "total": total,
                  "offset": offset, "next_offset": None, "complete": offset >= total,
                  "skipped": catalog.get("skipped", 0), "untrusted": True}
        for item in catalog["tools"][offset:]:
            row = {key: item[key] for key in ("name", "description", "read_only")}
            try:
                self._tool_detail(server, item)
                row["schema_available"] = True
            except ValueError:
                row.update(schema_available=False, unavailable_reason="参数定义过长，未开放调用")
            result["tools"].append(row)
            end = offset + len(result["tools"])
            result.update(next_offset=end if end < total else None, complete=end >= total)
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > MAX_CATALOG_PAGE:
                result["tools"].pop()
                end -= 1
                result.update(next_offset=end, complete=False)
                break
        if offset < total and not result["tools"]:
            raise ValueError("MCP 工具目录无法完整展示，请缩短服务或工具说明")
        return result

    async def prepare_call(self, server_id, tool, arguments):
        server = await self._server(server_id)
        catalog = self._catalogs.get(server_id)
        if not catalog or catalog["generation"] != server["generation"] or catalog["expires"] <= time.time():
            await self.discover(server_id)
            catalog = self._catalogs[server_id]
        server = await self._server(server_id)
        if catalog["generation"] != server["generation"]:
            raise PermissionError("MCP 服务设置已变化，请重新读取工具")
        item = next((row for row in catalog["tools"] if row["name"] == tool), None)
        if item is None:
            raise ValueError("MCP 工具不存在或参数规范暂不支持")
        self._tool_detail(server, item)
        arguments = validate_tool_arguments(item["parameters"], arguments, limit=4096)
        text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if (len(text) > 1500 or self._contains_sensitive(arguments, server)
                or self._safe(text, server, 10000) != text):
            raise ValueError("MCP 参数含敏感值或过长，不能完整确认")
        if not item["read_only"] and self._enabled().get("allow_actions") is not True:
            raise PermissionError("助手当前只允许查询，MCP 写操作未开放")
        payload = {"server_id": server_id, "tool": tool, "arguments": arguments}
        fingerprint = _digest([server, item, payload])
        prepared = {"name": "call_mcp_tool", "arguments": payload, "read_only": item["read_only"],
                    "summary": f"{'查询' if item['read_only'] else '执行'} MCP「{server['name']}」：{tool}\n参数：{text}",
                    "fingerprint": fingerprint}
        self._tickets = {key: value for key, value in self._tickets.items() if value["expires"] > time.time()}
        if len(self._tickets) >= 64:
            raise ValueError("待处理的 MCP 调用较多，请稍后重试")
        ticket = uuid4().hex
        self._tickets[ticket] = {"fingerprint": fingerprint, "expires": time.time() + 900}
        prepared["_mcp_ticket"] = ticket
        return prepared

    async def execute(self, prepared):
        if not isinstance(prepared, dict) or prepared.get("name") != "call_mcp_tool":
            raise ValueError("MCP 调用请求不正确")
        args = prepared.get("arguments")
        if not isinstance(args, dict) or set(args) != {"server_id", "tool", "arguments"}:
            raise ValueError("MCP 调用参数不正确")
        ticket_id = prepared.get("_mcp_ticket")
        ticket = self._tickets.get(ticket_id) if isinstance(ticket_id, str) else None
        if ticket is None or ticket["expires"] <= time.time():
            raise PermissionError("MCP 调用已失效，请重新发起")
        lock = self._servers_locks.setdefault(args["server_id"], asyncio.Lock())
        async with asyncio.timeout(45), lock:
            # Do not discover while holding this lock: changing/expired catalog
            # requires a new preview instead of executing an unseen tool schema.
            server = await self._server(args["server_id"])
            catalog = self._catalogs.get(server["id"])
            if not catalog or catalog["generation"] != server["generation"] or catalog["expires"] <= time.time():
                raise PermissionError("MCP 工具列表已变化或过期，请重新发起")
            item = next((row for row in catalog["tools"] if row["name"] == args["tool"]), None)
            if item is None:
                raise PermissionError("MCP 工具已不可用")
            arguments = validate_tool_arguments(item["parameters"], args["arguments"], limit=4096)
            fingerprint = _digest([server, item, {**args, "arguments": arguments}])
            if fingerprint != prepared.get("fingerprint") or fingerprint != ticket["fingerprint"] or prepared.get("read_only") is not item["read_only"]:
                raise PermissionError("MCP 服务或参数已变化，请重新确认")
            if not item["read_only"]:
                expires = prepared.get("_approval_expires")
                if (self._enabled().get("allow_actions") is not True or type(expires) not in (int, float)
                        or not math.isfinite(expires) or not time.time() < expires <= time.time() + 901):
                    raise PermissionError("MCP 写操作尚未确认或确认已过期")
            if (self._contains_sensitive(arguments, server)
                    or self._safe(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")), server, 10000)
                    != json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))):
                raise PermissionError("MCP 参数脱敏结果已变化，请重新发起")
            # Once network begins, a timeout/cancellation has an unknown result.
            # Consume before POST and never retry, including idempotent hints.
            if self._tickets.pop(ticket_id, None) is None:
                raise PermissionError("MCP 调用已使用，不能重复执行")
            async def check_call():
                if ticket["expires"] <= time.time():
                    raise PermissionError("MCP 调用确认已过期，未发送请求")
                if self._contains_sensitive(arguments, server):
                    raise PermissionError("MCP 参数脱敏结果已变化，未发送请求")
                if not item["read_only"]:
                    if self._enabled().get("allow_actions") is not True or prepared["_approval_expires"] <= time.time():
                        raise PermissionError("MCP 调用确认已失效，未发送请求")
            result = await self._rpc(server, "tools/call", {"name": item["name"], "arguments": arguments}, authorize=check_call)
            content = result.get("content", [])
            if not isinstance(content, list) or len(content) > 100:
                raise ValueError("MCP 结果格式不正确")
            texts = [row["text"] for row in content if isinstance(row, dict) and row.get("type") == "text" and isinstance(row.get("text"), str)]
            return {"server_id": server["id"], "tool": item["name"], "untrusted": True,
                    "is_error": result.get("isError") is True,
                    "text": self._safe("\n".join(texts), server),
                    "omitted_content": len(content) - len(texts)}
