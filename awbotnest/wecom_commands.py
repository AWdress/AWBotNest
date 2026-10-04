"""Restricted commands for authenticated WeCom application messages."""
from __future__ import annotations

import asyncio
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
from .wecom_config import callback_members, channel_config, validate_callback_config


logger = logging.getLogger("awbotnest.wecom")
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


class WeComCommandService:
    def __init__(self, settings, runtime, routes, *, store_path: Path | None = None) -> None:
        self.settings, self.runtime, self.routes = settings, runtime, routes
        self.store_path = Path(store_path) if store_path is not None else DATA_DIR / "wecom_commands.sqlite"
        self._queue: asyncio.Queue[CommandJob] = asyncio.Queue(maxsize=16)
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
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
        if user_id not in callback_members(config):
            raise HTTPException(403, "此成员无权执行指令")
        return config

    def _plugins(self, channel_id: str) -> list[str]:
        return sorted(plugin_id for plugin_id, route in self.settings.bot_routing.items()
                      if channel_id in {item.strip() for item in route.split(",")}
                      and plugin_id in self.runtime.loaded)

    def _action_allowed(self, channel_id: str, plugin_id: str, action: str) -> bool:
        return (plugin_id in self._plugins(channel_id)
                and action in self.routes.describe(plugin_id).get("actions", []))

    def _check_job(self, job: CommandJob) -> None:
        if self._closed:
            raise HTTPException(503, "系统正在停止")
        config = self._authorize(job.channel_id, job.user_id)
        application = (config["corpid"], str(config["agentid"]))
        if application != job.application or not self._action_allowed(job.channel_id, job.plugin_id, job.action):
            raise HTTPException(403, "插件动作权限或应用已变更")

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

    async def handle(self, channel_id: str, message: dict[str, str]) -> str:
        user_id = message.get("FromUserName", "")
        config = self._authorize(channel_id, user_id)
        if self._closed:
            raise HTTPException(503, "系统正在停止，请稍后重试")
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
        if command == "/动作" and len(parts) == 2:
            pid = parts[1]
            if pid not in self._plugins(channel_id):
                return "插件未关联此渠道，或尚未启用。"
            return self._limited(f"{self._name(pid)}\n动作：{'、'.join(self.routes.describe(pid)['actions']) or '无'}")
        if command not in {"/运行", "/run"}:
            return HELP
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
        admission = asyncio.create_task(self._submit(
            CommandJob(channel_id, user_id, plugin_id, action, payload,
                       (config["corpid"], str(config["agentid"]))), message_id))
        try:
            fresh = await asyncio.shield(admission)
        except asyncio.CancelledError:
            # A disconnect cannot strand a committed MsgId without its job.
            # Shutdown waits for admission under the same lock, then cancels jobs.
            await asyncio.gather(admission, return_exceptions=True)
            raise
        if not fresh:
            return "此消息已接收，请勿重复提交。"
        return f"已提交：{self._name(plugin_id)} / {action}"

    async def _submit(self, job: CommandJob, message_id: str) -> bool:
        async with self._lock:
            if self._closed or self._queue.full():
                raise HTTPException(503, "系统忙，请稍后重试")
            self._check_job(job)
            try:
                fresh = await asyncio.to_thread(self._claim, job.channel_id, message_id)
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                logger.warning("企业微信指令未提交：去重存储不可用（%s）", type(exc).__name__)
                raise HTTPException(503, "指令暂时无法提交，请稍后重试") from exc
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
                name = self._name(job.plugin_id)
                try:
                    result = await self.routes.dispatch_action(job.plugin_id, job.action, job.payload)
                    failed = result is False or isinstance(result, dict) and result.get("ok") is False
                    detail = result if isinstance(result, str) else (
                        result.get("message", "") if isinstance(result, dict) else "")
                    text = f"{name} / {job.action}：{'执行失败' if failed else '已完成'}"
                    if isinstance(detail, str) and detail:
                        text += "\n" + redact_secrets(detail)
                except Exception as exc:
                    logger.warning("企业微信插件动作失败：插件=%s 动作=%s 错误=%s", name, job.action, type(exc).__name__)
                    text = f"{name} / {job.action}：执行失败（{type(exc).__name__}）"
                # Re-check recipient permissions before delivering a late result.
                self._check_job(job)
                await asyncio.wait_for(self.runtime.notifier.send_wecom_text(
                    job.channel_id, job.user_id, self._limited(text),
                    permission_check=lambda: self._check_job(job)), timeout=30)
            except asyncio.CancelledError:
                raise
            except HTTPException:
                pass  # Permission revoked while the command was queued/running.
            except Exception as exc:
                logger.warning("企业微信指令结果发送失败（%s）", type(exc).__name__)
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            worker = self._worker
            if worker is not None:
                worker.cancel()
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
