"""Management-token-only assistant API. Never mounted in the open API router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionInput(Input):
    title: str = Field(default="新会话", max_length=80)


class MessageInput(Input):
    content: str = Field(min_length=1, max_length=8000)


class ConfirmationInput(Input):
    confirmation_id: str = Field(min_length=1, max_length=64)
    approved: StrictBool


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


def create_router(deps, service):
    router = APIRouter(prefix="/api/assistant", tags=["系统助手"], dependencies=[Depends(deps.require_admin)])

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

    @router.delete("/sessions/{session_id}", summary="删除已结束的会话")
    async def delete_session(session_id: str):
        await call(service.delete_session, session_id)
        return {"ok": True}

    @router.post("/sessions/{session_id}/messages", summary="向助手发送要求")
    async def message(session_id: str, body: MessageInput):
        return await call(service.submit, session_id, body.content)

    @router.get("/runs/{run_id}", summary="读取助手任务")
    async def run(run_id: str):
        return await call(service.run, run_id)

    @router.post("/runs/{run_id}/steer", summary="补充运行中的任务要求")
    async def steer(run_id: str, body: MessageInput):
        return await call(service.steer, run_id, body.content)

    @router.post("/runs/{run_id}/confirm", summary="批准或拒绝本次具体操作")
    async def confirm(run_id: str, body: ConfirmationInput):
        return await call(service.confirm, run_id, body.confirmation_id, body.approved)

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
