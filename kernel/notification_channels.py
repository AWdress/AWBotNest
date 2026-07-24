"""
kernel/notification_channels.py
多渠道通知发送实现：Telegram、企业微信、Bark
"""
from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from typing import Any, Optional
from core import logger

try:
    import httpx
except ImportError:
    httpx = None


class _TelegramHtmlToMarkdown(HTMLParser):
    """把 Telegram 常用 HTML 标签转换成企业微信/Bark 可识别的基础 Markdown。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        elif tag in {"s", "strike", "del"}:
            self.parts.append("~~")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "pre":
            self.parts.append("\n```\n")
        elif tag == "a":
            self.parts.append("[")
            self.links.append(str(attrs_dict.get("href") or ""))
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"br", "p", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        elif tag in {"s", "strike", "del"}:
            self.parts.append("~~")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "pre":
            self.parts.append("\n```\n")
        elif tag == "a":
            url = self.links.pop() if self.links else ""
            self.parts.append(f"]({url})" if url else "]")
        elif tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def markdown(self) -> str:
        return "".join(self.parts)


_HTML_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|br|p|div|li|ul|ol|blockquote|tg-spoiler)(?:\s[^>]*)?>",
    re.IGNORECASE,
)


def _clean_spacing(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按渠道的字节限制截断，避免中文正文过长导致整条消息被拒绝。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "\n…内容过长，已截断"
    budget = max(0, max_bytes - len(suffix.encode("utf-8")))
    clipped = encoded[:budget]
    while clipped:
        try:
            return clipped.decode("utf-8") + suffix
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return suffix.strip()


def _to_markdown(message: str) -> str:
    """识别 Telegram HTML，并转换为通用基础 Markdown。"""
    text = str(message or "")
    if _HTML_TAG_RE.search(text):
        parser = _TelegramHtmlToMarkdown()
        parser.feed(text)
        parser.close()
        text = parser.markdown()
    else:
        text = html.unescape(text)
    # Telegram MarkdownV2 常用转义在其他渠道会显示反斜杠，转换时去掉。
    text = re.sub(r"\\([_\-*\[\]()~`>#+=|{}.!])", r"\1", text)
    return _clean_spacing(text)


def _markdown_to_plain(markdown: str) -> str:
    """为不支持富文本的旧渠道生成仍包含链接地址的纯文本。"""
    text = markdown
    text = re.sub(r"!\[([^]]*)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"```(?:\w+)?\n?", "", text)
    text = re.sub(r"(?<!\w)(?:\*\*|__|~~)(.+?)(?:\*\*|__|~~)(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    return _clean_spacing(text)


def format_channel_message(message: str) -> tuple[str, str]:
    """返回（基础 Markdown，纯文本），供非 Telegram 渠道选择。"""
    markdown = _to_markdown(message)
    return markdown, _markdown_to_plain(markdown)


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


# 企业微信 access_token 全局缓存（corpid+secret → {token, expires_at}）
_wechat_token_cache: dict = {}


class WeChatWorkChannel(NotificationChannel):
    """企业微信通知渠道"""

    def __init__(self, config: dict):
        super().__init__(config)
        self.corpid = config.get("corpid", "")
        self.agentid = config.get("agentid", "")
        self.secret = config.get("secret", "")
        self.touser = str(config.get("touser") or "@all")
        self.base_url = str(config.get("proxy") or "https://qyapi.weixin.qq.com").rstrip("/")

    def _cache_key(self) -> str:
        return f"{self.corpid}:{self.secret}"

    async def _get_access_token(self) -> Optional[str]:
        """获取企业微信 access_token，优先从缓存取（有效期提前60秒刷新）"""
        if not httpx:
            logger.error("httpx库未安装，无法使用企业微信通知")
            return None

        key = self._cache_key()
        cached = _wechat_token_cache.get(key)
        if cached and cached["expires_at"] > time.time() + 60:
            return cached["token"]

        try:
            url = f"{self.base_url}/cgi-bin/gettoken"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={
                    "corpid": self.corpid,
                    "corpsecret": self.secret,
                })
                data = resp.json()
                if data.get("errcode") == 0:
                    token = data.get("access_token")
                    # 企业微信 token 有效期 7200 秒
                    expires_in = int(data.get("expires_in", 7200))
                    _wechat_token_cache[key] = {
                        "token": token,
                        "expires_at": time.time() + expires_in,
                    }
                    return token
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

            markdown, plain = format_channel_message(message)
            markdown = _truncate_utf8(markdown, 2000)
            plain = _truncate_utf8(plain, 2000)

            # 企业微信使用自身支持的 Markdown；若服务端不接受则退回纯文本。
            url = f"{self.base_url}/cgi-bin/message/send?access_token={token}"
            payload = {
                "touser": self.touser,
                "msgtype": "markdown",
                "agentid": int(self.agentid),
                "markdown": {
                    "content": markdown
                },
                "safe": 0
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()

                if data.get("errcode") == 0:
                    logger.info("企业微信通知发送成功")
                    return True

                fallback_payload = {
                    "touser": self.touser,
                    "msgtype": "text",
                    "agentid": int(self.agentid),
                    "text": {"content": plain},
                    "safe": 0,
                }
                fallback_resp = await client.post(url, json=fallback_payload)
                fallback_data = fallback_resp.json()
                if fallback_data.get("errcode") == 0:
                    logger.info("企业微信通知已用纯文本发送")
                    return True

                logger.error(
                    "企业微信通知发送失败: markdown=%s, text=%s",
                    data, fallback_data,
                )
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
            markdown, plain = format_channel_message(message)
            markdown_lines = markdown.split("\n", 1)
            plain_lines = plain.split("\n", 1)
            title = plain_lines[0] if plain_lines and plain_lines[0] else "通知"
            plain_body = plain_lines[1] if len(plain_lines) > 1 else title
            markdown_body = markdown_lines[1] if len(markdown_lines) > 1 else markdown

            # JSON 请求不会因正文里的链接、斜杠或特殊字符破坏 Bark 地址。
            url = f"{self.server.rstrip('/')}/{self.device_key}"
            payload = {"title": title, "body": plain_body}
            if markdown_body != plain_body:
                payload["markdown"] = markdown_body

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
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
                            bot: Any = None, target: Any = None, **kwargs) -> bool:
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
            return await channel.send_via_bot(bot, target, message, **kwargs)

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
