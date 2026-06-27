"""
core/services/red_packet_service.py
红包业务逻辑服务

职责：
- OCR 识别口令（通过 OcrPort 接口）
- 陷阱检测（通过 TrapService）
- 管理抢红包状态机（pending_copy / ocr_sent / confirmed）
- 不依赖 Pyrogram / SQLAlchemy

依赖：
    core/ports/ocr.py        -> OcrPort
    core/ports/messaging.py  -> MessageSender, NotificationPort
    core/ports/storage.py    -> StateRepository
    core/services/trap_service.py -> TrapService
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.domain.red_packet import (
    OcrMode, OcrResult, RedPacketMessage,
    SnatchStatus, SnatchTarget, SnatchResult,
)
from core.ports.messaging import MessageSender, NotificationPort
from core.ports.ocr import OcrPort
from core.ports.storage import StateRepository
from core.services.trap_service import TrapService
from core import logger


_STATE_SECTION = "REDPACKET_SNATCH"
_OCR_TIMEOUT = 30.0  # OCR 发送口令后等待确认的超时时间（秒）
_MAX_PROCESSED_PACKET_CACHE = 500  # 已处理红包缓存上限


@dataclass
class _PendingCopy:
    """等待复制模式的红包条目"""
    packet: RedPacketMessage
    target: SnatchTarget
    expires_at: float  # time.monotonic()


@dataclass
class _OcrSent:
    """已通过 OCR 发送口令，等待确认的条目"""
    packet: RedPacketMessage
    target: SnatchTarget
    keyword: str
    sent_message_id: int
    sent_at: datetime = field(default_factory=datetime.now)
    timeout_task: Optional[asyncio.Task] = field(default=None, repr=False)


class RedPacketService:
    """
    红包抢夺业务服务

    使用方式：
        service = RedPacketService(ocr=..., sender=..., notifier=..., state=..., trap=...)
        # 收到新红包消息
        result = await service.handle_new_packet(packet, target)
        # 收到"口令不对"回复
        await service.handle_failure_reply(chat_id, reply_to_id)
        # 收到"抢到了"回复
        await service.handle_success_reply(chat_id, reply_to_id, from_user_id)
    """

    def __init__(
        self,
        ocr: OcrPort,
        sender: MessageSender,
        notifier: NotificationPort,
        state: StateRepository,
        trap: TrapService,
        notify_chat_id: int,
    ) -> None:
        self._ocr = ocr
        self._sender = sender
        self._notifier = notifier
        self._state = state
        self._trap = trap
        self._notify_chat_id = notify_chat_id

        # 内部状态（替代全局变量）
        self._pending_copy: dict[tuple[int, int], _PendingCopy] = {}
        self._ocr_sent: dict[tuple[int, int], _OcrSent] = {}
        # key=(chat_id, reply_msg_id) -> (keyword, orig_packet_msg_id)
        self._reply_cache: dict[tuple[int, int], tuple[str, int]] = {}
        # 去重缓存：同一红包消息仅处理一次
        self._processed_packets: set[tuple[int, int]] = set()
        self._processed_packet_queue: deque[tuple[int, int]] = deque()

    # ------------------------------------------------------------------ #
    # 主入口                                                               #
    # ------------------------------------------------------------------ #

    async def handle_new_packet(
        self,
        packet: RedPacketMessage,
        target: SnatchTarget,
    ) -> SnatchResult:
        """
        处理新收到的口令红包消息

        Returns:
            SnatchResult，status 可能为：
            - SENT: OCR 识别成功，已发送口令
            - BLOCKED: 陷阱检测拦截
            - PENDING_COPY: OCR 关闭或识别失败，进入等待复制模式
        """
        # 同一红包去重（避免重复发送和重复进入等待复制）
        packet_key = (packet.account_id, packet.group_id, packet.message_id)
        if packet_key in self._processed_packets:
            logger.info(
                f"[抢红包 服务] 重复红包跳过 chat={packet.group_id} packet_msg={packet.message_id}"
            )
            return SnatchResult(
                packet=packet,
                target=target,
                status=SnatchStatus.EXPIRED,
                reason="重复红包消息，已跳过",
            )
        self._mark_packet_processed(packet_key)

        # 陷阱关键词检测（对 caption 做检测）
        trap_result = self._trap.check(packet.caption)
        if trap_result.is_trap:
            logger.info(
                f"[抢红包 服务] 陷阱拦截 chat={packet.group_id} packet_msg={packet.message_id} "
                f"reasons={'; '.join(trap_result.reasons)}"
            )
            result = SnatchResult(
                packet=packet,
                target=target,
                status=SnatchStatus.BLOCKED,
                reason=f"陷阱口令，已拦截: {'; '.join(trap_result.reasons)}",
                is_trap=True,
            )
            await self._notifier.notify_snatch_result(result, self._notify_chat_id)
            return result

        ocr_mode = self._get_ocr_mode()

        if ocr_mode == OcrMode.OFF:
            logger.info(
                f"[抢红包 服务] OCR关闭转等待复制 chat={packet.group_id} packet_msg={packet.message_id}"
            )
            return await self._enter_pending_copy(packet, target, "OCR 关闭，等待复制模式")

        # OCR 模式
        # 检查是否有图片字节可供识别
        image_bytes = getattr(packet, "_image_bytes", None)
        if not image_bytes:
            logger.info(
                f"[抢红包 服务] 无图片字节转等待复制 chat={packet.group_id} packet_msg={packet.message_id}"
            )
            return await self._enter_pending_copy(packet, target, "无图片字节，无法 OCR")

        # 尝试 OCR 识别
        ocr_result = await self._run_ocr(packet)
        if not ocr_result.success or not ocr_result.keyword:
            logger.info(
                f"[抢红包 服务] OCR失败转等待复制 chat={packet.group_id} packet_msg={packet.message_id} "
                f"error={ocr_result.error or 'empty'}"
            )
            result = SnatchResult(
                packet=packet,
                target=target,
                status=SnatchStatus.OCR_FAILED,
                reason="OCR 识别失败，退回复制模式",
            )
            await self._notifier.notify_snatch_result(result, self._notify_chat_id)
            return await self._enter_pending_copy(packet, target, "OCR 识别失败")

        # 发送口令
        sent_id = await self._sender.send_text(
            chat_id=packet.group_id,
            text=ocr_result.keyword,
            reply_to_message_id=packet.message_id,
        )

        if sent_id <= 0:
            logger.warning(
                f"[抢红包 服务] OCR口令发送失败 chat={packet.group_id} packet_msg={packet.message_id}"
            )
            result = SnatchResult(
                packet=packet,
                target=target,
                status=SnatchStatus.FAILED,
                reason="口令发送失败",
            )
            await self._notifier.notify_snatch_result(result, self._notify_chat_id)
            return result

        # 注册到 ocr_sent 状态，等待确认
        entry = _OcrSent(
            packet=packet,
            target=target,
            keyword=ocr_result.keyword,
            sent_message_id=sent_id,
        )
        self._ocr_sent[(packet.group_id, sent_id)] = entry
        entry.timeout_task = asyncio.create_task(
            self._ocr_timeout_cleanup(packet.group_id, sent_id)
        )
        logger.info(
            f"[抢红包 服务] OCR口令已发送 chat={packet.group_id} packet_msg={packet.message_id} "
            f"sent_msg={sent_id} keyword=[{ocr_result.keyword}]"
        )

        result = SnatchResult(
            packet=packet,
            target=target,
            status=SnatchStatus.SENT,
            keyword=ocr_result.keyword,
            sent_message_id=sent_id,
        )
        await self._notifier.notify_snatch_result(result, self._notify_chat_id)
        return result

    async def handle_failure_reply(
        self, chat_id: int, reply_to_id: int
    ) -> Optional[SnatchResult]:
        """
        处理"口令不对"或"口令错误"回复

        如果 reply_to_id 对应 ocr_sent 中的记录，触发失败通知并清理。
        """
        key = (chat_id, reply_to_id)
        entry = self._ocr_sent.pop(key, None)
        if entry is None:
            logger.info(
                f"[抢红包 服务] 失败确认未命中 chat={chat_id} reply_to={reply_to_id}"
            )
            return None

        if entry.timeout_task:
            entry.timeout_task.cancel()

        result = SnatchResult(
            packet=entry.packet,
            target=entry.target,
            status=SnatchStatus.FAILED,
            keyword=entry.keyword,
            sent_message_id=entry.sent_message_id,
            reason="OCR口令识别有误",
        )
        logger.info(
            f"[抢红包 服务] OCR失败已确认 chat={chat_id} reply_to={reply_to_id} "
            f"keyword=[{entry.keyword}]"
        )
        await self._notifier.notify_snatch_result(result, self._notify_chat_id)
        return result

    async def handle_success_reply(
        self,
        chat_id: int,
        reply_to_id: int,
        from_user_id: int,
        reply_text: str,
    ) -> Optional[SnatchResult]:
        """
        处理"抢到了"成功确认回复

        两条路径：
        1. reply_to_id 是 ocr_sent 的 sent_message_id → OCR 确认成功
        2. reply_to_id 是 _reply_cache 中的 red_packet_msg_id → 复制模式确认
        """
        # 路径 1: OCR 确认
        key = (chat_id, reply_to_id)
        ocr_entry = self._ocr_sent.pop(key, None)
        if ocr_entry is not None:
            if ocr_entry.timeout_task:
                ocr_entry.timeout_task.cancel()
            result = SnatchResult(
                packet=ocr_entry.packet,
                target=ocr_entry.target,
                status=SnatchStatus.CONFIRMED,
                keyword=ocr_entry.keyword,
                sent_message_id=ocr_entry.sent_message_id,
                snatched_at=datetime.now(),
            )
            logger.info(
                f"[抢红包 服务] OCR成功确认 chat={chat_id} reply_to={reply_to_id} "
                f"keyword=[{ocr_entry.keyword}]"
            )
            await self._notifier.notify_snatch_result(result, self._notify_chat_id)
            return result

        # 路径 2: 复制模式（通过 reply_cache 查找口令并转发）
        cached = self._reply_cache.get(key) if reply_to_id > 0 else None

        # fallback A: 某些红包机器人会把成功消息回复到“红包主消息”而不是“口令回复消息”
        if cached is None and reply_to_id > 0 and (chat_id, reply_to_id) in self._pending_copy:
            cached = self._find_latest_cached_for_packet(chat_id, reply_to_id)
            if cached is not None:
                logger.info(
                    f"[抢红包 服务] 成功确认fallback命中(按红包主消息) chat={chat_id} packet_msg={reply_to_id}"
                )

        # fallback B: 某些成功消息没有 reply_to，若当前群仅有一个 pending，则按该 pending 的最新缓存口令兜底
        if cached is None:
            only_pending_packet = self._get_only_pending_packet_id(chat_id)
            if only_pending_packet is not None:
                cached = self._find_latest_cached_for_packet(chat_id, only_pending_packet)
                if cached is not None:
                    logger.info(
                        f"[抢红包 服务] 成功确认fallback命中(单pending兜底) chat={chat_id} packet_msg={only_pending_packet}"
                    )

        if cached is None:
            logger.info(
                f"[抢红包 服务] 成功确认未命中缓存 chat={chat_id} reply_to={reply_to_id}"
            )
            return None
        keyword, orig_packet_id = cached

        pending_key = (chat_id, orig_packet_id)
        pending = self._pending_copy.pop(pending_key, None)
        if pending is None:
            logger.info(
                f"[抢红包 服务] 找到缓存口令但无pending chat={chat_id} packet_msg={orig_packet_id}"
            )
            return None

        # 等待 join_delay 后发送口令
        if pending.target.join_delay > 0:
            await asyncio.sleep(pending.target.join_delay)

        sent_id = await self._sender.send_text(
            chat_id=chat_id,
            text=keyword,
            reply_to_message_id=orig_packet_id,
        )

        result = SnatchResult(
            packet=pending.packet,
            target=pending.target,
            status=SnatchStatus.CONFIRMED if sent_id > 0 else SnatchStatus.FAILED,
            keyword=keyword,
            sent_message_id=sent_id if sent_id > 0 else None,
            snatched_at=datetime.now(),
        )
        logger.info(
            f"[抢红包 服务] 复制模式发送 chat={chat_id} packet_msg={orig_packet_id} "
            f"sent_msg={sent_id} keyword=[{keyword}] status={result.status.value}"
        )
        await self._notifier.notify_snatch_result(result, self._notify_chat_id)
        return result

    def cache_reply(
        self,
        chat_id: int,
        reply_msg_id: int,
        keyword: str,
        orig_packet_id: int,
    ) -> None:
        """缓存他人回复（用于复制模式）"""
        self._reply_cache[(chat_id, reply_msg_id)] = (keyword, orig_packet_id)
        logger.info(
            f"[抢红包 服务] 缓存口令 chat={chat_id} reply_msg={reply_msg_id} packet_msg={orig_packet_id} "
            f"keyword=[{keyword}]"
        )

    def has_pending(self, chat_id: int, msg_id: int) -> bool:
        """检查是否有等待复制的红包（用于过滤无关回复）"""
        return (chat_id, msg_id) in self._pending_copy

    def has_cached_reply(self, chat_id: int, reply_msg_id: int) -> bool:
        '''检查是否有缓存回复（用于确认消息诊断）'''
        return (chat_id, reply_msg_id) in self._reply_cache

    def _find_latest_cached_for_packet(
        self,
        chat_id: int,
        packet_msg_id: int,
    ) -> Optional[tuple[str, int]]:
        for (c_id, _reply_msg_id), (keyword, orig_packet_id) in reversed(list(self._reply_cache.items())):
            if c_id == chat_id and orig_packet_id == packet_msg_id:
                return (keyword, orig_packet_id)
        return None

    def _get_only_pending_packet_id(self, chat_id: int) -> Optional[int]:
        packet_ids = [msg_id for (c_id, msg_id) in self._pending_copy.keys() if c_id == chat_id]
        if len(packet_ids) == 1:
            return packet_ids[0]
        return None

    def _mark_packet_processed(self, packet_key: tuple[int, int]) -> None:
        self._processed_packets.add(packet_key)
        self._processed_packet_queue.append(packet_key)
        while len(self._processed_packet_queue) > _MAX_PROCESSED_PACKET_CACHE:
            old_key = self._processed_packet_queue.popleft()
            self._processed_packets.discard(old_key)

    # ------------------------------------------------------------------ #
    # 内部方法                                                             #
    # ------------------------------------------------------------------ #

    def _get_ocr_mode(self) -> OcrMode:
        val = str(self._state.get(_STATE_SECTION, "ocr_enabled", "off") or "off")
        return OcrMode.ON if val == "on" else OcrMode.OFF

    async def _enter_pending_copy(
        self,
        packet: RedPacketMessage,
        target: SnatchTarget,
        reason: str,
    ) -> SnatchResult:
        import time
        entry = _PendingCopy(
            packet=packet,
            target=target,
            expires_at=time.monotonic() + 86400,  # 24h
        )
        self._pending_copy[(packet.group_id, packet.message_id)] = entry
        logger.info(
            f"[抢红包 服务] 进入等待复制 chat={packet.group_id} packet_msg={packet.message_id} reason={reason}"
        )
        return SnatchResult(
            packet=packet,
            target=target,
            status=SnatchStatus.PENDING_COPY,
            reason=reason,
        )

    async def _run_ocr(self, packet: RedPacketMessage) -> OcrResult:
        """委托给 OcrPort 识别"""
        # packet 的图片字节需由插件层传入，这里通过 packet.ocr_keyword 已设置
        # 如果没有原始字节，返回失败
        if not hasattr(packet, "_image_bytes") or not packet._image_bytes:  # type: ignore[attr-defined]
            return OcrResult(
                raw_bytes=b"",
                keyword="",
                success=False,
                error="无图片字节数据",
            )
        return await self._ocr.recognize(packet._image_bytes)  # type: ignore[attr-defined]

    async def _ocr_timeout_cleanup(self, chat_id: int, sent_id: int) -> None:
        """30s 超时后静默清理 ocr_sent 记录"""
        await asyncio.sleep(_OCR_TIMEOUT)
        removed = self._ocr_sent.pop((chat_id, sent_id), None)
        if removed is not None:
            logger.info(
                f"[抢红包 服务] OCR等待确认超时 chat={chat_id} sent_msg={sent_id} keyword=[{removed.keyword}]"
            )
