"""Separate Docker broker: this service alone is allowed the Docker socket.

The main application uses authenticated HTTP and never imports Docker code.
No request may select an image, mount, environment, network or Docker option.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from sandbox.contracts import (IDENTIFIER, MAX_OUTPUT, MAX_REQUEST, MAX_WIRE,
                               arguments_for, json_bytes)

RUNNER_IMAGE = "awbotnest-assistant-runner:local"
IDLE_SECONDS = 15 * 60
MAX_SESSIONS = 2
MAX_FETCHES = 80
MAX_FETCH_BYTES = 12 * 1024 * 1024


class BrokerError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class DockerBackend:
    def __init__(self, image=RUNNER_IMAGE, *, owner="awbotnest"):
        if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{1,250}", image):
            raise ValueError("固定执行镜像名称不正确")
        self.image, self.owner = image, owner
        # A killed/timed-out Docker CLI does not prove the daemon cancelled
        # container creation. Keep those names quarantined even if one rm
        # currently says absent; a delayed daemon result may still arrive.
        self.uncertain_creates = set()

    async def command(self, arguments, *, data=None, timeout=10, output_limit=MAX_WIRE):
        process = await asyncio.create_subprocess_exec("docker", *arguments,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        async def read(name, stream):
            while chunk := await stream.read(16384):
                if len(chunks["stdout"]) + len(chunks["stderr"]) + len(chunk) > output_limit:
                    raise BrokerError("执行端响应过长，已停止")
                chunks[name].extend(chunk)
        async def write():
            if process.stdin is not None:
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(read("stdout", process.stdout), read("stderr", process.stderr),
                                     write(), process.wait())
            return process.returncode, bytes(chunks["stdout"]), bytes(chunks["stderr"])
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def start(self, name):
        self.uncertain_creates.add(name)
        code, _, _ = await self.command([
            "run", "-d", "--pull=never", "--name", name,
            "--label", "awbotnest.sandbox.protocol=1", "--label", "awbotnest.sandbox.owner=" + self.owner,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", "1000:1000",
            "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128",
            "--ipc", "private", "--init",
            "--tmpfs", "/workspace:rw,nosuid,nodev,noexec,size=128m,uid=1000,gid=1000,mode=0700",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777", self.image])
        if code:
            raise BrokerError("执行容器启动失败，请检查固定镜像和 Docker 状态", 503)
        self.uncertain_creates.discard(name)
        # Startup probing never executes the requested operation. Only poll
        # until the fixed Unix server is ready, bounded inside the 30s lease.
        for _ in range(30):
            code, _, _ = await self.command(["exec", name, "python3", "-I", "-c",
                "import os,sys;sys.exit(0 if os.path.exists('/tmp/awbotnest-sandbox.sock') else 1)"], timeout=2)
            if code == 0:
                return
            await asyncio.sleep(.05)
        raise BrokerError("执行容器未就绪", 503)

    async def rpc(self, name, payload):
        code, output, _ = await self.command(["exec", "-i", name, "python3", "-I", "/opt/runner/client.py"],
                                               data=json_bytes(payload), timeout=29)
        if code:
            raise BrokerError("执行连接已中断，操作结果未确认；未自动重试", 502)
        try:
            value = json.loads(output)
        except (ValueError, UnicodeError, RecursionError):
            raise BrokerError("执行端未返回有效结果；未自动重试", 502) from None
        if not isinstance(value, dict):
            raise BrokerError("执行端结果不正确", 502)
        return value

    async def remove(self, name):
        code, _, error = await self.command(["rm", "-f", name], timeout=5, output_limit=16384)
        if code and b"No such container" not in error:
            raise BrokerError("执行容器清理未完成，请检查 Docker 状态", 503)
        if name in self.uncertain_creates:
            raise BrokerError("容器创建结果未确认，会话已隔离；请检查 Docker 状态并重启隔离服务", 503)

    async def recover(self):
        code, output, _ = await self.command(["ps", "-aq", "--filter", "label=awbotnest.sandbox.protocol=1",
            "--filter", "label=awbotnest.sandbox.owner=" + self.owner], output_limit=16384)
        if code:
            raise BrokerError("Docker 不可用，隔离执行端未启动", 503)
        identifiers = output.decode("ascii").split()
        if len(identifiers) > 20 or any(not re.fullmatch(r"[a-f0-9]{12,64}", name) for name in identifiers):
            raise BrokerError("隔离容器清单不正确，请检查 Docker 状态", 503)
        for name in identifiers:
            await self.remove(name)


@dataclass
class Session:
    name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    broken: bool = False
    ready: bool = False
    active_task: asyncio.Task | None = None


class Broker:
    def __init__(self, backend, *, fetch=None):
        self.backend, self.fetch = backend, fetch
        self.sessions = {}
        self.seen = {}
        self.pool_lock = asyncio.Lock()
        self.admission = asyncio.Semaphore(4)

    async def claim(self, session_id, request_id):
        async with self.pool_lock:
            cutoff = time.monotonic() - 86400
            self.seen = {key: when for key, when in self.seen.items() if when > cutoff}
            if request_id in self.seen:
                raise BrokerError("请求标识已使用，未重复执行", 409)
            if len(self.seen) >= 4096:
                raise BrokerError("今日执行请求已达上限", 429)
            session = self.sessions.get(session_id)
            if session is None:
                if len(self.sessions) >= MAX_SESSIONS:
                    raise BrokerError("隔离会话已满，请关闭空闲会话后重试", 429)
                session = Session("awbn-sbox-" + self.backend.owner[:12] + "-" + uuid4().hex)
                self.sessions[session_id] = session
            if session.broken:
                raise BrokerError("此隔离会话正在清理，未执行请求", 503)
            self.seen[request_id] = time.monotonic()
            return session

    async def destroy(self, session_id, expected=None, *, cancel_active=True):
        async with self.pool_lock:
            session = self.sessions.get(session_id)
            if session is None or expected is not None and session is not expected:
                return
            session.broken = True
        active = session.active_task
        if cancel_active and active is not None and active is not asyncio.current_task() and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        await self.backend.remove(session.name)
        async with self.pool_lock:
            if self.sessions.get(session_id) is session:
                self.sessions.pop(session_id)

    async def reap(self):
        for session_id, session in list(self.sessions.items()):
            if not session.lock.locked() and time.monotonic() - session.last_used >= IDLE_SECONDS:
                try:
                    await self.destroy(session_id, session)
                except Exception:
                    # Failed cleanup keeps this broken session quarantined,
                    # but must not prevent other idle sessions being removed.
                    pass

    async def _browser(self, session, arguments):
        from sandbox.public_fetch import fetch_public
        fetch = self.fetch or fetch_public
        started = await self.backend.rpc(session.name, {"method": "execute", "operation": "browser", "arguments": arguments})
        job_id = started.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise BrokerError("浏览器任务启动失败", 502)
        waiting, tasks, responses = deque(), {}, deque()
        count, transferred, fetch_ids = 0, 0, set()
        try:
            while True:
                for task in list(tasks):
                    if not task.done():
                        continue
                    fetch_id = tasks.pop(task)
                    try:
                        result = task.result()
                        if not isinstance(result, dict):
                            raise ValueError("invalid fetch result")
                        raw = base64.b64decode(result.get("body_base64", ""), validate=True)
                        transferred += len(raw)
                        if transferred > MAX_FETCH_BYTES:
                            raise BrokerError("网页加载超过 12 MiB 上限，已停止", 413)
                    except BrokerError:
                        raise
                    except Exception:
                        result = {"error": "网页请求失败"}
                    responses.append({"fetch_id": fetch_id, **result})
                # One 1MiB response at a time keeps the fixed CLI framing under
                # 2MiB, even though up to four safe HTTP fetches run in parallel.
                state = await self.backend.rpc(session.name, {"method": "tick", "job_id": job_id,
                    "responses": [responses.popleft()] if responses else []})
                pending = state.get("pending_requests", [])
                if not isinstance(pending, list) or len(pending) > 8:
                    raise BrokerError("浏览器请求队列不正确", 502)
                for item in pending:
                    if (not isinstance(item, dict) or set(item) != {"fetch_id", "url", "method", "headers", "body_base64"}
                            or not isinstance(item["fetch_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", item["fetch_id"])):
                        raise BrokerError("浏览器请求参数不正确", 502)
                    if item["fetch_id"] in fetch_ids:
                        raise BrokerError("浏览器请求标识重复，已停止", 502)
                    fetch_ids.add(item["fetch_id"])
                    try:
                        uploaded = base64.b64decode(item["body_base64"], validate=True)
                    except (ValueError, TypeError):
                        raise BrokerError("浏览器请求正文不正确", 502) from None
                    if len(uploaded) > 64 * 1024:
                        raise BrokerError("浏览器请求正文超过上限，已停止", 413)
                    transferred += len(uploaded)
                    if transferred > MAX_FETCH_BYTES:
                        raise BrokerError("网页加载超过 12 MiB 上限，已停止", 413)
                    count += 1
                    if count > MAX_FETCHES or len(waiting) >= 8:
                        raise BrokerError("网页请求数量超过上限，已停止", 413)
                    waiting.append(item)
                if state.get("done") is True:
                    response = state.get("response")
                    if not isinstance(response, dict):
                        raise BrokerError("浏览器结果不正确", 502)
                    return response
                while waiting and len(tasks) < 4:
                    item = waiting.popleft()
                    fetch_id = item.pop("fetch_id")
                    tasks[asyncio.create_task(fetch(item))] = fetch_id
                await asyncio.sleep(.03)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, session_id, operation, arguments, request_id):
        if not IDENTIFIER.fullmatch(session_id) or not IDENTIFIER.fullmatch(request_id):
            raise BrokerError("会话或请求标识不正确")
        try:
            arguments = arguments_for(operation, arguments)
        except (ValueError, TypeError, UnicodeError):
            raise BrokerError("操作参数不正确") from None
        session = await self.claim(session_id, request_id)
        try:
            async with asyncio.timeout(30), session.lock:
                if session.broken or self.sessions.get(session_id) is not session:
                    raise BrokerError("隔离会话已关闭，未执行请求", 409)
                session.active_task = asyncio.current_task()
                if not session.ready:
                    await self.backend.start(session.name)
                    session.ready = True
                if operation == "browser":
                    response = await self._browser(session, arguments)
                else:
                    response = await self.backend.rpc(session.name, {"method": "execute", "operation": operation, "arguments": arguments})
                if response.get("timed_out"):
                    raise TimeoutError
                if response.get("cleanup_required"):
                    raise BrokerError("检测到残留进程或执行状态异常，隔离会话已关闭；未重试操作", 502)
                if not isinstance(response.get("ok"), bool):
                    raise BrokerError("执行端响应不正确", 502)
                if not isinstance(response.get("result", {}), dict):
                    raise BrokerError("执行端结果字段不正确", 502)
                result = {"session_id": session_id, "request_id": request_id, "operation": operation,
                          "ok": response["ok"], "result": response.get("result", {})}
                if not response["ok"]:
                    result["error"] = str(response.get("error", "执行失败"))[:300]
                if len(json_bytes(result)) > MAX_OUTPUT:
                    raise BrokerError("执行结果超过 16 KiB，请分段读取", 413)
                session.last_used = time.monotonic()
                return result
        except asyncio.CancelledError:
            await asyncio.shield(self.destroy(session_id, session, cancel_active=False))
            raise
        except TimeoutError:
            await asyncio.shield(self.destroy(session_id, session, cancel_active=False))
            raise BrokerError("执行超过 30 秒，隔离会话已删除；未重试操作", 504) from None
        except Exception:
            await asyncio.shield(self.destroy(session_id, session, cancel_active=False))
            raise
        finally:
            if session.active_task is asyncio.current_task():
                session.active_task = None


def read_token(path):
    with Path(path).open("rb") as token_file:
        raw = token_file.read(513)
    if len(raw) > 512:
        raise ValueError("隔离服务密钥文件不正确")
    token = raw.strip().decode("ascii")
    if not 32 <= len(token) <= 256 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("隔离服务密钥至少需要 32 个可打印字符")
    return token


def create_app(*, backend=None, token=None, fetch=None):
    token = token or read_token(os.environ.get("AUTH_TOKEN_FILE", "/run/secrets/assistant_sandbox_token"))
    owner = hashlib.sha256(token.encode()).hexdigest()[:20]
    backend = backend or DockerBackend(os.environ.get("RUNNER_IMAGE", RUNNER_IMAGE), owner=owner)
    broker = Broker(backend, fetch=fetch)

    @asynccontextmanager
    async def lifespan(app):
        await backend.recover()
        async def reaper():
            while True:
                await asyncio.sleep(30)
                try:
                    await broker.reap()
                except Exception:
                    pass  # Broken sessions remain quarantined and count toward the two-session cap.
        worker = asyncio.create_task(reaper())
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await asyncio.gather(*(broker.destroy(key, session) for key, session in list(broker.sessions.items())),
                                 return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.broker = broker
    async def authenticate(request):
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not hmac.compare_digest(values[0].encode(), ("Bearer " + token).encode()):
            raise HTTPException(status_code=401, detail="隔离服务身份验证失败")

    @app.exception_handler(BrokerError)
    async def broker_error(request, exc):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)

    @app.get("/v1/health")
    async def health(request: Request):
        await authenticate(request)
        return {"ok": True, "service": "awbotnest-sandbox", "protocol": 1,
                "network": "broker-public-fetch", "max_sessions": MAX_SESSIONS}

    @app.post("/v1/execute")
    async def execute(request: Request):
        await authenticate(request)
        try:
            async with asyncio.timeout(.05):
                await broker.admission.acquire()
        except TimeoutError:
            raise BrokerError("隔离服务忙，请稍后重试", 429) from None
        try:
            data = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_REQUEST:
                        raise BrokerError("请求超过大小上限", 413)
                    data.extend(chunk)
            try:
                value = json.loads(data)
                if (not isinstance(value, dict) or set(value) != {"session_id", "operation", "arguments", "request_id"}
                        or not isinstance(value["session_id"], str) or not isinstance(value["request_id"], str)):
                    raise ValueError
            except (ValueError, UnicodeError, RecursionError):
                raise BrokerError("请求格式不正确") from None
            # Disconnects cancel only this operation/container. No execution is
            # silently continued and no unknown result is automatically replayed.
            operation = asyncio.create_task(broker.execute(**value))
            async def disconnected():
                while not operation.done():
                    if await request.is_disconnected():
                        operation.cancel()
                        return
                    await asyncio.sleep(.1)
            monitor = asyncio.create_task(disconnected())
            try:
                return await operation
            finally:
                monitor.cancel()
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(monitor, operation, return_exceptions=True)
        finally:
            broker.admission.release()

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        await authenticate(request)
        if not IDENTIFIER.fullmatch(session_id):
            raise BrokerError("会话标识不正确")
        await broker.destroy(session_id)
        return {"session_id": session_id, "deleted": True}
    return app
