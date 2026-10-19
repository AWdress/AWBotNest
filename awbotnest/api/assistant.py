"""Management-token-only assistant API. Never mounted in the open API router."""
from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..auth import bearer


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionInput(Input):
    title: str = Field(default="新会话", max_length=80)


class TextAttachmentInput(Input):
    name: str = Field(min_length=1, max_length=200, description="不含路径的 UTF-8 文本文件名")
    data: str = Field(min_length=1, max_length=87384, description="标准 Base64 文件内容，不带 data URL 前缀")
    content_type: str = Field(default="text/plain", max_length=100)


class AudioInput(Input):
    name: str = Field(min_length=1, max_length=200, description="不含路径的音频文件名")
    data: str = Field(min_length=1, max_length=11184812, description="最多 8 MiB 音频的标准 Base64 内容")
    content_type: str = Field(default="", max_length=100)


class MessageInput(Input):
    content: str = Field(default="", max_length=8000)
    images: list[Annotated[str, Field(max_length=6990572)]] = Field(default_factory=list, max_length=3)
    attachments: list[TextAttachmentInput] = Field(default_factory=list, max_length=3)
    audio: AudioInput | None = None


class ConfirmationInput(Input):
    confirmation_id: str = Field(min_length=1, max_length=64)
    approved: StrictBool


class AnswerInput(Input):
    input_id: str = Field(min_length=1, max_length=64)
    answer: str = Field(min_length=1, max_length=2000)


class ResourceInput(Input):
    id: str | None = Field(default=None, max_length=64)
    content: str = Field(min_length=1, max_length=12000)


class SkillInput(ResourceInput):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)


class TaskInput(Input):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=2000)
    run_at: str | None = Field(default=None, max_length=100)
    cron: str | None = Field(default=None, max_length=100)


class ToggleInput(Input):
    enabled: StrictBool


class PairInput(Input):
    kind: str = Field(pattern="^(telegram|wecom)$")
    channel_id: str = Field(min_length=1, max_length=128)


class MCPInput(Input):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    enabled: StrictBool = False
    token: str | None = Field(default=None, max_length=4096)
    read_only_tools: list[str] = Field(default_factory=list, max_length=32)


class SandboxInput(Input):
    enabled: StrictBool = False
    url: str = Field(default="http://assistant-sandbox:8765", max_length=2048)
    token: str | None = Field(default=None, max_length=4096)
    clear_token: StrictBool = False


