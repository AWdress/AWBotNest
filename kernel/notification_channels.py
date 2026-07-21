"""
kernel/notification_channels.py
多渠道通知发送实现：Telegram、企业微信、Bark
"""
from __future__ import annotations

import json
from typing import Any, Optional
from core import logger

try:
    import httpx
except ImportError:
    httpx = None


class NotificationChannel:
    """通知渠道基类"""

    def __init__(self, config: dict):
        self.config = config

    async def send(self, message: str) -> bool:
        """发送通知，返回是否成功"""
        raise NotImplementedError


class TelegramChannel(NotificationChannel):
    """Telegram通知渠道（使用已有的Bot）"""

    async def send_via_bot(self, bot: Any, target: Any, message: str, **kwargs) -> bool:
        """通过已连接的Bot发送"""
        try:
            if bot and getattr(bot, "is_connected", False) and target:
                await bot.send_message(target, message, **kwargs)
                return True
            return False
        except Exception as e:
            logger.error(f"Telegram发送失败: {e}")
            return False


class WeChatWorkChannel(NotificationChannel):
    """企业微信通知渠道"""

    def __init__(self, config: dict):
        super().__init__(config)
        self.corpid = config.get("corpid", "")
        self.agentid = config.get("agentid", "")
        self.secret = config.get("secret", "")
        self.touser = config.get("touser", "@all")  # 默认发送给所有人
        self._access_token: Optional[str] = None

    async def _get_access_token(self) -> Optional[str]:
        """获取企业微信access_token"""
        if not httpx:
            logger.error("httpx库未安装，无法使用企业微信通知")
            return None

        try:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corpid}&corpsecret={self.secret}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                data = resp.json()
                if data.get("errcode") == 0:
                    self._access_token = data.get("access_token")
                    return self._access_token
                else:
                    logger.error(f"获取企业微信access_token失败: {data}")
                    return None
        except Exception as e:
            logger.error(f"获取企业微信access_token异常: {e}")
            return None

    async def send(self, message: str) -> bool:
        """发送企业微信通知"""
        if not httpx:
            logger.error("httpx库未安装，无法使用企业微信通知")
            return False

        if not all([self.corpid, self.agentid, self.secret]):
            logger.error("企业微信配置不完整")
            return False

        try:
            # 获取access_token
            token = await self._get_access_token()
            if not token:
                return False

            # 发送消息
            url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            payload = {
                "touser": self.touser,
                "msgtype": "text",
                "agentid": int(self.agentid),
                "text": {
                    "content": message
                },
                "safe": 0
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()

                if data.get("errcode") == 0:
                    logger.info("企业微信通知发送成功")
                    return True
                else:
                    logger.error(f"企业微信通知发送失败: {data}")
                    return False

        except Exception as e:
            logger.error(f"企业微信通知发送异常: {e}")
            return False


class BarkChannel(NotificationChannel):
    """Bark通知渠道"""

    def __init__(self, config: dict):
        super().__init__(config)
        self.server = config.get("server", "https://api.day.app")
        self.device_key = config.get("device_key", "")

    async def send(self, message: str) -> bool:
        """发送Bark通知"""
        if not httpx:
            logger.error("httpx库未安装，无法使用Bark通知")
            return False

        if not self.device_key:
            logger.error("Bark配置不完整：缺少device_key")
            return False

        try:
            # Bark API: https://api.day.app/{device_key}/{title}/{body}
            # 将消息分割为标题和正文
            lines = message.strip().split("\n", 1)
            title = lines[0] if lines else "通知"
            body = lines[1] if len(lines) > 1 else message

            # URL编码
            from urllib.parse import quote
            url = f"{self.server.rstrip('/')}/{self.device_key}/{quote(title)}/{quote(body)}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                data = resp.json()

                if data.get("code") == 200:
                    logger.info("Bark通知发送成功")
                    return True
                else:
                    logger.error(f"Bark通知发送失败: {data}")
                    return False

        except Exception as e:
            logger.error(f"Bark通知发送异常: {e}")
            return False


async def send_notification(channel_type: str, config: dict, message: str,
                           bot: Any = None, target: Any = None) -> bool:
    """
    统一的通知发送接口

    Args:
        channel_type: 渠道类型 telegram/wechat/bark
        config: 渠道配置
        message: 消息内容
        bot: Telegram Bot实例（仅telegram需要）
        target: Telegram目标Chat ID（仅telegram需要）

    Returns:
        是否发送成功
    """
    try:
        if channel_type == "telegram":
            channel = TelegramChannel(config)
            return await channel.send_via_bot(bot, target, message)

        elif channel_type == "wechat":
            channel = WeChatWorkChannel(config)
            return await channel.send(message)

        elif channel_type == "bark":
            channel = BarkChannel(config)
            return await channel.send(message)

        else:
            logger.error(f"不支持的通知渠道类型: {channel_type}")
            return False

    except Exception as e:
        logger.error(f"发送{channel_type}通知异常: {e}")
        return False
