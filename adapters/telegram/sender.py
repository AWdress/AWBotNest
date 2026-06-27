"""
adapters/telegram/sender.py
Telegram 消息发送适配器 - 实现 core/ports/messaging.py::MessageSender

包装 Pyrogram Client，将 core 层的抽象操作映射到实际 API 调用。
"""
from __future__ import annotations

from typing import Optional

from core import enums

from core.domain.lottery import ParticipationResult
from core.domain.red_packet import SnatchResult, SnatchStatus


class PyrogramMessageSender:
    """
    MessageSender 的 Pyrogram 实现

    Args:
        client: Pyrogram Client 实例（user_app）
    """

    def __init__(self, client: object) -> None:
        self._client = client

    async def send_text(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> int:
        """发送文字消息，返回消息 ID（失败返回 -1）"""
        try:
            msg = await self._client.send_message(  # type: ignore[attr-defined]
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                parse_mode=enums.ParseMode.HTML,
            )
            return msg.id
        except Exception:
            return -1

    async def send_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> int:
        """发送图片消息，返回消息 ID（失败返回 -1）"""
        try:
            msg = await self._client.send_photo(  # type: ignore[attr-defined]
                chat_id=chat_id,
                photo=photo_path,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
                parse_mode=enums.ParseMode.HTML,
            )
            return msg.id
        except Exception:
            return -1

    async def delete_message(self, chat_id: int, message_id: int, delay: int = 0) -> bool:
        import asyncio
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self._client.delete_messages(  # type: ignore[attr-defined]
                chat_id=chat_id,
                message_ids=message_id,
            )
            return True
        except Exception:
            return False

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> bool:
        try:
            await self._client.edit_message_text(  # type: ignore[attr-defined]
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
            return True
        except Exception:
            return False


class PyrogramNotifier:
    """
    NotificationPort 的 Pyrogram 实现

    所有通知都发往 notify_chat_id（通常是 BOT_MESSAGE_CHAT）
    """

    def __init__(self, bot_client: object) -> None:
        self._bot = bot_client

    async def notify_snatch_result(
        self,
        result: SnatchResult,
        notify_chat_id: int,
    ) -> None:
        text = _format_snatch_result(result)
        if text:
            await self._send(notify_chat_id, text)

    async def notify_lottery_result(
        self,
        result: ParticipationResult,
        notify_chat_id: int,
    ) -> None:
        text = _format_lottery_result(result)
        if text:
            await self._send(notify_chat_id, text)

    async def notify_text(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = None,
    ) -> None:
        await self._send(chat_id, text, parse_mode=parse_mode)

    async def notify_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: Optional[str] = None,
    ) -> int:
        try:
            msg = await self._bot.send_photo(  # type: ignore[attr-defined]
                chat_id=chat_id,
                photo=photo_path,
                caption=caption,
            )
            return msg.id
        except Exception as e:
            from libs.log import logger
            logger.error(f"[Notifier] 图片发送失败: {e}, chat_id={chat_id}")
            return -1

    async def delete_message(self, chat_id: int, message_id: int, delay: int = 0) -> bool:
        import asyncio
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self._bot.delete_messages(  # type: ignore[attr-defined]
                chat_id=chat_id,
                message_ids=message_id,
            )
            return True
        except Exception:
            return False

    async def _send(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = None,
    ) -> None:
        try:
            # 默认切换为 HTML 模式，以支持新版美化文案
            pm = parse_mode if parse_mode is not None else enums.ParseMode.HTML
            await self._bot.send_message(  # type: ignore[attr-defined]
                chat_id=chat_id,
                text=text,
                parse_mode=pm,
            )
        except Exception as e:
            from libs.log import logger
            logger.error(f"[Notifier] 消息发送失败: {e}, chat_id={chat_id}")


# ------------------------------------------------------------------ #
# 通知文案格式化（从现有 _send_snatch_notify 迁移）                  #
# ------------------------------------------------------------------ #

def _format_snatch_result(result: SnatchResult) -> str:
    from core.domain.red_packet import SnatchStatus  # noqa: PLC0415

    status = result.status
    packet = result.packet
    keyword = result.keyword or "（未知）"

    if status == SnatchStatus.SENT:
        return (
            f"🧧 抢红包成功\n\n"
            f"🏠 群组 ID：{packet.group_id}\n"
            f"🔑 发送口令：{keyword}"
        )
    if status == SnatchStatus.BLOCKED:
        return (
            f"🛡️ 抢红包已拦截（陷阱）\n\n"
            f"🏠 群组 ID：{packet.group_id}\n"
            f"⚠️ 原因：{result.reason}"
        )
    if status == SnatchStatus.CONFIRMED:
        return (
            f"✅ 抢红包已确认\n\n"
            f"🏠 群组 ID：{packet.group_id}\n"
            f"🔑 口令：{keyword}"
        )
    if status == SnatchStatus.FAILED:
        if "OCR口令" in (result.reason or ""):
            return (
                f"🔍 OCR口令识别有误\n\n"
                f"🏠 群组 ID：{packet.group_id}\n"
                f"🔑 识别口令：{keyword}（有误）"
            )
        return (
            f"❌ 抢红包发送失败\n\n"
            f"🏠 群组 ID：{packet.group_id}\n"
            f"⚠️ 原因：{result.reason}"
        )
    if status == SnatchStatus.OCR_FAILED:
        return (
            f"⚠️ 口令红包 OCR 失败\n\n"
            f"🏠 群组 ID：{packet.group_id}\n"
            f"📋 已切换为复制模式"
        )
    return ""


def _format_lottery_result(result: ParticipationResult) -> str:
    from core.domain.lottery import LotteryStatus  # noqa: PLC0415

    status = result.status
    event = result.lottery

    if status == LotteryStatus.JOINED:
        return (
            f"✅ 抽奖参与成功\n\n"
            f"🆔 抽奖 ID：{event.message_id}\n"
            f"🎁 奖品：{event.prize_text}\n"
            f"🏠 群组：{event.group_id}"
        )
    if status == LotteryStatus.BLOCKED:
        return (
            f"🛡️ 陷阱抽奖已拦截\n\n"
            f"🆔 抽奖 ID：{event.message_id}\n"
            f"⚠️ 原因：{result.reason}"
        )
    if status == LotteryStatus.FAILED:
        return (
            f"❌ 抽奖参与失败\n\n"
            f"🆔 抽奖 ID：{event.message_id}\n"
            f"⚠️ 原因：{result.reason}"
        )
    return ""
