"""
core/ports/messaging.py
消息发送接口 - Protocol 定义

依赖方向：ports 只知道 domain，不知道具体实现（Pyrogram）
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from core.domain.red_packet import SnatchResult
from core.domain.lottery import ParticipationResult


@runtime_checkable
class MessageSender(Protocol):
    """消息发送接口 - 适配器必须实现此协议"""

    async def send_text(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> int:
        """发送文字消息，返回消息 ID"""
        ...

    async def send_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> int:
        """发送图片消息，返回消息 ID"""
        ...

    async def delete_message(self, chat_id: int, message_id: int, delay: int = 0) -> bool:
        """删除消息，返回是否成功（支持延迟删除）"""
        ...

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> bool:
        """编辑消息，返回是否成功"""
        ...


@runtime_checkable
class NotificationPort(Protocol):
    """通知接口 - 向 Bot 通知频道发送各类状态通知"""

    async def notify_snatch_result(
        self,
        result: SnatchResult,
        notify_chat_id: int,
    ) -> None:
        """通知抢红包结果"""
        ...

    async def notify_lottery_result(
        self,
        result: ParticipationResult,
        notify_chat_id: int,
    ) -> None:
        """通知抽奖结果"""
        ...

    async def notify_text(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = None,
    ) -> None:
        """发送纯文字通知"""
        ...

    async def notify_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: Optional[str] = None,
    ) -> int:
        """发送图片通知，返回消息 ID"""
        ...

    async def delete_message(self, chat_id: int, message_id: int, delay: int = 0) -> bool:
        """删除消息"""
        ...


