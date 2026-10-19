"""Unprivileged, network-less worker. Its socket is never mounted on the host."""
from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import signal
import stat
import time
from pathlib import Path
from uuid import uuid4

from sandbox.contracts import (MAX_FILE, MAX_OUTPUT, MAX_REQUEST, MAX_WIRE,
                               arguments_for, bounded_result, json_bytes, path_parts)

WORKSPACE = "/workspace"
SOCKET = "/tmp/awbotnest-sandbox.sock"


class Workspace:
    """All traversals use directory descriptors and O_NOFOLLOW, including races."""
    def __init__(self, root=WORKSPACE):
        self.root = root

    def directory(self, parts, *, create=False):
        current = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = next_fd
            return current
        except BaseException:
            os.close(current)
            raise

    def read(self, args):
        parts = path_parts(args["path"])
        parent = self.directory(parts[:-1])
        try:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                    raise ValueError("只能读取不超过 64 KiB 的普通文件")
                data = bytearray()
                while len(data) <= MAX_FILE:
                    chunk = os.read(descriptor, min(8192, MAX_FILE + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > MAX_FILE:
                    raise ValueError("文件超过 64 KiB")
                text = bytes(data).decode("utf-8")
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        offset, width = args["offset"], args["limit"]
        while True:
            end = min(len(text), offset + width)
            result = {"path": args["path"], "content": text[offset:end], "offset": offset,
                      "total": len(text), "next_offset": end if end < len(text) else None,
                      "complete": end >= len(text)}
            if len(json_bytes(result)) <= MAX_OUTPUT - 1024:
                return result
            width = max(1, width // 2)

    def write(self, args):
        parts = path_parts(args["path"])
        data = args["content"].encode("utf-8")
        if len(data) > MAX_FILE:
            raise ValueError("文件不能超过 64 KiB")
        parent = self.directory(parts[:-1], create=True)
        try:
            # Open without truncating until the final descriptor is proven to
            # be a regular file. Neither symlinks nor FIFO/device nodes work.
            descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 mode=0o600, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("只能写入普通文件")
                os.ftruncate(descriptor, 0)
                with os.fdopen(descriptor, "wb", closefd=False) as output:
                    output.write(data)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        return {"path": args["path"], "bytes": len(data), "written": True}

    def list(self, args):
        descriptor = self.directory(path_parts(args["path"], root=True))
        items, truncated = [], False
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if len(items) >= 60:
                        truncated = True
                        break
                    if len(entry.name.encode("utf-8")) > 240:
                        continue
                    item = {"name": entry.name, "symlink": entry.is_symlink(),
                            "directory": entry.is_dir(follow_symlinks=False)}
                    if len(json_bytes({"items": [*items, item]})) > MAX_OUTPUT - 2048:
                        truncated = True
                        break
                    items.append(item)
        finally:
            os.close(descriptor)
        return {"path": args["path"], "items": sorted(items, key=lambda row: row["name"]), "truncated": truncated}


def process_identities():
    """Linux PID/start-time pairs; PID reuse must not hide a new process."""
    identities = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            value = (entry / "stat").read_text(encoding="utf-8")
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue  # Process exited during the bounded /proc scan.
            raise RuntimeError("无法检查执行进程") from None
        fields = value[value.rfind(")") + 2:].split()
        if len(fields) < 20 or not fields[19].isdecimal():
            raise RuntimeError("执行进程信息不正确")
        identities.add((int(entry.name), int(fields[19])))
    if not identities:
        raise RuntimeError("无法检查执行进程")
    return identities


async def run_process(operation, args, *, cwd=WORKSPACE):
    # This includes the persistent runner/browser and the current fixed RPC
    # client, but not user subprocesses. Inspection failure is fail-closed.
    baseline = process_identities()
    command = ["python3", "-I", "-c", args["code"]] if operation == "python" else ["/bin/sh", "-c", args["command"]]
    process = await asyncio.create_subprocess_exec(*command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": WORKSPACE, "TMPDIR": "/tmp",
             "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8"})
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    used, truncated = 0, False
    async def collect(name, stream):
        nonlocal used, truncated
        while chunk := await stream.read(4096):
            remaining = max(0, 7000 - used)
            outputs[name].extend(chunk[:remaining])
            used += min(len(chunk), remaining)
            truncated |= len(chunk) > remaining
    try:
        async with asyncio.timeout(27):
            await asyncio.gather(collect("stdout", process.stdout), collect("stderr", process.stderr), process.wait())
    finally:
        # Kill the original process group. Code can deliberately call setsid;
        # a separate /proc comparison below detects those remaining processes
        # and requires the broker to destroy the whole session, not reuse it.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            await process.wait()
    result = {key: bytes(value).decode("utf-8", errors="replace") for key, value in outputs.items()}
    result.update(exit_code=process.returncode, truncated=truncated)
    try:
        residual = bool(process_identities() - baseline)
    except (OSError, RuntimeError, ValueError):
        residual = True
    if residual:
        # Do not attempt selective PID killing: a recycled PID or late child
        # could race it. Only Docker removal reliably ends this PID namespace.
        result["cleanup_required"] = True
    while len(json_bytes(result)) > MAX_OUTPUT - 1024:
        for key in ("stdout", "stderr"):
            result[key] = result[key][:len(result[key]) // 2]
        result["truncated"] = True
    return result


class Browser:
    def __init__(self):
        self.playwright = self.browser = self.context = self.page = None
        self.job = None
        self.task = None
        self.pending = {}

    async def initialize(self):
        if self.page is not None:
            return
        from playwright.async_api import async_playwright
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True,
                args=["--disable-dev-shm-usage", "--disable-gpu", "--disable-background-networking"])
            self.context = await self.browser.new_context(service_workers="block", accept_downloads=False,
                                                         viewport={"width": 640, "height": 480})
            await self.context.route("**/*", self.route)
            async def close_websocket(socket):
                await socket.close(code=1008, reason="WebSocket disabled")
            await self.context.route_web_socket("**/*", close_websocket)
            self.page = await self.context.new_page()
            client = await self.context.new_cdp_session(self.page)
            await client.send("Network.enable")
            await client.send("Network.setBlockedURLs", {"urls": ["file:*", "ftp:*", "ws:*", "wss:*"]})
            async def cancel_download(download):
                await download.cancel()
            self.page.on("download", cancel_download)
            # Only one controlled page is supported. Close new tabs/popups
            # rather than permit pages without the main page's CDP guard.
            async def close_extra_page(page):
                if page is not self.page:
                    await page.close()
            self.context.on("page", close_extra_page)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        browser, playwright = self.browser, self.playwright
        self.playwright = self.browser = self.context = self.page = None
        for target, method in ((browser, "close"), (playwright, "stop")):
            if target is not None:
                try:
                    async with asyncio.timeout(2):
                        await getattr(target, method)()
                except (Exception, asyncio.CancelledError):
                    pass

    async def route(self, route):
        request = route.request
        if (self.job is None or self.job.get("done") or not self.job.get("network_allowed")
                or not request.url.startswith(("https://", "http://"))
                or request.method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                or len(self.pending) >= 8):
            await route.abort("blockedbyclient")
            return
        body = request.post_data_buffer or b""
        if len(body) > MAX_FILE:
            await route.abort("blockedbyclient")
            return
        job = self.job
        headers = await request.all_headers()
        if self.job is not job or job.get("done") or len(self.pending) >= 8:
            await route.abort("blockedbyclient")
            return
        fetch_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[fetch_id] = {"future": future, "sent": False, "request": {
            "fetch_id": fetch_id, "url": request.url, "method": request.method,
            "headers": headers, "body_base64": base64.b64encode(body).decode()}}
        try:
            result = await future
            if result.get("error"):
                await route.abort("failed")
            else:
                await route.fulfill(status=result["status"], headers=result["headers"],
                                    body=base64.b64decode(result["body_base64"], validate=True))
        except Exception:
            try:
                await route.abort("failed")
            except Exception:
                pass
        finally:
            self.pending.pop(fetch_id, None)

    async def start(self, args):
        if self.task is not None and not self.task.done():
            raise ValueError("浏览器操作尚未完成")
        self.job = {"job_id": uuid4().hex, "done": False,
                    "network_allowed": args["action"] not in {"snapshot", "screenshot"}}
        self.task = asyncio.create_task(self.action(args, self.job))
        return {"job_id": self.job["job_id"]}

    async def action(self, args, job):
        initialized = False
        try:
            async with asyncio.timeout(26):
                await self.initialize()
                initialized = True
                action = args["action"]
                if action == "goto":
                    await self.page.goto(args["url"], wait_until="load", timeout=25000)
                    result = {"url": self.page.url, "title": (await self.page.title())[:300]}
                elif action == "set_content":
                    await self.page.set_content(args["html"], wait_until="load", timeout=25000)
                    result = {"loaded": True}
                elif action == "snapshot":
                    text = (await self.page.locator("body").inner_text(timeout=3000))[:6000]
                    elements = await self.page.evaluate("""() => Array.from(document.querySelectorAll('a,button,input,textarea,select,[role=button]')).slice(0,35).map((el,i) => {el.setAttribute('data-awbn-ref',String(i+1));return {selector:'[data-awbn-ref="'+(i+1)+'"]',tag:el.tagName.toLowerCase(),text:(el.innerText||el.getAttribute('aria-label')||el.getAttribute('placeholder')||'').slice(0,120)}})""")
                    result = {"url": self.page.url, "title": (await self.page.title())[:300], "text": text, "elements": elements}
                    while len(json_bytes(result)) > MAX_OUTPUT - 1024 and result["text"]:
                        result["text"] = result["text"][:len(result["text"]) // 2]
                        result["truncated"] = True
                elif action in {"click", "fill", "select"}:
                    locator = self.page.locator(args["selector"])
                    if action == "click":
                        await locator.click(timeout=10000)
                    elif action == "fill":
                        await locator.fill(args["text"], timeout=10000)
                    else:
                        await locator.select_option(args["value"], timeout=10000)
                    result = {"action": action, "url": self.page.url, "completed": True}
                elif action == "evaluate":
                    result = {"value": await self.page.evaluate(args["script"])}
                else:
                    image = b""
                    for quality in (40, 20, 10):
                        image = await self.page.screenshot(type="jpeg", quality=quality, timeout=5000)
                        if len(image) <= 11000:
                            break
                    if len(image) > 11000:
                        raise ValueError("截图超过输出上限，请缩小页面内容")
                    result = {"mime_type": "image/jpeg", "data_base64": base64.b64encode(image).decode()}
                job["response"] = bounded_result({"ok": True, "result": result})
        except TimeoutError:
            job["response"] = {"ok": False, "error": "浏览器操作超时", "timed_out": True}
        except Exception as exc:
            # Playwright exceptions can include full page text/URLs. Return
            # only a local category, never the raw exception payload.
            job["response"] = {"ok": False, "error": "浏览器操作失败（" + type(exc).__name__ + "）",
                               "timed_out": type(exc).__name__ == "TimeoutError",
                               "cleanup_required": not initialized}
        finally:
            job["done"] = True
            for item in list(self.pending.values()):
                if not item["future"].done():
                    item["future"].set_result({"error": "操作已结束"})

    async def tick(self, job_id, responses):
        if self.job is None or self.job["job_id"] != job_id:
            raise ValueError("浏览器任务不存在")
        if not isinstance(responses, list) or len(responses) > 8:
            raise ValueError("浏览器回填响应不正确")
        for result in responses:
            if not isinstance(result, dict) or not isinstance(result.get("fetch_id"), str):
                raise ValueError("浏览器回填标识不正确")
            item = self.pending.get(result["fetch_id"])
            if item is not None and not item["future"].done():
                item["future"].set_result(result)
        await asyncio.sleep(0)
        requests = []
        if not self.job["done"]:
            for item in self.pending.values():
                if not item["sent"]:
                    item["sent"] = True
                    requests.append(item["request"])
        return {"job_id": job_id, "done": self.job["done"], "response": self.job.get("response"),
                "pending_requests": requests}


class Runner:
    def __init__(self, workspace=None, browser=None):
        self.workspace = workspace or Workspace()
        self.browser = browser or Browser()
        self.lock = asyncio.Lock()

    async def dispatch(self, value):
        method = value.get("method")
        if method == "tick":
            return await self.browser.tick(value.get("job_id"), value.get("responses", []))
        if method != "execute":
            raise ValueError("不支持此请求")
        operation = value.get("operation")
        args = arguments_for(operation, value.get("arguments"))
        if self.lock.locked():
            raise ValueError("执行环境忙，请等待本次操作结束")
        async with self.lock:
            if operation == "browser":
                return await self.browser.start(args)
            if self.browser.task is not None and not self.browser.task.done():
                raise ValueError("浏览器操作尚未结束")
            if operation in {"python", "shell"}:
                try:
                    result = await run_process(operation, args, cwd=self.workspace.root)
                except TimeoutError:
                    return {"ok": False, "error": "执行超时", "timed_out": True}
                except (OSError, RuntimeError):
                    return {"ok": False, "error": "执行进程检查失败，隔离会话需要关闭", "cleanup_required": True}
                if result.pop("cleanup_required", False):
                    return {"ok": False, "error": "检测到残留进程，隔离会话需要关闭", "cleanup_required": True}
            else:
                result = getattr(self.workspace, {"read_file": "read", "write_file": "write", "list_files": "list"}[operation])(args)
            return bounded_result({"ok": True, "result": result})


async def serve():
    runner = Runner()
    Path(SOCKET).unlink(missing_ok=True)
    async def handle(reader, writer):
        try:
            async with asyncio.timeout(29):
                raw = await reader.readuntil(b"\n")
                if len(raw) > MAX_WIRE:
                    raise ValueError("请求过长")
                result = await runner.dispatch(json.loads(raw))
            encoded = json_bytes(result)
            if len(encoded) > MAX_WIRE:
                raise ValueError("内部响应过长")
        except Exception as exc:
            encoded = json_bytes({"ok": False, "error": "执行失败（" + type(exc).__name__ + "）"})
        writer.write(encoded + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_unix_server(handle, SOCKET, limit=MAX_WIRE + 1)
    os.chmod(SOCKET, 0o600)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve())
