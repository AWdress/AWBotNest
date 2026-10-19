"""Explicit administrator pairing and bounded assistant channel delivery.

Notification allowlists are not administrator grants. Bindings belong to one
provider application and sender; generic plugin messages remain untouched.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import secrets
import time
import weakref
from datetime import datetime, timezone

from .assistant_attachments import (MAX_FILE_BYTES, is_text_filename,
                                    parse_text_attachment)
from .assistant_audio import MAX_AUDIO_BYTES, is_audio_filename, parse_audio
from .wecom_config import (callback_application, callback_member_allowed,
                           channel_config, validate_callback_config,
                           validate_callback_message)

log = logging.getLogger("awbotnest.assistant_channels")
ACTIVE = {"queued", "running", "waiting_confirmation", "waiting_input"}
MAX_IMAGE = 5 * 1024 * 1024
HELP = ("助手已开启。可以发送文字、图片、文本文件或语音。\n/新会话：开始新的对话\n"
        "/停止：停止当前任务\n/退出助手：恢复普通插件消息")
COMMANDS = {"/助手", "/退出助手", "/新会话", "/停止"}
RETIRED_DECISIONS = {"/确认", "/拒绝", "/选择", "/回答"}
BUTTON_PREFIX = b"awbn:assistant:"
BUTTON_DATA = re.compile(r"awbn:assistant:([cq]):([0-9a-f]{8}):(yes|no|[0-5])")


class ChannelDisconnected(ConnectionError):
    """A known, pre-send transport outage, not an authorization change."""


def _id(kind, channel, user):
    return hashlib.sha256(f"{kind}\0{channel}\0{user}".encode()).hexdigest()


def _image(content):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_IMAGE:
        raise ValueError("图片不能超过 5 MiB")
    if content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("仅支持 JPEG、PNG、WebP 和 GIF 图片")
    return f"data:{mime};base64," + base64.b64encode(content).decode("ascii")


class AssistantChannels:
    def __init__(self, deps, assistant):
        self.deps, self.assistant, self.store = deps, assistant, assistant.store
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._codes = {}
        self._bindings = {}
        self._bots = {}
        self._queue = asyncio.Queue(maxsize=16)
        self._workers = []
        self._watchers = {}
        self._delivery_wait_seconds = 60
        # Waiting/in-flight handlers retain their lock; old revoked senders do not.
        self._peer_locks = weakref.WeakValueDictionary()
        self._bot_hook = self.attach_bot
        # Installed before Telegram startup and before plugin handlers.
        if hasattr(deps.accounts, "on_bot_ready"):
            deps.accounts.on_bot_ready = self._bot_hook

    async def start(self):
        async with self._lock:
            if self._closed:
                raise RuntimeError("助手渠道已停止")
            if self._started:
                return
            self._bindings = {item["id"]: item for item in await self.store.list("channel_binding", 32)}
            self._workers = [asyncio.create_task(self._work(), name="assistant-channel") for _ in range(2)]
            self._started = True
        for channel, client in getattr(self.deps.accounts, "bots", {}).copy().items():
            await self.attach_bot(channel, client)

    def _channels(self):
        values = [{"kind": "telegram", "id": str(spec.id), "name": str(spec.name or spec.id)}
                  for spec in getattr(self.deps.settings, "bot_specs", lambda: [])() if spec.token]
        for raw in getattr(self.deps.settings, "notification_channels", []):
            config = channel_config(self.deps.settings, str(raw.get("id") or ""))
            if config and config.get("enabled", True) is True and config.get("callback_enabled") is True:
                try:
                    validate_callback_config(config)
                except ValueError:
                    continue
                values.append({"kind": "wecom", "id": str(raw["id"]),
                               "name": str(raw.get("name") or raw["id"])})
        return values

    async def status(self):
        await self.start()
        return {"bindings": [{k: item.get(k) for k in
                ("id", "kind", "channel_id", "user_id", "enabled", "session_id", "created_at")}
                for item in self._bindings.values()], "channels": self._channels()}

    async def create_pair_code(self, kind, channel_id):
        await self.start()
        if not any(item["kind"] == kind and item["id"] == channel_id for item in self._channels()):
            raise ValueError("请选择已配置的 Bot 或已启用回调的企业微信应用")
        async with self._lock:
            current = time.time()
            self._codes = {key: value for key, value in self._codes.items() if value[2] > current}
            if len(self._codes) >= 16:
                raise ValueError("未使用的配对码较多，请稍后再试")
            code = secrets.token_urlsafe(12)
            expires = current + 600
            self._codes[hashlib.sha256(code.encode()).hexdigest()] = (kind, channel_id, expires)
        return {"code": code, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                "command": f"/配对 {code}"}

    async def revoke(self, binding_id):
        await self.start()
        async with self._peer_locks.setdefault(binding_id, asyncio.Lock()):
            async with self._lock:
                item = self._bindings.pop(binding_id, None)
                await self.store.delete("channel_binding", binding_id)
            if item:
                tasks = [task for key, task in list(self._watchers.items()) if key[0] == binding_id]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await self._stop(item)

    def _live(self, item, *, require_connected=True):
        current = self._bindings.get(item["id"])
        if self._closed or not current or current.get("generation") != item.get("generation"):
            raise PermissionError("助手绑定已撤销")
        if item["kind"] == "telegram":
            bot = self._bots.get(item["channel_id"])
            if not bot or bot[1] != item["identity"]:
                raise PermissionError("Bot 身份已变更")
            if not any(spec.id == item["channel_id"] and spec.token for spec in self.deps.settings.bot_specs()):
                raise PermissionError("Bot 已移除")
            if require_connected and not bot[0].is_connected():
                raise ChannelDisconnected("Bot 连接正在恢复")
        else:
            config = channel_config(self.deps.settings, item["channel_id"])
            if (not config or config.get("enabled", True) is not True
                    or config.get("callback_enabled") is not True
                    or not callback_member_allowed(config, item["user_id"])):
                raise PermissionError("企业微信成员或回调权限已变更")
            validate_callback_config(config)
            if list(callback_application(config)) != item["identity"]:
                raise PermissionError("企业微信应用身份已变更")
        return current

    async def _wait_live(self, item, *, deadline=None):
        """Wait only before a send has been attempted; never replay unknown sends."""
        deadline = deadline if deadline is not None else time.monotonic() + self._delivery_wait_seconds
        while True:
            try:
                return self._live(item)
            except ChannelDisconnected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Bot 连接尚未恢复，请在网页查看任务记录") from None
                await asyncio.sleep(min(.25, remaining))

    async def _send(self, item, text, *, buttons=None):
        # Split on UTF-8 bytes, never silently truncate an approval preview.
        encoded = str(text).encode("utf-8")
        if len(encoded) > 64000:
            raise ValueError("回复过长，请在网页中查看")
        chunks = []
        while encoded:
            part = encoded[:1700].decode("utf-8", errors="ignore")
            chunks.append(part)
            encoded = encoded[len(part.encode("utf-8")):]
        deadline = time.monotonic() + self._delivery_wait_seconds
        message_id = None
        for index, part in enumerate(chunks):
            await self._wait_live(item, deadline=deadline)
            timeout = min(15, max(.001, deadline - time.monotonic()))
            if item["kind"] == "telegram":
                kwargs = {"parse_mode": None, "link_preview": False}
                if buttons and index == len(chunks) - 1:
                    from telethon import Button
                    kwargs["buttons"] = [[Button.inline(button["label"], button["key"].encode("ascii"))
                                           for button in row] for row in buttons]
                message = await asyncio.wait_for(self._bots[item["channel_id"]][0].send_message(
                    int(item["user_id"]), part, **kwargs), timeout)
                message_id = getattr(message, "id", None)
            else:
                await asyncio.wait_for(self.deps.runtime.notifier.send_wecom_text(
                    item["channel_id"], item["user_id"], part,
                    permission_check=lambda: self._live(item)), 15)
        return message_id

    async def interact(self, kind, channel_id, user_id, identity, receipt_id, data, *,
                       message_id=None, client=None, task_id=None):
        """Consume native buttons only; callback data never grants authority."""
        await self.start()
        if isinstance(data, bytes):
            try:
                data = data.decode("ascii")
            except UnicodeError:
                data = ""
        match = BUTTON_DATA.fullmatch(data) if isinstance(data, str) else None
        invalid = "按钮已失效，请查看最新消息。"
        if (not match or len(str(receipt_id)) > 100 or not str(receipt_id)
                or match[1] == "c" and match[3] not in {"yes", "no"}
                or match[1] == "q" and not match[3].isdigit()):
            return invalid
        user = str(user_id).lower() if kind == "wecom" else str(user_id)
        key = _id(kind, channel_id, user)
        async with self._peer_locks.setdefault(key, asyncio.Lock()):
            item = self._bindings.get(key)
            if not item or not item.get("enabled") or item.get("identity") != identity:
                return invalid
            try:
                self._live(item)
            except (PermissionError, ValueError, ChannelDisconnected):
                return invalid
            record_kind = "confirmation" if match[1] == "c" else "input"
            record = item.get(record_kind)
            if (not record or record.get("expires", 0) <= time.time()
                    or not secrets.compare_digest(match[2], record.get("code", ""))
                    or record.get("session_id") != item["session_id"]
                    or record.get("message_epoch") != item.get("message_epoch", 0)):
                return invalid
            if kind == "telegram":
                if (not self._bots.get(channel_id) or self._bots[channel_id][0] is not client
                        or type(message_id) is not int or message_id <= 0
                        or record.get("message_id") != message_id):
                    return invalid
            elif kind == "wecom":
                if not task_id or task_id != record.get("task_id"):
                    return invalid
            else:
                return invalid
            try:
                session = await self.assistant.session(item["session_id"])
                if (session.get("run") or {}).get("id") != record["run_id"]:
                    return invalid
                if not await self._claim(key, "button:" + str(receipt_id)):
                    return invalid
                self._live(item)
                if kind == "telegram" and self._bots[channel_id][0] is not client:
                    return invalid
                if record_kind == "confirmation":
                    await self.assistant.confirm(record["run_id"], record["id"], match[3] == "yes")
                    reply = "已确认。" if match[3] == "yes" else "已拒绝。"
                else:
                    index = int(match[3])
                    if index >= len(record["options"]):
                        return invalid
                    await self.assistant.answer(record["run_id"], record["id"], record["options"][index])
                    reply = "已选择。"
            except (ValueError, KeyError, PermissionError):
                return invalid
            item = {**item, record_kind: None}
            self._bindings[key] = item
            await self.store.put("channel_binding", item)
            return reply

    async def _claim(self, item_id, message_id):
        receipt = _id("receipt", item_id, message_id)
        def claim(db):
            if db.execute("SELECT 1 FROM documents WHERE kind='channel_receipt' AND id=?", (receipt,)).fetchone():
                return False
            self.store._put(db, "channel_receipt", {"id": receipt})
            db.execute("DELETE FROM documents WHERE kind='channel_receipt' AND id NOT IN "
                       "(SELECT id FROM documents WHERE kind='channel_receipt' ORDER BY updated DESC LIMIT 2000)")
            return True
        return await self.store.transaction(claim)

    async def receive(self, kind, channel_id, user_id, identity, message_id, text="", image_loader=None,
                      attachment_loader=None, audio_loader=None):
        """Return (consumed, reply); adapters deliver only this sender's reply."""
        await self.start()
        if not isinstance(text, str) or len(text) > 8000 or not str(message_id) or len(str(message_id)) > 128:
            return False, ""
        user = str(user_id).lower() if kind == "wecom" else str(user_id)
        key = _id(kind, channel_id, user)
        parts = text.strip().split(maxsplit=1)
        command = parts[0] if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command == "/配对":
            # Invalid codes must not allocate one permanent lock per unknown sender.
            preliminary = self._codes.get(hashlib.sha256(argument.encode()).hexdigest())
            if not preliminary or preliminary[:2] != (kind, channel_id) or preliminary[2] <= time.time():
                return True, "配对码无效或已过期，请在网页重新生成。"
            async with self._peer_locks.setdefault(key, asyncio.Lock()):
                async with self._lock:
                    code_key = hashlib.sha256(argument.encode()).hexdigest()
                    code = self._codes.get(code_key)
                    if not code or code[:2] != (kind, channel_id) or code[2] <= time.time():
                        return True, "配对码无效或已过期，请在网页重新生成。"
                    if len(self._bindings) >= 16 and key not in self._bindings:
                        return True, "助手绑定已达到 16 个，请先在网页移除不用的绑定。"
                    self._codes.pop(code_key, None)
                    previous = self._bindings.get(key)
                    if previous:
                        await self._stop(previous)
                    session = await self.assistant.new_session("渠道助手")
                    item = {"id": key, "kind": kind, "channel_id": channel_id, "user_id": user,
                            "identity": identity, "generation": secrets.token_hex(16), "enabled": True,
                            "session_id": session["id"], "created_at": datetime.now(timezone.utc).isoformat()}
                    await self.store.put("channel_binding", item)
                    self._bindings[key] = item
            return True, "已配对管理员。" + HELP
        item = self._bindings.get(key)
        if not item or item.get("identity") != identity:
            return False, ""
        try:
            self._live(item)
        except ChannelDisconnected:
            return True, "Bot 连接正在恢复，请稍后重发。"
        except (PermissionError, ValueError):
            return False, ""
        if command in RETIRED_DECISIONS:
            return True, "请点击消息下方的按钮。"
        if command.startswith("/") and command not in COMMANDS:
            return False, ""
        if command not in COMMANDS and (not item.get("enabled") or not (text.strip() or image_loader or attachment_loader or audio_loader)):
            return False, ""
        async with self._peer_locks.setdefault(key, asyncio.Lock()):
            item = self._live(item)
            if command not in COMMANDS and not item.get("enabled"):
                return False, ""
            if not await self._claim(key, str(message_id)):
                return True, ""
            self._live(item)
            if command in {"/助手", "/退出助手"}:
                entering = command == "/助手"
                changed = bool(item.get("enabled")) != entering
                if changed:
                    item = {**item, "enabled": entering, "message_epoch": item.get("message_epoch", 0) + 1,
                            "confirmation": None, "input": None}
                    self._bindings[key] = item
                    await self.store.put("channel_binding", item)
                if command == "/退出助手":
                    return True, "已退出助手，后续消息交给原插件处理。"
                if changed:
                    await self._resume_watcher(item)
                if not argument:
                    return True, HELP
                text = argument
            elif command == "/停止":
                item = {**item, "message_epoch": item.get("message_epoch", 0) + 1,
                        "confirmation": None, "input": None}
                self._bindings[key] = item
                await self.store.put("channel_binding", item)
                await self._stop(item)
                return True, "已停止当前助手任务。"
            elif command == "/新会话":
                try:
                    session = await self.assistant.session(item["session_id"])
                except KeyError:
                    session = {}
                if (session.get("run") or {}).get("status") in ACTIVE:
                    return True, "请先用 /停止 结束当前任务。"
                session = await self.assistant.new_session("渠道助手")
                item = {**item, "session_id": session["id"], "confirmation": None, "input": None,
                        "message_epoch": item.get("message_epoch", 0) + 1, "enabled": True}
                self._bindings[key] = item
                await self.store.put("channel_binding", item)
                return True, "已开始新会话。"
            item, session = await self._session(item)
            question = item.get("input")
            session_run = session.get("run") or {}
            if question and (session_run.get("id") != question["run_id"]
                    or (session_run.get("pending_input") or {}).get("id") != question["id"]):
                item = {**item, "input": None}
                self._bindings[key] = item
                await self.store.put("channel_binding", item)
                question = None
            if question and command not in COMMANDS:
                answer = text.strip()
                choice_text = set(question["options"]) | set(question.get("labels", []))
                for index, label in enumerate(question.get("labels", []), 1):
                    choice_text.update((str(index), f"{index}. {label}", f"{index}：{label}"))
                if (not question.get("allow_custom") or not answer
                        or image_loader or attachment_loader or audio_loader
                        or answer in choice_text):
                    return True, "请点击消息中的选项按钮。"
                if (question["expires"] <= time.time() or question.get("session_id") != item["session_id"]
                        or question.get("message_epoch") != item.get("message_epoch", 0)):
                    return True, "问题已过期，请查看最新消息。"
                try:
                    await self.assistant.answer(question["run_id"], question["id"], answer)
                except (ValueError, KeyError) as exc:
                    return True, self.assistant.clean(str(exc), 1000)
                item = {**item, "input": None}
                self._bindings[key] = item
                await self.store.put("channel_binding", item)
                return True, ""
            if self._queue.full():
                # Roll back the receipt: provider retries can be admitted later.
                await self.store.delete("channel_receipt", _id("receipt", key, str(message_id)))
                return True, "助手等待中的消息较多，请稍后重发。"
            self._queue.put_nowait((item.copy(), text.strip(), image_loader, attachment_loader, audio_loader))
        return True, ""

    async def _session(self, item):
        """A pruned/deleted channel conversation must not permanently break its binding.

        Callers hold the peer lock; never rebuild for stop/shutdown or reuse old approvals.
        """
        try:
            return item, await self.assistant.session(item["session_id"])
        except KeyError:
            session = await self.assistant.new_session("渠道助手")
            current = self._live(item, require_connected=False)
            current = {**current, "session_id": session["id"], "confirmation": None, "input": None,
                       "message_epoch": current.get("message_epoch", 0) + 1}
            self._bindings[item["id"]] = current
            await self.store.put("channel_binding", current)
            return current, session

    async def _stop(self, item):
        try:
            session = await self.assistant.session(item["session_id"])
            if (session.get("run") or {}).get("status") in ACTIVE:
                await self.assistant.cancel(session["run"]["id"])
        except KeyError:
            pass

    def _start_watcher(self, item, run_id):
        key = (item["id"], run_id)
        task = asyncio.create_task(self._watch(item.copy(), run_id), name="assistant-channel-result")
        self._watchers[key] = task
        task.add_done_callback(lambda done, k=key: self._watcher_done(k, done))

    async def _resume_watcher(self, item):
        try:
            session = await self.assistant.session(item["session_id"])
        except KeyError:
            return
        run = session.get("run") or {}
        if run.get("status") not in ACTIVE:
            return
        previous = self._watchers.get((item["id"], run["id"]))
        if previous:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        self._start_watcher(item, run["id"])

    async def _work(self):
        while True:
            item, text, loader, attachment_loader, audio_loader = await self._queue.get()
            try:
                deadline = time.monotonic() + self._delivery_wait_seconds
                current = await self._wait_live(item, deadline=deadline)
                if (current["session_id"] != item["session_id"] or not current.get("enabled")
                        or current.get("message_epoch", 0) != item.get("message_epoch", 0)):
                    continue
                images = [_image(await asyncio.wait_for(loader(), 60))] if loader else []
                attachments = []
                if attachment_loader:
                    media = await asyncio.wait_for(attachment_loader(), 60)
                    attachments.append(parse_text_attachment(media["name"], media["content"], media.get("content_type", "")))
                audio = None
                if audio_loader:
                    media = await asyncio.wait_for(audio_loader(), 60)
                    audio = parse_audio(media["name"], media["content"], media.get("content_type", ""))
                busy_reply = False
                while True:
                    # The receipt is already durable. Wait for a known outage
                    # before admission, without holding the revocation lock.
                    await self._wait_live(item, deadline=deadline)
                    async with self._peer_locks.setdefault(item["id"], asyncio.Lock()):
                        try:
                            current = self._live(item)
                        except ChannelDisconnected:
                            continue
                        if (current["session_id"] != item["session_id"] or not current.get("enabled")
                                or current.get("message_epoch", 0) != item.get("message_epoch", 0)):
                            break
                        item, session = await self._session(item)
                        try:
                            self._live(item)
                        except ChannelDisconnected:
                            continue
                        run = session.get("run")
                        if run and run.get("status") in ACTIVE and (images or attachments or audio):
                            busy_reply = True
                            break
                        # Once submit/steer has been called, its result may be
                        # unknown on error. Never loop/replay that operation.
                        if run and run.get("status") in ACTIVE:
                            run = await self.assistant.steer(run["id"], text)
                        else:
                            run = await self.assistant.submit(item["session_id"], text, images=images,
                                                              attachments=attachments, audio=audio)
                        key = (item["id"], run["id"])
                        if key not in self._watchers:
                            self._start_watcher(item, run["id"])
                        break
                if busy_reply:
                    await self._send(item, "当前任务尚未结束。请稍后发送附件，或先用 /停止 结束任务。")
            except asyncio.CancelledError:
                raise
            except PermissionError:
                pass
            except Exception as exc:
                try:
                    message = str(exc) if isinstance(exc, (ValueError, KeyError)) else "处理失败，请在网页查看任务记录。"
                    await self._send(item, self.assistant.clean(message, 1000))
                except Exception:
                    log.warning("助手渠道回复失败（%s）", type(exc).__name__)
            finally:
                self._queue.task_done()

    def _watcher_done(self, key, task):
        if self._watchers.get(key) is task:
            self._watchers.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            log.warning("助手渠道结果发送失败（%s）", type(task.exception()).__name__)

    async def _send_preview(self, item, preview, fallback, *, controls=None):
        # Invalidate previous codes before attempting a new multipart preview.
        # A delivered final chunk is not proof that every earlier chunk arrived.
        async with self._peer_locks.setdefault(item["id"], asyncio.Lock()):
            # Authorization checks must not confuse a known transport outage
            # with revocation. _send waits outside this lock before each chunk.
            current = self._live(item, require_connected=False)
            if (current["session_id"] != item["session_id"] or not current.get("enabled")
                    or current.get("message_epoch", 0) != item.get("message_epoch", 0)):
                raise PermissionError("助手会话已变更")
            current = {**current, "confirmation": None, "input": None}
            self._bindings[item["id"]] = current
            await self.store.put("channel_binding", current)
        try:
            message_id = await self._send(item, preview,
                buttons=controls["buttons"] if controls and item["kind"] == "telegram" else None)
            if controls and item["kind"] == "telegram" and (type(message_id) is not int or message_id <= 0):
                raise ValueError("按钮消息未确认送达")
            if controls and item["kind"] == "wecom":
                await self._wait_live(item)
                await self.deps.runtime.notifier.send_wecom_card(
                    item["channel_id"], item["user_id"], title=controls["title"], text="请核对上方完整内容后选择。",
                    buttons=[button for row in controls["buttons"] for button in row], task_id=controls["task_id"],
                    permission_check=lambda: self._live(item))
            return {"message_id": message_id}
        except PermissionError:
            raise
        except Exception as exc:
            try:
                await self._send(item, fallback)
            except PermissionError:
                raise
            except Exception:
                log.warning("助手渠道预览发送失败（%s）", type(exc).__name__)
            return False

    async def _telegram_typing(self, item, run_id):
        """Native presence, not another chat message; never essential to delivery."""
        if item["kind"] != "telegram":
            return
        client = self._bots.get(item["channel_id"], (None,))[0]
        if not callable(getattr(client, "action", None)):
            return
        try:
            # Fast replies need no transient status at all.
            await asyncio.sleep(1)
            while not self._closed:
                current = self._live(item)
                if (not current.get("enabled") or current["session_id"] != item["session_id"]
                        or self._bots[item["channel_id"]][0] is not client):
                    return
                run = await self.assistant.run(run_id)
                if run.get("status") not in ACTIVE:
                    return
                if run.get("status") != "running":
                    await asyncio.sleep(1)
                    continue
                # Telethon's default cancellation sends another RPC, which can
                # wait indefinitely offline and block watcher/channel shutdown.
                # Stop the sender only; Telegram expires native presence itself.
                async with client.action(int(item["user_id"]), "typing", delay=5, auto_cancel=False):
                    while not self._closed:
                        await asyncio.sleep(1)
                        current = self._live(item)
                        if (not current.get("enabled") or current["session_id"] != item["session_id"]
                                or self._bots[item["channel_id"]][0] is not client):
                            return
                        if (await self.assistant.run(run_id)).get("status") != "running":
                            break
        except asyncio.CancelledError:
            raise
        except Exception:
            # Presence errors must not cancel an accepted task or replay a send.
            return

    async def _watch(self, item, run_id):
        typing = asyncio.create_task(self._telegram_typing(item, run_id), name="assistant-telegram-typing")
        try:
            await self._watch_results(item, run_id)
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)

    async def _watch_results(self, item, run_id):
        delivered = None
        input_delivered = None
        while not self._closed:
            await self._wait_live(item)
            run = await self.assistant.run(run_id)
            pending = run.get("pending_confirmation")
            if pending and pending["id"] != delivered:
                code = secrets.token_hex(4)
                arguments = pending.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, indent=2)
                warning = "\n将提交到 GitHub，内容可能公开。请确认没有敏感信息。" if pending.get("tool") == "submit_github_issue" else ""
                preview = f"需要管理员确认\n{pending.get('summary', '')}{warning}\n\n{arguments}\n\n请点击下方按钮。"
                controls = {"title": "需要管理员确认", "task_id": f"awbnas_{code}_{secrets.token_hex(4)}",
                            "buttons": [[{"label": "确认执行", "key": f"awbn:assistant:c:{code}:yes"},
                                         {"label": "拒绝", "key": f"awbn:assistant:c:{code}:no"}]]}
                sent = await self._send_preview(item, preview,
                        "操作预览未能完整发送，请在网页中审阅并确认。本次消息按钮无效。", controls=controls)
                if sent:
                    async with self._peer_locks.setdefault(item["id"], asyncio.Lock()):
                        current = self._live(item, require_connected=False)
                        latest = await self.assistant.run(run_id)
                        if (current["session_id"] == item["session_id"] and current.get("enabled")
                                and current.get("message_epoch", 0) == item.get("message_epoch", 0)
                                and (latest.get("pending_confirmation") or {}).get("id") == pending["id"]):
                            expires = datetime.fromisoformat(pending["expires_at"]).timestamp()
                            current = {**current, "confirmation": {"id": pending["id"], "run_id": run_id,
                                       "code": code, "expires": min(time.time() + 900, expires),
                                       "session_id": item["session_id"], "message_epoch": item.get("message_epoch", 0),
                                       "message_id": sent["message_id"], "task_id": controls["task_id"]}}
                            self._bindings[item["id"]] = current
                            await self.store.put("channel_binding", current)
                delivered = pending["id"]
            question = run.get("pending_input")
            if question and question["id"] != input_delivered:
                code = secrets.token_hex(4)
                options = question.get("options", [])
                preview = ("需要补充信息\n" + str(question.get("question", "")) + "\n\n"
                           + "\n".join(f"{index + 1}：{option['label']}" for index, option in enumerate(options))
                           + "\n\n请点击下方选项。")
                if question.get("allow_custom"):
                    preview += "\n需要补充时，直接发送文字。"
                controls = {"title": "需要补充信息", "task_id": f"awbnas_{code}_{secrets.token_hex(4)}",
                            "buttons": [[{"label": f"{index + 1}. {str(option['label'])[:50]}" +
                                                  ("…" if len(str(option["label"])) > 50 else ""),
                                          "key": f"awbn:assistant:q:{code}:{index}"}]
                                        for index, option in enumerate(options)]}
                sent = await self._send_preview(item, preview,
                        "问题未能完整发送，请在网页中查看并回答。本次消息按钮无效。", controls=controls)
                if sent:
                    async with self._peer_locks.setdefault(item["id"], asyncio.Lock()):
                        current = self._live(item, require_connected=False)
                        latest = await self.assistant.run(run_id)
                        if (current["session_id"] == item["session_id"] and current.get("enabled")
                                and current.get("message_epoch", 0) == item.get("message_epoch", 0)
                                and (latest.get("pending_input") or {}).get("id") == question["id"]):
                            expires = datetime.fromisoformat(question["expires_at"]).timestamp()
                            current = {**current, "input": {"id": question["id"], "run_id": run_id,
                                       "code": code, "options": [option["id"] for option in options],
                                       "labels": [option["label"] for option in options],
                                       "allow_custom": question.get("allow_custom", False),
                                       "expires": min(time.time() + 900, expires),
                                       "session_id": item["session_id"], "message_epoch": item.get("message_epoch", 0),
                                       "message_id": sent["message_id"], "task_id": controls["task_id"]}}
                            self._bindings[item["id"]] = current
                            await self.store.put("channel_binding", current)
                input_delivered = question["id"]
            if run.get("status") not in ACTIVE:
                session = await self.assistant.session(item["session_id"])
                result = next((message.get("content", "") for message in reversed(session.get("messages", []))
                               if message.get("role") == "assistant" and message.get("run_id") == run_id), "任务已完成。")
                if run.get("status") != "completed":
                    result = run.get("error") or {"cancelled": "任务已停止。", "interrupted": "任务已中断。"}.get(run.get("status"), "任务失败，请在网页查看记录。")
                await self._send(item, self.assistant.clean(result, 16000))
                return
            await asyncio.sleep(1)

    async def attach_bot(self, channel_id, client):
        if self._closed:
            return
        old = self._bots.get(channel_id)
        if old and old[0] is client:
            return
        from telethon import events
        user_id = getattr(client, "_self_id", None)
        if not user_id:
            user_id = (await asyncio.wait_for(client.get_me(), 10)).id
        identity = str(int(user_id))

        async def handler(event):
            if not event.is_private or event.out or not event.sender_id or event.sender_id <= 0:
                return
            def size_error(limit):
                return ("语音不能超过 8 MiB" if limit == MAX_AUDIO_BYTES else
                        "图片不能超过 5 MiB" if limit == MAX_IMAGE else "单个文本附件不能超过 64 KiB")
            async def download(limit):
                media = event.message.media
                size = getattr(getattr(event.message, "file", None), "size", None)
                if size and size > limit:
                    raise ValueError(size_error(limit))
                output = io.BytesIO()
                async for chunk in client.iter_download(media, request_size=262144):
                    if output.tell() + len(chunk) > limit:
                        raise ValueError(size_error(limit))
                    output.write(chunk)
                return output.getvalue()
            async def image_loader():
                return await download(MAX_IMAGE)
            async def file_loader():
                return {"name": filename, "content": await download(MAX_FILE_BYTES), "content_type": mime}
            async def voice_loader():
                return {"name": audio_name, "content": await download(MAX_AUDIO_BYTES), "content_type": mime}
            photo = bool(getattr(event.message, "photo", None))
            mime = str(getattr(getattr(event.message, "file", None), "mime_type", ""))
            filename = getattr(getattr(event.message, "file", None), "name", "")
            voice = bool(getattr(event.message, "voice", None))
            default_extension = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
                                 "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm",
                                 "audio/aac": "aac", "audio/amr": "amr"}.get(mime, "ogg")
            audio_name = filename if filename else f"voice.{default_extension}"
            loader = image_loader if photo or mime in {"image/jpeg", "image/png", "image/webp", "image/gif"} else None
            attachment = file_loader if not loader and is_text_filename(filename) else None
            audio = voice_loader if not loader and not attachment and (voice or mime.startswith("audio/") or is_audio_filename(filename)) else None
            consumed = False
            try:
                consumed, reply = await self.receive("telegram", channel_id, str(event.sender_id), identity,
                                                     str(event.id), event.raw_text or "", loader, attachment, audio)
                if not consumed:
                    return
                if reply:
                    await asyncio.wait_for(client.send_message(event.sender_id, reply, parse_mode=None,
                                                               link_preview=False), 15)
            except Exception:
                # Do not reinterpret an accepted assistant operation as a plugin command.
                log.warning("助手 Telegram 消息处理失败")
                command = (event.raw_text or "").strip().split(maxsplit=1)
                item = self._bindings.get(_id("telegram", channel_id, str(event.sender_id)))
                intended = command and command[0] in COMMANDS | RETIRED_DECISIONS | {"/配对"}
                if consumed or intended or item and item.get("enabled") and (not command or not command[0].startswith("/")):
                    raise events.StopPropagation
                return
            raise events.StopPropagation

        async def callback_handler(event):
            data = getattr(event, "data", b"")
            if not isinstance(data, bytes) or not data.startswith(BUTTON_PREFIX):
                return
            reply = "按钮已失效，请查看最新消息。"
            try:
                if (event.is_private and not getattr(event, "via_inline", False)
                        and type(event.sender_id) is int and event.sender_id > 0
                        and event.chat_id == event.sender_id):
                    reply = await self.interact("telegram", channel_id, str(event.sender_id), identity,
                        str(event.id), data, message_id=event.message_id, client=client)
                try:
                    await asyncio.wait_for(event.answer(reply, alert=False), 5)
                except Exception:
                    pass
                if reply in {"已确认。", "已拒绝。", "已选择。"}:
                    try:
                        kwargs = {"buttons": None}
                        if callable(getattr(event, "get_message", None)):
                            message = await asyncio.wait_for(event.get_message(), 5)
                            text = getattr(message, "raw_text", None)
                            if isinstance(text, str) and text:
                                kwargs.update(text=text + "\n\n" + reply, parse_mode=None, link_preview=False)
                        await asyncio.wait_for(client.edit_message(event.sender_id, event.message_id, **kwargs), 10)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    await asyncio.wait_for(event.answer("处理未确认，请在网页查看任务记录。", alert=False), 5)
                except Exception:
                    pass
            raise events.StopPropagation

        client.add_event_handler(handler, events.NewMessage(incoming=True))
        client.add_event_handler(callback_handler, events.CallbackQuery(pattern=rb"^awbn:assistant:"))
        self._bots[channel_id] = (client, identity, handler, callback_handler)
        if old:
            for old_handler in old[2:]:
                old[0].remove_event_handler(old_handler)

    async def handle_wecom(self, channel_id, message):
        config = channel_config(self.deps.settings, channel_id)
        if (not config or config.get("enabled", True) is not True or config.get("callback_enabled") is not True
                or not callback_member_allowed(config, message.get("FromUserName", ""))):
            return False, ""
        validate_callback_config(config)
        if not validate_callback_message(message, config):
            return False, ""
        if message.get("MsgType") == "event":
            if (message.get("Event") != "template_card_event"
                    or not str(message.get("EventKey", "")).startswith(BUTTON_PREFIX.decode("ascii"))):
                return False, ""
            from .wecom_messages import WeComButtonUpdate
            receipt = hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            reply = await self.interact("wecom", channel_id, message["FromUserName"],
                list(callback_application(config)), receipt, message["EventKey"], task_id=message.get("TaskId"))
            labels = {"已确认。": "已确认", "已拒绝。": "已拒绝", "已选择。": "已选择"}
            # An early click while delivery is still unconfirmed must remain
            # retryable; do not freeze a card until the operation is consumed.
            return True, WeComButtonUpdate(labels[reply]) if reply in labels else ""
        if message.get("MsgType") not in {"text", "image", "file", "voice"}:
            return False, ""
        if message["MsgType"] == "file" and not (is_text_filename(message.get("FileName", ""))
                                                  or is_audio_filename(message.get("FileName", ""))):
            return False, ""
        identity = list(callback_application(config))
        user = message["FromUserName"]
        async def download(limit):
            key = _id("wecom", channel_id, user.lower())
            item = self._bindings.get(key)
            if not item:
                raise PermissionError("助手绑定已撤销")
            self._live(item)
            media = await self.deps.runtime.notifier.download_wecom_media(channel_id, user, message["MediaId"],
                max_bytes=limit, permission_check=lambda: self._live(item))
            self._live(item)
            return media
        async def image_loader():
            return (await download(MAX_IMAGE))["content"]
        async def file_loader():
            media = await download(MAX_FILE_BYTES)
            return {**media, "name": message["FileName"]}
        async def voice_loader():
            media = await download(MAX_AUDIO_BYTES)
            if message["MsgType"] == "file":
                return {**media, "name": message["FileName"]}
            extension = str(message.get("Format", "amr")).lower()
            if extension not in {"amr", "mp3", "wav", "ogg", "aac", "m4a", "webm"}:
                raise ValueError("企业微信语音格式暂不支持，请发送 AMR、MP3 或 WAV 文件")
            return {**media, "name": f"voice.{extension}"}
        return await self.receive("wecom", channel_id, user, identity, message["MsgId"],
                                  message.get("Content", ""), image_loader if message["MsgType"] == "image" else None,
                                  file_loader if message["MsgType"] == "file" and is_text_filename(message.get("FileName", "")) else None,
                                  voice_loader if message["MsgType"] == "voice" or message["MsgType"] == "file"
                                  and is_audio_filename(message.get("FileName", "")) else None)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if getattr(self.deps.accounts, "on_bot_ready", None) is self._bot_hook:
            self.deps.accounts.on_bot_ready = None
        for bot in self._bots.values():
            for handler in bot[2:]:
                bot[0].remove_event_handler(handler)
        self._bots.clear()
        tasks = list(self._workers) + list(self._watchers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._watchers.clear()
        await asyncio.gather(*(self._stop(item) for item in self._bindings.values()), return_exceptions=True)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._codes.clear()