def create_router(deps, service):
    uploads = asyncio.Semaphore(2)
    class BoundedRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()
            async def bounded(request: Request):
                if request.method in {"POST", "PUT"}:
                    # Authenticate before buffering attachments, not only in
                    # FastAPI's later dependency phase.
                    credentials = await bearer(request)
                    await deps.require_admin(credentials=credentials)
                    # Bound actual streamed bytes too; Content-Length may be
                    # absent or forged. Keep native schema/OpenAPI validation.
                    maximum = 12 * 1024 * 1024 if request.url.path.endswith(("/messages", "/steer")) else 256 * 1024
                    try:
                        async with asyncio.timeout(30):
                            async with uploads:
                                body = bytearray()
                                async for chunk in request.stream():
                                    if len(body) + len(chunk) > maximum:
                                        raise HTTPException(413, "附件或请求内容过大，请精简后再发送")
                                    body.extend(chunk)
                                request._body = bytes(body)
                                return await handler(request)
                    except TimeoutError:
                        raise HTTPException(408, "上传超时，请重新发送") from None
                return await handler(request)
            return bounded
    router = APIRouter(prefix="/api/assistant", tags=["系统助手"], dependencies=[Depends(deps.require_admin)],
                       route_class=BoundedRoute)

    async def call(callback, *args, **kwargs):
        try:
            await service.start()
            return await callback(*args, **kwargs)
        except KeyError:
            raise HTTPException(404, "记录不存在") from None
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, service.clean(str(exc), 500)) from None
        except RuntimeError:
            raise HTTPException(503, "助手暂不可用，请稍后重试") from None

    @router.get("", summary="读取助手设置、会话和预约")
    async def overview():
        return await call(service.overview)

    @router.put("/settings", summary="修改助手权限与用量限制")
    async def settings(values: dict):
        return await call(service.update_settings, values)

    @router.post("/sessions", summary="新建助手会话")
    async def new_session(body: SessionInput):
        return await call(service.new_session, body.title)

    @router.get("/sessions/{session_id}", summary="读取会话与执行进度")
    async def session(session_id: str):
        return await call(service.session, session_id)

    @router.get("/sessions/{session_id}/events", summary="订阅助手实时文字、进度和交互状态")
    async def events(session_id: str, request: Request):
        await call(service.session, session_id)
        credentials = await bearer(request)
        try:
            queue = service.subscribe(session_id)
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from None
        async def stream():
            try:
                initial = await service.session(session_id)
                try:
                    await deps.require_admin(credentials=credentials)
                except HTTPException:
                    return
                yield "event: snapshot\ndata: " + json.dumps(initial, ensure_ascii=False) + "\n\n"
                while not service._closed and not await request.is_disconnected():
                    try:
                        run = await asyncio.wait_for(queue.get(), 15)
                    except TimeoutError:
                        try:
                            await deps.require_admin(credentials=credentials)
                        except HTTPException:
                            return
                        yield ": keepalive\n\n"
                        continue
                    try:
                        await deps.require_admin(credentials=credentials)
                    except HTTPException:
                        return
                    yield "event: run\ndata: " + json.dumps(run, ensure_ascii=False) + "\n\n"
                    if run["status"] not in {"queued", "running", "waiting_confirmation", "waiting_input"}:
                        final = await service.session(session_id)
                        try:
                            await deps.require_admin(credentials=credentials)
                        except HTTPException:
                            return
                        yield "event: snapshot\ndata: " + json.dumps(final, ensure_ascii=False) + "\n\n"
            finally:
                service.unsubscribe(session_id, queue)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})

    @router.delete("/sessions/{session_id}", summary="删除已结束的会话")
    async def delete_session(session_id: str):
        await call(service.delete_session, session_id)
        return {"ok": True}

    @router.post("/sessions/{session_id}/messages", summary="向助手发送要求")
    async def message(session_id: str, body: MessageInput):
        from ..assistant_attachments import normalize_attachments
        from ..assistant_audio import normalize_audio
        async def submit():
            files = normalize_attachments([item.model_dump() for item in body.attachments])
            audio = normalize_audio(body.audio.model_dump() if body.audio else None)
            return await service.submit(session_id, body.content, images=body.images, attachments=files, audio=audio)
        return await call(submit)

    @router.get("/runs/{run_id}", summary="读取助手任务")
    async def run(run_id: str):
        return await call(service.run, run_id)

    @router.post("/runs/{run_id}/steer", summary="补充运行中的任务要求")
    async def steer(run_id: str, body: MessageInput):
        if body.images or body.attachments or body.audio:
            raise HTTPException(400, "任务执行中暂不接受附件补充，请等待完成后发送")
        return await call(service.steer, run_id, body.content)

    async def channels():
        target = getattr(service, "channels", None)
        if target is None:
            raise HTTPException(503, "消息渠道尚未就绪")
        return target

    @router.get("/channels", summary="读取助手渠道和已配对管理员")
    async def channel_status():
        target = await channels()
        return await call(target.status)

    @router.post("/channels/pair", summary="生成一次性管理员渠道配对码")
    async def channel_pair(body: PairInput):
        target = await channels()
        return await call(target.create_pair_code, body.kind, body.channel_id)

    @router.delete("/channels/{binding_id}", summary="撤销渠道管理员配对")
    async def channel_revoke(binding_id: str):
        target = await channels()
        await call(target.revoke, binding_id)
        return {"ok": True}

    @router.get("/mcp/servers", summary="读取管理员配置的 MCP 服务（不返回凭据）")
    async def mcp_servers():
        return await call(service.mcp.list_servers)

    @router.get("/sandbox", summary="读取隔离沙箱配置（不连接、不返回凭据）")
    async def sandbox_status():
        return await call(service.sandbox.status)

    @router.put("/sandbox/config", summary="保存独立沙箱连接设置")
    async def sandbox_config(body: SandboxInput):
        return await call(service.sandbox.save_config, body.model_dump(exclude_none=True))

    @router.post("/sandbox/check", summary="显式检查隔离沙箱执行服务")
    async def sandbox_check():
        return await call(service.sandbox.check)

    @router.get("/sandbox/artifacts/{artifact_id}", summary="读取未过期的沙箱截图（仅内存）",
                response_class=Response, responses={200: {"content": {
                    "image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}})
    async def sandbox_artifact(artifact_id: str):
        data, mime_type = await call(service.sandbox.artifact, artifact_id)
        return Response(data, media_type=mime_type, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/mcp/servers", summary="添加外部 MCP 服务")
    async def mcp_add(body: MCPInput):
        return await call(service.mcp.save_server, body.model_dump(exclude_none=True))

    @router.put("/mcp/servers/{server_id}", summary="修改 MCP 服务并使旧确认失效")
    async def mcp_update(server_id: str, body: MCPInput):
        return await call(service.mcp.save_server, body.model_dump(exclude_none=True), server_id)

    @router.delete("/mcp/servers/{server_id}", summary="移除 MCP 服务")
    async def mcp_delete(server_id: str):
        return await call(service.mcp.delete_server, server_id)

    @router.post("/mcp/servers/{server_id}/discover", summary="连接 MCP 服务并读取工具目录")
    async def mcp_discover(server_id: str):
        return await call(service.mcp.discover, server_id)

    @router.post("/runs/{run_id}/confirm", summary="批准或拒绝本次具体操作")
    async def confirm(run_id: str, body: ConfirmationInput):
        return await call(service.confirm, run_id, body.confirmation_id, body.approved)

    @router.post("/runs/{run_id}/answer", summary="回答本次助手追问（不批准修改操作）")
    async def answer(run_id: str, body: AnswerInput):
        return await call(service.answer, run_id, body.input_id, body.answer)

    @router.post("/runs/{run_id}/cancel", summary="取消等待或执行中的助手任务")
    async def cancel(run_id: str):
        return await call(service.cancel, run_id)

    @router.post("/memories", summary="保存助手偏好")
    async def memory(body: ResourceInput):
        return await call(service.save_resource, "memory", body.model_dump(exclude_none=True))

    @router.post("/skills", summary="保存可复用的办事方法")
    async def skill(body: SkillInput):
        return await call(service.save_resource, "skill", body.model_dump(exclude_none=True))

    @router.get("/skills/{item_id}", summary="按需读取内置或自定义技能")
    async def get_skill(item_id: str):
        return await call(service.skill, item_id)

    @router.delete("/memories/{item_id}", summary="删除一条记忆")
    async def delete_memory(item_id: str):
        await call(service.store.delete, "memory", item_id)
        return {"ok": True}

    @router.delete("/skills/{item_id}", summary="删除一项技能")
    async def delete_skill(item_id: str):
        await call(service.delete_skill, item_id)
        return {"ok": True}

    @router.post("/tasks", summary="创建一次性或 Cron 助手预约")
    async def task(body: TaskInput):
        return await call(service.save_task, body.model_dump(exclude_none=True))

    @router.put("/tasks/{task_id}", summary="暂停或恢复预约")
    async def toggle(task_id: str, body: ToggleInput):
        return await call(service.toggle_task, task_id, body.enabled)

    @router.delete("/tasks/{task_id}", summary="删除预约（不取消已开始的任务）")
    async def delete_task(task_id: str):
        async def remove():
            async with service._lock:
                await service.store.delete("task", task_id)
        await call(remove)
        return {"ok": True}

    return router
