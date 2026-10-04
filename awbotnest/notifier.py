from __future__ import annotations

import json
import time
import logging
import re
from email.message import Message
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .services import HttpService
from .telegram import TelegramAccounts
from .notification_text import content, notification
from .rich_delivery import send_rich, DeliveryUncertain
from .wecom_messages import valid_wecom_media_id


class NotificationService:
    def __init__(self, settings: Settings, accounts: TelegramAccounts, http: HttpService) -> None:
        self.settings = settings
        self.accounts = accounts
        self.http = http
        from .config import DATA_DIR
        self.history_path = DATA_DIR / "notifications.json"
        self.state_path = DATA_DIR / "notification_state.json"

    def history(self) -> list[dict[str, object]]:
        try:
            values = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(values, list):
            return []
        cutoff = time.time() - 30 * 24 * 3600
        result = []
        for item in reversed(values[-100:]):
            try:
                if isinstance(item, dict) and float(item.get("t") or 0) >= cutoff:
                    result.append(item)
            except (ValueError, TypeError):
                continue
        return result

    def _append_history(self, item: dict[str, object]) -> None:
        values = list(reversed(self.history()))
        values.append(item)
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.history_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values[-100:], ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.history_path)
        except OSError:
            logging.getLogger("awbotnest.notifier").debug("通知历史保存失败，继续投递通知", exc_info=True)

    def read_at(self) -> float:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return float(value.get("read_at") or 0) if isinstance(value, dict) else 0
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return 0

    def mark_read(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"read_at": time.time()}), encoding="utf-8")
        temporary.replace(self.state_path)

    def clear_history(self) -> None:
        self.history_path.unlink(missing_ok=True)
        self.mark_read()

    async def _wecom_post(self, url: str, **kwargs):
        try:
            return await self.http.post(url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                httpx.ProxyError, httpx.UnsupportedProtocol, httpx.LocalProtocolError,
                httpx.InvalidURL):
            raise RuntimeError("企业微信通知连接失败") from None
        except httpx.HTTPError:
            raise DeliveryUncertain("企业微信通知结果未确认，请检查接收端；未重复发送") from None

    @staticmethod
    def _wecom_delivery_data(response) -> dict:
        # Missing acknowledgement does not prove rejection: do not retry via another channel.
        if not response.is_success:
            raise DeliveryUncertain("企业微信通知结果未确认，请检查接收端；未重复发送")
        try:
            data = response.json()
        except (ValueError, RecursionError):
            raise DeliveryUncertain("企业微信未返回有效的投递结果；未重复发送") from None
        if not isinstance(data, dict) or type(data.get("errcode")) is not int:
            raise DeliveryUncertain("企业微信未返回有效的投递结果；未重复发送")
        if data["errcode"] != 0:
            # Upstream error messages can contain credentials or request details.
            raise RuntimeError(f"企业微信通知失败（错误码 {data['errcode']}）")
        if any(data.get(key) for key in ("invaliduser", "invalidparty", "invalidtag", "unlicenseduser")):
            raise DeliveryUncertain("企业微信通知未送达全部收件人，请检查接收范围；未重复发送")
        return data

    async def _wecom_application_token(self, config: dict, *, follow_redirects: bool = True) -> tuple[str, str, str]:
        corpid = str(config.get("corpid") or "")
        secret = str(config.get("secret") or "")
        agentid = str(config.get("agentid") or "")
        base = str(config.get("proxy") or "https://qyapi.weixin.qq.com").rstrip("/")
        if not corpid or not secret or not agentid:
            raise RuntimeError("企业微信通知配置不完整")
        try:
            if follow_redirects:
                token_response = await self.http.get(
                    f"{base}/cgi-bin/gettoken", params={"corpid": corpid, "corpsecret": secret},
                )
                token_response.raise_for_status()
                token_data = token_response.json()
            else:
                token_body = bytearray()
                async with self.http.stream(
                        "GET", f"{base}/cgi-bin/gettoken", params={"corpid": corpid, "corpsecret": secret},
                        headers={"Accept-Encoding": "identity"}, follow_redirects=False) as token_response:
                    token_response.raise_for_status()
                    if token_response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
                        raise ValueError("Unsupported token encoding")
                    async for chunk in token_response.aiter_bytes(chunk_size=16 * 1024):
                        if len(token_body) + len(chunk) > 64 * 1024:
                            raise ValueError("Oversized token response")
                        token_body.extend(chunk)
                token_data = json.loads(token_body)
            token = token_data.get("access_token") if isinstance(token_data, dict) else None
            if (not isinstance(token, str) or not token
                    or type(token_data.get("errcode")) is not int or token_data["errcode"] != 0):
                raise ValueError("Invalid token response")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, RecursionError):
            # HTTPStatusError includes the query URL, which contains the application Secret.
            raise RuntimeError("企业微信获取令牌失败") from None
        return base, token, agentid

    async def _wecom_application_request(self, config: dict, text: str, user_id: str, *, before_send=None):
        base, token, agentid = await self._wecom_application_token(config)
        if before_send is not None:
            before_send()
        return await self._wecom_post(
            f"{base}/cgi-bin/message/send", params={"access_token": token},
            json={"touser": user_id, "msgtype": "text", "agentid": int(agentid),
                  "text": {"content": text}, "safe": 0},
        )

    async def send_wecom_text(self, channel_id: str, user_id: str, text: str, *, permission_check=None) -> dict:
        """Reply only to the authorized sender, never broadcast or fall back."""
        from .wecom_config import callback_member_allowed, channel_config, validate_callback_config

        config = channel_config(self.settings, channel_id)
        if (config is None or config.get("enabled", True) is not True
                or config.get("callback_enabled") is not True):
            raise RuntimeError("企业微信消息回调已停用")
        validate_callback_config(config)
        if not callback_member_allowed(config, user_id):
            raise PermissionError("此成员无权接收指令结果")
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 2000:
            raise ValueError("企业微信指令结果长度不正确")

        def before_send() -> None:
            current = channel_config(self.settings, channel_id)
            if (current is None or current.get("enabled", True) is not True
                    or current.get("callback_enabled") is not True):
                raise PermissionError("企业微信消息回调已停用")
            validate_callback_config(current)
            if not callback_member_allowed(current, user_id) or any(
                    str(current.get(key) or "") != str(config.get(key) or "")
                    for key in ("corpid", "agentid", "secret", "proxy")):
                raise PermissionError("企业微信接收权限或应用配置已变更")
            if permission_check is not None:
                permission_check()

        response = await self._wecom_application_request(config, text, user_id, before_send=before_send)
        return self._wecom_delivery_data(response)

    @staticmethod
    def _wecom_media_base(config: dict) -> str:
        base = str(config.get("proxy") or "https://qyapi.weixin.qq.com").rstrip("/")
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment and "\\" not in base
                     and not any(ord(char) <= 32 or ord(char) == 127 for char in base))
            parsed.port  # Reject invalid ports before attaching application credentials.
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("企业微信应用接口地址不正确")
        return base

    @staticmethod
    def _wecom_media_filename(disposition: str, content_type: str) -> str:
        default = {"image/jpeg": "image.jpg", "image/png": "image.png", "image/gif": "image.gif",
                   "image/webp": "image.webp"}.get(content_type, "image" if content_type.startswith("image/") else "attachment.bin")
        if not disposition or len(disposition) > 4096:
            return default
        try:
            message = Message()
            message["Content-Disposition"] = disposition
            filename = message.get_filename()
        except (TypeError, ValueError):
            return default
        if not isinstance(filename, str):
            return default
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", filename).strip(" .")
        return filename[:200] or default

    async def download_wecom_media(self, channel_id: str, user_id: str, media_id: str, *,
                                   permission_check=None, max_bytes: int = 20 * 1024 * 1024) -> dict:
        """Download only the callback sender's authorized media, without exposing credentials."""
        from .wecom_config import callback_member_allowed, channel_config, validate_callback_config

        if type(max_bytes) is not int or not 0 < max_bytes <= 20 * 1024 * 1024:
            raise ValueError("企业微信媒体大小限制须为 1 字节至 20 MiB")
        if not valid_wecom_media_id(media_id):
            raise ValueError("企业微信 MediaID 格式不正确，不能使用下载地址")
        config = channel_config(self.settings, channel_id)
        if (config is None or config.get("enabled", True) is not True
                or config.get("callback_enabled") is not True):
            raise PermissionError("企业微信消息回调已停用")
        validate_callback_config(config)
        if not callback_member_allowed(config, user_id):
            raise PermissionError("此成员无权下载消息附件")
        guarded_fields = ("id", "type", "enabled", "callback_enabled", "callback_token", "callback_aes_key",
                          "callback_users", "corpid", "agentid", "secret", "proxy", "url", "webhook")
        snapshot = {key: config.get(key) for key in guarded_fields}

        def check_permission() -> None:
            current = channel_config(self.settings, channel_id)
            if (current is None or current.get("enabled", True) is not True
                    or current.get("callback_enabled") is not True):
                raise PermissionError("企业微信消息回调已停用")
            validate_callback_config(current)
            if (not callback_member_allowed(current, user_id)
                    or any(current.get(key) != snapshot[key] for key in guarded_fields)):
                raise PermissionError("企业微信下载权限或应用配置已变更")
            if permission_check is not None:
                permission_check()

        check_permission()
        base = self._wecom_media_base(config)
        _, token, _ = await self._wecom_application_token(config, follow_redirects=False)
        check_permission()
        content = bytearray()
        try:
            async with self.http.stream(
                    "GET", f"{base}/cgi-bin/media/get", params={"access_token": token, "media_id": media_id},
                    headers={"Accept-Encoding": "identity"}, follow_redirects=False) as response:
                response.raise_for_status()
                encoding = response.headers.get("content-encoding", "").strip().lower()
                if encoding not in {"", "identity"}:
                    raise RuntimeError("企业微信媒体返回了不支持的压缩内容")
                declared = response.headers.get("content-length")
                if declared is not None:
                    if not re.fullmatch(r"[0-9]{1,20}", declared):
                        raise RuntimeError("企业微信媒体长度信息不正确")
                    if int(declared) > max_bytes:
                        raise ValueError("企业微信媒体超过允许的大小")
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                content_type = mime if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime) and len(mime) <= 127 else "application/octet-stream"
                disposition = response.headers.get("content-disposition", "")
                attachment = False
                if 0 < len(disposition) <= 4096:
                    try:
                        metadata = Message()
                        metadata["Content-Disposition"] = disposition
                        attachment = (metadata.get_content_disposition() in {"attachment", "inline"}
                                      and bool(metadata.get_filename()))
                    except (TypeError, ValueError):
                        pass
                filename = self._wecom_media_filename(disposition, content_type)
                async for chunk in response.aiter_bytes(chunk_size=min(64 * 1024, max_bytes + 1)):
                    check_permission()
                    if len(content) + len(chunk) > max_bytes:
                        raise ValueError("企业微信媒体超过允许的大小")
                    content.extend(chunk)
        except (httpx.HTTPError, httpx.InvalidURL):
            raise RuntimeError("企业微信媒体下载失败") from None
        check_permission()
        if not content:
            raise RuntimeError("企业微信媒体未返回附件内容")
        json_response = content_type == "application/json" or content_type.endswith("+json")
        if not attachment and (json_response or content.lstrip().startswith(b"{")):
            try:
                # Error/control responses are small. Do not parse a potentially
                # large document into an unbounded Python object graph.
                data = json.loads(content) if len(content) <= 64 * 1024 else None
            except (ValueError, UnicodeError, RecursionError):
                if json_response:
                    raise RuntimeError("企业微信媒体接口返回了无效的 JSON") from None
                data = None
            if isinstance(data, dict) and type(data.get("errcode")) is int and data["errcode"] != 0:
                raise RuntimeError(f"企业微信媒体下载失败（错误码 {data['errcode']}）")
            if json_response or isinstance(data, dict) and "video_url" in data:
                raise RuntimeError("企业微信媒体接口未返回可下载的附件")
        if not attachment and content_type == "text/html":
            raise RuntimeError("企业微信媒体接口未返回可下载的附件")
        return {"content": bytes(content), "filename": filename, "content_type": content_type}

    async def send_to_default_bot(self, text: str, *, level: str = "info",
                                  category: str = "", format: str = "text") -> Any:
        """Send a system notice only through the selected Telegram Bot."""
        if category == "系统更新" and self.settings.system_update_notify_enabled is not True:
            return False
        plain_text, rich_text = content(text, format)
        if not plain_text:
            raise ValueError("通知内容不能为空")
        if level not in {"info", "success", "warning", "error"}:
            level = "info"

        def delivery_config():
            selected_id = str(self.settings.default_bot_id or "default")
            configured = next((item for item in self.settings.bot_specs() if item.id == selected_id), None)
            token = configured.token if configured else ""
            # A non-Telegram channel or plugin route must not redirect this notice.
            channel_config = {}
            for raw in self.settings.notification_channels:
                if not isinstance(raw, dict):
                    continue
                nested = raw.get("config") if isinstance(raw.get("config"), dict) else {}
                config = {**nested, **raw}
                if str(config.get("type") or "telegram") != "telegram":
                    continue
                if str(config.get("id") or "") == selected_id:
                    channel_config = config
                    break
                if not channel_config and str(config.get("bot_id") or "") == selected_id:
                    channel_config = config
            return (selected_id, token, channel_config.get("enabled") is not False,
                    channel_config.get("chat_id"), self.settings.default_bot_chat_id,
                    tuple(self.settings.user_sessions[:1]), self.settings.proxy_url)

        snapshot = delivery_config()
        selected_id, token, enabled, target, default_target, _, proxy = snapshot
        bot = self.accounts.bots.get(selected_id)
        if not enabled or (not token and (bot is None or not bot.is_connected())):
            return False
        if isinstance(target, str):
            target = target.strip()
        if target in (None, "", 0, "0"):
            target = default_target
        if isinstance(target, str):
            target = target.strip()
        if target in (None, "", 0):
            target = await self.accounts.default_notification_target()
        if target in (None, "", 0):
            return False
        if isinstance(target, str) and target.lstrip("-").isdigit():
            target = int(target)
        if target == 0:
            return False

        # Resolving an account may await network I/O; do not use a stale recipient or credential.
        if ((category == "系统更新" and self.settings.system_update_notify_enabled is not True)
                or delivery_config() != snapshot or self.accounts.bots.get(selected_id) is not bot):
            return False

        delivered_plain, delivered_rich = notification("AWBotNest", plain_text, rich_text, level, category)
        result = await send_rich(bot, target, delivered_rich, delivered_plain,
                                 token=token, proxy=proxy)
        self._append_history({
            "id": str(time.time_ns()), "t": time.time(), "plugin_id": "__system_updates__",
            "plugin_name": "AWBotNest", "level": level, "category": str(category or ""),
            "account": "", "text": plain_text,
        })
        return result

    async def send(self, text: str, *, channel: str = "", entity: object = None,
                   bot_id: str = "", plugin_id: str = "", plugin_name: str = "",
                   level: str = "info", category: str = "", _record: bool = True,
                   format: str = "text", account: Any = None) -> Any:
        plain_text, rich_text = content(text, format)
        if not plain_text:
            raise ValueError("通知内容不能为空")
        if level not in {"info", "success", "warning", "error"}:
            level = "info"
        account = str(account if isinstance(account, str) else
                      getattr(account, "name", "") or getattr(getattr(account, "me", None), "first_name", "") or "")
        if _record:
            self._append_history({
                "id": str(time.time_ns()), "t": time.time(), "plugin_id": str(plugin_id or ""),
                "plugin_name": str(plugin_name or plugin_id or "系统"), "level": level,
                "category": str(category or ""), "account": account, "text": plain_text,
            })
        if _record:
            routed = [channel] if channel else list(dict.fromkeys(
                item.strip() for item in str(self.settings.bot_routing.get(plugin_id, "")).split(",") if item.strip()))
            routed = routed or [""]
            if routed:
                results = []
                failures = []
                uncertain = None
                for channel_id in routed:
                    try:
                        results.append(await self.send(
                            text, channel=channel_id, entity=entity, bot_id=bot_id,
                            plugin_id=plugin_id, plugin_name=plugin_name, level=level,
                            category=category, _record=False, format=format, account=account,
                        ))
                    except Exception as exc:
                        if isinstance(exc, DeliveryUncertain):
                            uncertain = exc
                        failures.append(channel_id)
                        logging.getLogger("awbotnest.notifier").warning(
                            "[%s] 通知渠道 %s 投递失败（%s），继续其他渠道",
                            plugin_name or plugin_id, channel_id, type(exc).__name__)
                if results:
                    return results
                if uncertain is not None:
                    raise uncertain
                sessions = self.settings.user_sessions
                user = self.accounts.users.get(sessions[0]) if sessions else None
                if user is not None and user.is_connected():
                    plain_text, rich_text = notification(plugin_name or plugin_id, plain_text, rich_text, level, category, account)
                    return await send_rich(user, "me", rich_text, plain_text)
                raise RuntimeError("无可用通知渠道，且没有在线主账号用于保底投递")
        plain_text, rich_text = notification(plugin_name or plugin_id, plain_text, rich_text, level, category, account)
        if not channel:
            channel = next((str(item.get("id") or "") for item in self.settings.notification_channels
                            if item.get("is_default") and item.get("enabled", True)), "")
        spec = next((item for item in self.settings.notification_channels
                     if str(item.get("id")) == channel), None) if channel else None
        if channel and spec is None and channel not in {item.id for item in self.settings.bot_specs()}:
            raise LookupError("通知渠道不存在")
        raw = spec or {}
        nested = raw.get("config") if isinstance(raw.get("config"), dict) else {}
        config = {**nested, **raw}
        if config.get("enabled") is False:
            raise RuntimeError("通知渠道已停用")
        kind = str(config.get("type") or "telegram")
        if kind == "telegram":
            target = entity if entity is not None else (config.get("chat_id") or self.settings.default_bot_chat_id)
            selected_id = str(
                config.get("bot_id") or config.get("id") or channel or bot_id
                or self.settings.default_bot_id or "default"
            )
            bot = self.accounts.bots.get(selected_id)
            resolved_id = selected_id
            configured = next((item for item in self.settings.bot_specs() if item.id == resolved_id), None)
            token = configured.token if configured else ""
            if not token and (bot is None or not bot.is_connected()):
                bot = self.accounts.choose_bot(selected_id)
                if bot is not None:
                    resolved_id = next(
                        (key for key, value in self.accounts.bots.items() if value is bot), selected_id,
                    )
                    configured = next(
                        (item for item in self.settings.bot_specs() if item.id == resolved_id), None,
                    )
                    token = configured.token if configured else ""
            if not token and (bot is None or not bot.is_connected()):
                raise RuntimeError("Telegram 通知 Bot 不可用")
            if isinstance(target, str):
                target = target.strip()
            if target in (None, "", 0):
                target = await self.accounts.default_notification_target()
            if target in (None, "", 0):
                raise RuntimeError("Telegram 通知未找到接收人：请登录首个用户账号或填写 Chat ID")
            if isinstance(target, str) and target.lstrip("-").isdigit():
                target = int(target)
            return await send_rich(bot, target, rich_text, plain_text, token=token, proxy=self.settings.proxy_url)
        if kind == "bark":
            url = str(config.get("url") or config.get("server") or "").rstrip("/")
            device_key = str(config.get("device_key") or "")
            if device_key:
                url = f"{url or 'https://api.day.app'}/{device_key}"
            if not url:
                raise RuntimeError("Bark 通知缺少地址")
            response = await self.http.post(url, json={"body": plain_text, "title": "AWBotNest"})
        elif kind in {"wecom", "wechat"}:
            url = str(config.get("url") or config.get("webhook") or "")
            if url:
                response = await self._wecom_post(url, json={"msgtype": "text", "text": {"content": plain_text}})
            else:
                response = await self._wecom_application_request(config, plain_text, str(config.get("touser") or "@all"))
            return self._wecom_delivery_data(response)
        elif kind == "webhook":
            url = str(config.get("url") or "")
            if not url:
                raise RuntimeError("Webhook 通知缺少地址")
            response = await self.http.post(url, json={"text": plain_text, "source": "AWBotNest"})
        else:
            raise RuntimeError(f"不支持的通知渠道：{kind}")
        response.raise_for_status()
        try:
            data = response.json() if response.content else {"ok": True}
        except ValueError:
            return {"ok": True, "response": response.text[:1000]}
        if kind == "bark" and isinstance(data, dict) and data.get("code") not in (None, 200):
            raise RuntimeError(f"Bark 通知失败：{data.get('message') or data}")
        return data
