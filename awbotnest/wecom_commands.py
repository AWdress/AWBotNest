"""Restricted commands for authenticated WeCom application messages."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from . import __version__
from .config import DATA_DIR
from .logs import redact_secrets
from .wecom_config import (callback_application, callback_member_allowed, channel_config,
                           validate_callback_config, validate_callback_message)
from .wecom_messages import WeComMedia, WeComMessage


logger = logging.getLogger("awbotnest.wecom")
WECOM_SHUTDOWN_SECONDS = 3.0
HELP = ("/状态：查看系统状态\n/插件：查看关联插件及动作\n"
        "/动作 插件ID：查看可用动作\n/运行 插件ID 动作名：执行动作\n"
        "需要参数时，在动作名后附 JSON 对象。")


@dataclass(frozen=True)
class CommandJob:
    channel_id: str
    user_id: str
    plugin_id: str
    action: str
    payload: dict
    application: tuple[str, str]


@dataclass(frozen=True)
class MessageJob:
    channel_id: str
    user_id: str
    message: dict
    targets: tuple[tuple[str, str], ...]
    application: tuple[str, str]


class WeComCommandService:
    def __init__(self, settings, runtime, routes, *, store_path: Path | None = None) -> None:
        self.settings, self.runtime, self.routes = settings, runtime, routes
        self.store_path = Path(store_path) if store_path is not None else DATA_DIR / "wecom_commands.sqlite"
        self._queue: asyncio.Queue[CommandJob | MessageJob] = asyncio.Queue(maxsize=16)
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._admissions: set[asyncio.Task] = set()
        self._closed = False

    def _authorize(self, channel_id: str, user_id: str) -> dict:
        config = channel_config(self.settings, channel_id)
        if (config is None or config.get("enabled", True) is not True
                or config.get("callback_enabled") is not True):
            raise HTTPException(403, "消息回调已停用")
        try:
            validate_callback_config(config)
        except ValueError as exc:
            raise HTTPException(403, "消息回调配置不完整") from exc
        if not callback_member_allowed(config, user_id):
            raise HTTPException(403, "此成员无权执行指令")
        return config

    def _plugins(self, channel_id: str) -> list[str]:
        return sorted(plugin_id for plugin_id, route in self.settings.bot_routing.items()
                      if channel_id in {item.strip() for item in route.split(",")}
                      and plugin_id in self.runtime.loaded)

    def _action_allowed(self, channel_id: str, plugin_id: str, action: str) -> bool:
        return (plugin_id in self._plugins(channel_id)
                and action in self.routes.describe(plugin_id).get("actions", []))

    def _target_allowed(self, job: MessageJob, plugin_id: str, token: str) -> bool:
        lookup = getattr(self.routes, "wecom_handler_token", None)
        return (callable(lookup) and plugin_id in self._plugins(job.channel_id)
                and lookup(plugin_id, job.message.get("MsgType", ""), job.message.get("Event", "")) == token)

    def _check_job(self, job: CommandJob | MessageJob) -> None:
        if self._closed:
            raise HTTPException(503, "系统正在停止")
        config = self._authorize(job.channel_id, job.user_id)
        application = callback_application(config)
        allowed = (any(self._target_allowed(job, pid, token) for pid, token in job.targets)
                   if isinstance(job, MessageJob)
                   else self._action_allowed(job.channel_id, job.plugin_id, job.action))
        if application != job.application or not allowed:
            raise HTTPException(403, "插件动作权限或应用已变更")

    def _check_target(self, job: MessageJob, plugin_id: str, token: str) -> None:
        self._check_job(job)
        if not self._target_allowed(job, plugin_id, token):
            raise HTTPException(403, "插件消息接收权限已变更")

    def _name(self, plugin_id: str) -> str:
        return str(self.runtime.display_name(plugin_id))[:100]

    @staticmethod
    def _limited(text: str, limit: int = 1800) -> str:
        encoded = text.encode("utf-8")
        return text if len(encoded) <= limit else encoded[:limit].decode("utf-8", errors="ignore") + "…"

    def _claim(self, channel_id: str, message_id: str) -> bool:
        # Atomic INSERT makes retries safe even across ASGI processes. No message
        # content, member name or callback secret is written to the database.
        key = hashlib.sha256(f"{channel_id}\0{message_id}".encode()).hexdigest()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.store_path, timeout=0.2)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS received (id TEXT PRIMARY KEY, time REAL NOT NULL)")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM received WHERE time < ?", (time.time() - 86400,))
            if connection.execute("SELECT 1 FROM received WHERE id = ?", (key,)).fetchone():
                return False
            if connection.execute("SELECT count(*) FROM received").fetchone()[0] >= 10000:
                raise RuntimeError("回调去重记录已满")
            connection.execute("INSERT INTO received VALUES (?, ?)", (key, time.time()))
        return True

    def _received(self, channel_id: str, message_id: str) -> bool:
        # A full queue must acknowledge accepted retries without claiming a new
        # message that it cannot enqueue. Read persisted records across restarts.
        if not self.store_path.exists():
            return False
        key = hashlib.sha256(f"{channel_id}\0{message_id}".encode()).hexdigest()
        with closing(sqlite3.connect(self.store_path, timeout=0.2)) as connection:
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'received'").fetchone():
                return False
            return connection.execute("SELECT 1 FROM received WHERE id = ? AND time >= ?",
                                      (key, time.time() - 86400)).fetchone() is not None

    def _release_claim(self, channel_id: str, message_id: str) -> None:
        # Only the admission that inserted a fresh record may undo it, and only
        # before enqueueing. Rejected retries must keep the original record.
        key = hashlib.sha256(f"{channel_id}\0{message_id}".encode()).hexdigest()
        with closing(sqlite3.connect(self.store_path, timeout=0.2)) as connection, connection:
            connection.execute("DELETE FROM received WHERE id = ?", (key,))

    @staticmethod
    def _consume_task(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    def _admission_finished(self, task: asyncio.Task) -> None:
        self._admissions.discard(task)
        self._consume_task(task)

    @staticmethod
    def _message_id(message: dict, application: tuple[str, str]) -> str:
        message_id = message.get("MsgId", "")
        if message.get("MsgType") == "event" and not message_id:
            # Provider retries change the encryption nonce, not the plaintext.
            identity = json.dumps([application, message], sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":"))
            return "event:" + hashlib.sha256(identity.encode()).hexdigest()
        return message_id

    async def handle(self, channel_id: str, message: dict[str, str]) -> str:
        if not isinstance(message, dict):
            raise HTTPException(400, "回调消息字段格式不正确")
        user_id = message.get("FromUserName", "")
        config = self._authorize(channel_id, user_id)
        if self._closed:
            raise HTTPException(503, "系统正在停止，请稍后重试")
        self._validate_message(message, config)
        if message.get("MsgType") != "text":
            return ""
        text = message.get("Content", "").strip()
        parts = text.split(maxsplit=3)
        command = parts[0].lower() if parts else ""
        if command in {"/帮助", "/help", "帮助"}:
            return HELP
        if command in {"/状态", "/status"}:
            accounts = getattr(self.runtime, "accounts", None)
            users = getattr(accounts, "users", {})
            online = sum(bool(client.is_connected()) for client in users.values())
            return f"AWBotNest {__version__}\n在线账号：{online}/{len(users)}\n关联插件：{len(self._plugins(channel_id))}"
        if command in {"/插件", "/plugins"}:
            lines = [f"{self._name(pid)}（{pid}）\n动作：{'、'.join(self.routes.describe(pid)['actions']) or '无'}"
                     for pid in self._plugins(channel_id)]
            return self._limited("\n\n".join(lines) or "此渠道没有已启用的关联插件。")
        if command == "/动作":
            if len(parts) != 2:
                return "用法：/动作 插件ID"
            pid = parts[1]
            if pid not in self._plugins(channel_id):
                return "插件未关联此渠道，或尚未启用。"
            return self._limited(f"{self._name(pid)}\n动作：{'、'.join(self.routes.describe(pid)['actions']) or '无'}")
        if command not in {"/运行", "/run"}:
            return await self.handle_message(channel_id, message, fallback=HELP)
        if len(parts) < 3:
            return "用法：/运行 插件ID 动作名 [JSON 参数]"
        plugin_id, action = parts[1:3]
        if not self._action_allowed(channel_id, plugin_id, action):
            return "动作不可用：请检查插件是否启用、关联此渠道并注册了该动作。"
        payload = {}
        if len(parts) == 4:
            try:
                payload = json.loads(parts[3])
            except (ValueError, RecursionError):
                return "参数必须是 JSON 对象。"
            if not isinstance(payload, dict) or len(parts[3].encode()) > 8192:
                return "参数必须是 JSON 对象，且不能超过 8 KB。"
        message_id = message.get("MsgId", "")
        if not message_id or len(message_id) > 128:
            raise HTTPException(400, "消息 ID 无效")
        fresh = await self._admit(CommandJob(channel_id, user_id, plugin_id, action, payload,
                                            callback_application(config)), message_id)
        if not fresh:
            return "此消息已接收，请勿重复提交。"
        return f"已提交：{self._name(plugin_id)} / {action}"

    async def handle_message(self, channel_id: str, message: dict, *, fallback: str = "") -> str:
        if not isinstance(message, dict):
            raise HTTPException(400, "回调消息字段格式不正确")
        config = self._authorize(channel_id, message.get("FromUserName", ""))
        if self._closed:
            raise HTTPException(503, "系统正在停止，请稍后重试")
        if not self._validate_message(message, config):
            return ""
        lookup = getattr(self.routes, "wecom_handler_token", None)
        targets = []
        if callable(lookup):
            for plugin_id in self._plugins(channel_id):
                token = lookup(plugin_id, message.get("MsgType", ""), message.get("Event", ""))
                if token is not None:
                    targets.append((plugin_id, token))
        if not targets:
            return fallback
        application = callback_application(config)
        message_id = self._message_id(message, application)
        if not isinstance(message_id, str) or not message_id or len(message_id) > 128:
            raise HTTPException(400, "消息 ID 无效")
        job = MessageJob(channel_id, message.get("FromUserName", ""), copy.deepcopy(message),
                         tuple(targets), application)
        await self._admit(job, message_id)
        # The HTTP callback acknowledges promptly; plugins reply separately to
        # the sender only. No default "completed" notification for every image.
        return ""

    @staticmethod
    def _validate_message(message: dict, config: dict) -> bool:
        try:
            return validate_callback_message(message, config)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    async def _admit(self, job: CommandJob | MessageJob, message_id: str) -> bool:
        admission = asyncio.create_task(self._submit(job, message_id))
        self._admissions.add(admission)
        admission.add_done_callback(self._admission_finished)
        try:
            fresh = await asyncio.shield(admission)
        except asyncio.CancelledError:
            # A disconnect cannot strand a committed MsgId without its job.
            # Repeated cancellation must not propagate to the admission either.
            while not admission.done() and not self._closed:
                try:
                    await asyncio.shield(admission)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            raise
        return fresh

    async def _submit(self, job: CommandJob | MessageJob, message_id: str) -> bool:
        async with self._lock:
            self._check_job(job)
            try:
                if self._queue.full():
                    if await asyncio.to_thread(self._received, job.channel_id, message_id):
                        self._check_job(job)
                        return False
                    raise HTTPException(503, "系统忙，请稍后重试")
                fresh = await asyncio.to_thread(self._claim, job.channel_id, message_id)
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                logger.warning("企业微信指令未提交：去重存储不可用（%s）", type(exc).__name__)
                raise HTTPException(503, "指令暂时无法提交，请稍后重试") from exc
            # Shutdown and permission edits do not wait for storage I/O.
            try:
                self._check_job(job)
            except HTTPException:
                if fresh:
                    try:
                        await asyncio.to_thread(self._release_claim, job.channel_id, message_id)
                    except (OSError, sqlite3.Error) as exc:
                        logger.warning("未提交指令的去重记录清理失败（%s）", type(exc).__name__)
                raise
            if not fresh:
                return False
            self._queue.put_nowait(job)
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._run(), name="wecom-commands")
        return True

    async def _run(self) -> None:
        while not self._closed:
            job = await self._queue.get()
            try:
                self._check_job(job)
                if isinstance(job, MessageJob):
                    await self._dispatch_message(job)
                    continue
                name = self._name(job.plugin_id)
                try:
                    # Isolate current_task().cancel() in a plugin callback from
                    # the queue worker. Awaiting directly would run synchronous
                    # callbacks in the worker task; system cancellation still
                    # propagates normally to this child dispatch.
                    dispatch = asyncio.create_task(self.routes.dispatch_action(
                        job.plugin_id, job.action, job.payload), name="wecom-action")
                    result = await dispatch
                    failed = result is False or isinstance(result, dict) and result.get("ok") is False
                    detail = result if isinstance(result, str) else (
                        result.get("message", "") if isinstance(result, dict) else "")
                    text = f"{name} / {job.action}：{'执行失败' if failed else '已完成'}"
                    if isinstance(detail, str) and detail:
                        text += "\n" + redact_secrets(detail)
                except asyncio.CancelledError:
                    if self._closed or asyncio.current_task().cancelling():
                        raise
                    # A plugin may cancel its own action; that must not stop the
                    # worker and abandon other commands already acknowledged.
                    logger.warning("企业微信插件动作已取消：插件=%s 动作=%s", name, job.action)
                    text = f"{name} / {job.action}：已取消"
                except Exception as exc:
                    logger.warning("企业微信插件动作失败：插件=%s 动作=%s 错误=%s", name, job.action, type(exc).__name__)
                    text = f"{name} / {job.action}：执行失败（{type(exc).__name__}）"
                # Re-check recipient permissions before delivering a late result.
                self._check_job(job)
                await asyncio.wait_for(self.runtime.notifier.send_wecom_text(
                    job.channel_id, job.user_id, self._limited(text),
                    permission_check=lambda: self._check_job(job)), timeout=30)
            except asyncio.CancelledError:
                if self._closed or asyncio.current_task().cancelling():
                    raise
                logger.warning("企业微信指令结果发送已取消")
            except HTTPException:
                pass  # Permission revoked while the command was queued/running.
            except Exception as exc:
                logger.warning("企业微信指令结果发送失败（%s）", type(exc).__name__)
            finally:
                self._queue.task_done()

    async def _dispatch_message(self, job: MessageJob) -> None:
        for plugin_id, token in job.targets:
            try:
                self._check_target(job, plugin_id, token)
            except HTTPException:
                continue

            def permission_check(pid=plugin_id, generation=token):
                self._check_target(job, pid, generation)

            async def reply(text: str, check=permission_check):
                check()
                if not isinstance(text, str) or not text:
                    raise ValueError("企业微信回复内容不能为空")
                return await asyncio.wait_for(self.runtime.notifier.send_wecom_text(
                    job.channel_id, job.user_id, self._limited(redact_secrets(text)),
                    permission_check=check), timeout=30)

            async def download(max_bytes: int, check=permission_check):
                check()
                media = await self.runtime.notifier.download_wecom_media(
                    job.channel_id, job.user_id, job.message.get("MediaId", ""),
                    permission_check=check, max_bytes=max_bytes)
                check()
                return WeComMedia(content=media["content"], filename=media["filename"],
                                  content_type=media["content_type"])

            fields = copy.deepcopy(job.message)
            file_size = fields.get("FileSize")
            message = WeComMessage(channel_id=job.channel_id, user_id=job.user_id,
                corp_id=job.application[0], agent_id=job.application[1],
                message_id=self._message_id(fields, job.application), create_time=int(fields.get("CreateTime", "0")),
                message_type=fields.get("MsgType", ""), text=fields.get("Content", ""),
                media_id=fields.get("MediaId", ""), pic_url=fields.get("PicUrl", ""),
                file_name=fields.get("FileName", ""), file_size=int(file_size) if file_size else None,
                event=fields.get("Event", ""), event_key=fields.get("EventKey", ""), fields=fields,
                _reply=reply, _download=download)
            try:
                async def invoke(pid=plugin_id, generation=token, event=message, check=permission_check):
                    check()
                    return await self.routes.dispatch_wecom_message(pid, event, expected_token=generation)

                dispatch = asyncio.create_task(invoke(), name="wecom-message")
                result = await dispatch
                if isinstance(result, str) and result:
                    await reply(result)
            except asyncio.CancelledError:
                if self._closed or asyncio.current_task().cancelling():
                    raise
                logger.warning("企业微信插件消息处理已取消：插件=%s", self._name(plugin_id))
            except HTTPException:
                pass
            except Exception as exc:
                logger.warning("企业微信插件消息处理失败：插件=%s 错误=%s",
                               self._name(plugin_id), type(exc).__name__)

    async def close(self) -> None:
        first_close = not self._closed
        self._closed = True
        worker = self._worker
        if worker is not None:
            worker.cancel()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        tasks = set(self._admissions)
        if worker is not None:
            tasks.add(worker)
        if tasks:
            # wait_for/gather may wait forever for a plugin that suppresses
            # cancellation. Closed state blocks queued work and late replies.
            done, pending = await asyncio.wait(tasks, timeout=WECOM_SHUTDOWN_SECONDS)
            for task in done:
                self._consume_task(task)
            for task in pending:
                task.add_done_callback(self._consume_task)
            if pending and first_close:
                logger.warning("企业微信指令清理超时：%s 个任务未退出", len(pending))
