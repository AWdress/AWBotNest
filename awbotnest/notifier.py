from __future__ import annotations

import json
import time
from typing import Any

from .config import Settings
from .services import HttpService
from .telegram import TelegramAccounts


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
        cutoff = time.time() - 30 * 24 * 3600
        return [item for item in reversed(values[-100:]) if isinstance(item, dict)
                and float(item.get("t") or 0) >= cutoff]

    def _append_history(self, item: dict[str, object]) -> None:
        values = list(reversed(self.history()))
        values.append(item)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values[-100:], ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.history_path)

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
                   level: str = "info", category: str = "", _record: bool = True) -> Any:
        plain_text = str(text or "").strip()
        if not plain_text:
            raise ValueError("通知内容不能为空")
        if level not in {"info", "success", "warning", "error"}:
            level = "info"
        account = ""
        if isinstance(entity, str):
            account = entity
        elif entity is not None:
            account = str(getattr(entity, "name", "") or getattr(entity, "id", "") or "")
        if _record:
            self._append_history({
                "id": str(time.time_ns()), "t": time.time(), "plugin_id": str(plugin_id or ""),
                "plugin_name": str(plugin_name or plugin_id or "系统"), "level": level,
                "category": str(category or ""), "account": account, "text": plain_text,
            })
        if not channel and plugin_id:
            routed = [item.strip() for item in str(self.settings.bot_routing.get(plugin_id, "")).split(",") if item.strip()]
            if routed:
                results = []
                for channel_id in routed:
                    results.append(await self.send(
                        plain_text, channel=channel_id, entity=entity, bot_id=bot_id,
                        plugin_id=plugin_id, plugin_name=plugin_name, level=level,
                        category=category, _record=False,
                    ))
                return results
        spec = next((item for item in self.settings.notification_channels
                     if str(item.get("id")) == channel), None) if channel else None
        raw = spec or {}
        nested = raw.get("config") if isinstance(raw.get("config"), dict) else {}
        config = {**nested, **raw}
        if config.get("enabled") is False:
            raise RuntimeError("通知渠道已停用")
        kind = str(config.get("type") or "telegram")
        if kind == "telegram":
            target = entity if entity is not None else (config.get("chat_id") or self.settings.default_bot_chat_id)
            bot = self.accounts.choose_bot(bot_id or str(config.get("bot_id") or config.get("id") or ""))
            if bot is None or not bot.is_connected() or target in (None, ""):
                raise RuntimeError("Telegram 通知缺少可用 Bot 或目标会话")
            return await bot.send_message(target, plain_text)
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
