from __future__ import annotations

import json
import time
import logging
from typing import Any

from .config import Settings
from .services import HttpService
from .telegram import TelegramAccounts
from .notification_text import content, notification
from .rich_delivery import send_rich, DeliveryUncertain


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
            selected_id = str(config.get("bot_id") or config.get("id") or channel or bot_id or "")
            bot = self.accounts.choose_bot(selected_id)
            if bot is None or not bot.is_connected():
                raise RuntimeError("Telegram 通知 Bot 不可用")
            if isinstance(target, str):
                target = target.strip()
            if target in (None, "", 0):
                target = await self.accounts.default_notification_target()
            if target in (None, "", 0):
                raise RuntimeError("Telegram 通知未找到接收人：请登录首个用户账号或填写 Chat ID")
            if isinstance(target, str) and target.lstrip("-").isdigit():
                target = int(target)
            resolved_id = next((key for key, value in self.accounts.bots.items() if value is bot), selected_id)
            token = next((spec.token for spec in self.settings.bot_specs() if spec.id == resolved_id), '')
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
                response = await self.http.post(url, json={"msgtype": "text", "text": {"content": plain_text}})
            else:
                corpid = str(config.get("corpid") or "")
                secret = str(config.get("secret") or "")
                agentid = str(config.get("agentid") or "")
                base = str(config.get("proxy") or "https://qyapi.weixin.qq.com").rstrip("/")
                if not corpid or not secret or not agentid:
                    raise RuntimeError("企业微信通知配置不完整")
                token_response = await self.http.get(
                    f"{base}/cgi-bin/gettoken", params={"corpid": corpid, "corpsecret": secret},
                )
                token_response.raise_for_status()
                token_data = token_response.json()
                token = str(token_data.get("access_token") or "")
                if not token:
                    raise RuntimeError(f"企业微信获取令牌失败：{token_data.get('errmsg') or '未知错误'}")
                response = await self.http.post(
                    f"{base}/cgi-bin/message/send", params={"access_token": token},
                    json={"touser": str(config.get("touser") or "@all"), "msgtype": "text",
                          "agentid": int(agentid), "text": {"content": plain_text}, "safe": 0},
                )
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
        if kind in {"wecom", "wechat"} and isinstance(data, dict) and data.get("errcode") not in (None, 0):
            raise RuntimeError(f"企业微信通知失败：{data.get('errmsg') or data}")
        return data
