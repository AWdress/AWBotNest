"""
core/services/lottery_service.py
抽奖业务逻辑服务

职责：
- 抽奖信息解析（extract_lottery_info）
- 奖品匹配（is_prize_matched）
- 参与方式决策（forward / text / forward_first）
- 时间窗口检查
- 不依赖 Pyrogram / 数据库

依赖：
    core/ports/messaging.py  -> MessageSender, NotificationPort
    core/ports/storage.py    -> StateRepository
    core/services/trap_service.py -> TrapService
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from random import randint
from typing import Optional

from core.domain.lottery import (
    LotteryEvent, LotteryStatus,
    ParticipationMethod, ParticipationResult,
)
from core.ports.messaging import MessageSender, NotificationPort
from core.ports.storage import StateRepository
from core.services.trap_service import TrapService


# 状态 section 名
_SECTION = "AUTO_LOTTERY"
_SECTION_TIME = "LOTTERY_WAIT_TIME"


@dataclass
class LotteryConfig:
    """抽奖配置（从 config.py 读入）"""
    target_groups: list[int] = field(default_factory=list)
    # 每个群组有奖品列表  {group_id: [prize_kw1, prize_kw2, ...]}
    prize_list: dict[int, list[str]] = field(default_factory=dict)
    case_sensitive: bool = False
    # 等待时间范围（秒）
    wait_min: int = 3
    wait_max: int = 20
    # 参与时间窗口 [(start_hour, end_hour), ...]
    active_windows: list[tuple[int, int]] = field(default_factory=list)


class LotteryService:
    """
    抽奖参与决策服务

    使用方式：
        service = LotteryService(config, state, sender, notifier, trap)
        result = await service.handle_lottery_announcement(event)
    """

    def __init__(
        self,
        config: LotteryConfig,
        state: StateRepository,
        sender: MessageSender,
        notifier: NotificationPort,
        trap: TrapService,
        notify_chat_id: int,
    ) -> None:
        self._config = config
        self._state = state
        self._sender = sender
        self._notifier = notifier
        self._trap = trap
        self._notify_chat_id = notify_chat_id

        # 内部抽奖缓存（替代全局 lottery_list）
        # key: lottery_id -> dict
        self._lottery_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # 主入口                                                               #
    # ------------------------------------------------------------------ #

    async def handle_lottery_announcement(
        self,
        event: LotteryEvent,
    ) -> ParticipationResult:
        """
        处理新抽奖公告

        Returns:
            ParticipationResult，status 可能为：
            - JOINED: 成功发送参与消息
            - SKIPPED: 奖品不符合 / 不在时间窗口 / 开关关闭
            - BLOCKED: 陷阱拦截
            - FAILED: 发送失败
        """
        # 1. 开关检查
        if not self._is_enabled():
            return ParticipationResult(
                lottery=event,
                status=LotteryStatus.SKIPPED,
                reason="自动抽奖开关未开启",
            )

        # 2. 时间窗口检查
        if not self._in_active_window():
            return ParticipationResult(
                lottery=event,
                status=LotteryStatus.SKIPPED,
                reason="不在设定的自动抽奖时间内",
            )

        # 3. 奖品匹配
        matched_prize = self._match_prize(event)
        if not matched_prize:
            return ParticipationResult(
                lottery=event,
                status=LotteryStatus.SKIPPED,
                reason="奖品不符合设定范围",
            )
        event.matched_prize = matched_prize

        # 4. 陷阱检测
        lottery_info = {
            "prize": event.prize_text,
            "boss_ID": event.creator_id,
        }
        trap_result = self._trap.check(event.raw_text, lottery_info)
        if trap_result.is_trap:
            result = ParticipationResult(
                lottery=event,
                status=LotteryStatus.BLOCKED,
                reason=f"陷阱抽奖: {'; '.join(trap_result.reasons)}",
            )
            await self._notifier.notify_lottery_result(result, self._notify_chat_id)
            return result

        # 5. 随机等待
        wait_sec = self._get_wait_seconds()
        import asyncio
        await asyncio.sleep(wait_sec)

        # 6. 确认抽奖还在（超时期间可能已结束）
        if event.message_id not in [v.get("msg_id") for v in self._lottery_cache.values()]:
            pass  # 抽奖仍有效，继续

        # 7. 发送参与消息
        keyword = self._get_keyword(event)
        method = self._decide_method(keyword)

        sent_id = await self._do_send(event, keyword, method)

        if sent_id <= 0:
            result = ParticipationResult(
                lottery=event,
                status=LotteryStatus.FAILED,
                method=method,
                reason="发送参与消息失败",
            )
            await self._notifier.notify_lottery_result(result, self._notify_chat_id)
            return result

        result = ParticipationResult(
            lottery=event,
            status=LotteryStatus.JOINED,
            method=method,
            sent_message_id=sent_id,
            participated_at=datetime.now(),
        )
        await self._notifier.notify_lottery_result(result, self._notify_chat_id)
        return result

    def register_lottery(self, lottery_id: str, info: dict) -> None:
        """注册一个新抽奖到内部缓存"""
        self._lottery_cache[lottery_id] = info

    def remove_lottery(self, lottery_id: str) -> None:
        """从缓存中移除抽奖（抽奖结束时调用）"""
        self._lottery_cache.pop(lottery_id, None)

    def get_lottery(self, lottery_id: str) -> Optional[dict]:
        """获取缓存中的抽奖信息"""
        return self._lottery_cache.get(lottery_id)

    def pre_check(self, event: "LotteryEvent", my_tgid: int = 0) -> "LotteryDecision":
        """
        执行所有前置检查（开关、时间窗口、奖品匹配、陷阱检测），
        但不发送消息。用于需要自定义发送逻辑的场景。

        Returns:
            LotteryDecision 包含是否参与和等待时间
        """
        from config.config import MY_TGID as _fallback_tgid
        _my_id = my_tgid or _fallback_tgid

        # 排除自己发起的抽奖
        if event.creator_id and str(event.creator_id) == str(_my_id):
            return LotteryDecision(should_participate=False, reason="抽奖是由自己发起的")

        if not self._is_enabled():
            return LotteryDecision(should_participate=False, reason="自动抽奖开关未开启")

        if not self._in_active_window():
            return LotteryDecision(should_participate=False, reason="不在设定的自动抽奖时间内")

        matched = self._match_prize(event)
        if not matched:
            return LotteryDecision(should_participate=False, reason="奖品不符合设定范围")

        lottery_info_dict = {
            "prize": event.prize_text,
            "boss_ID": str(event.creator_id),
            "all_prizes": getattr(event, "all_prizes", [event.prize_text]),
            "keyword": getattr(event, "keyword", ""),
            "description": getattr(event, "description", ""),
            "max_participants": getattr(event, "max_participants", None),
        }
        trap_result = self._trap.check(event.raw_text, lottery_info_dict)
        if trap_result.is_trap:
            return LotteryDecision(
                should_participate=False,
                reason=f"陷阱抽奖: {'; '.join(trap_result.reasons)}",
                is_trap=True,
            )

        return LotteryDecision(
            should_participate=True,
            matched_prize=matched,
            wait_seconds=self._get_wait_seconds(),
        )

    # ------------------------------------------------------------------ #
    # 奖品匹配                                                             #
    # ------------------------------------------------------------------ #

    def _match_prize(self, event: LotteryEvent) -> Optional[str]:
        """
        从 prize_list 中找匹配的奖品关键词。
        返回匹配到的群组 ID (Key)，兼容旧版逻辑。
        """
        prize_text = event.prize_text
        group_id_str = str(event.group_id)

        # 1. 群组专属奖品列表 (优先匹配当前群组)
        group_prizes = self._config.prize_list.get(group_id_str, [])
        if self._find_prize_in_list(prize_text, group_prizes):
            return group_id_str

        # 2. 通用匹配 - 如果专属列表没匹配到，尝试匹配全局所有关键词 (兼容旧版行为)
        universal_enabled = (
            self._state.get(_SECTION, "universal_prize_match", "on") == "on"
        )
        if universal_enabled:
            for group_key, prizes in self._config.prize_list.items():
                if self._find_prize_in_list(prize_text, prizes):
                    return group_key

        return None

    def _find_prize_in_list(self, prize_text: str, keywords: list[str]) -> Optional[str]:
        if not self._config.case_sensitive:
            prize_text = prize_text.lower()
            for kw in keywords:
                if kw.lower() in prize_text:
                    return kw
        else:
            for kw in keywords:
                if kw in prize_text:
                    return kw
        return None

    # ------------------------------------------------------------------ #
    # 参与方式决策                                                         #
    # ------------------------------------------------------------------ #

    def _decide_method(self, keyword: str) -> ParticipationMethod:
        """决定参与方式"""
        if _has_markdown_format(keyword):
            return ParticipationMethod.FORWARD

        forward_first = (
            self._state.get(_SECTION, "lottery_forward_first_participant", "off") == "on"
        )
        if forward_first:
            return ParticipationMethod.FORWARD_FIRST

        forward = (
            self._state.get(_SECTION, "lottery_forward_enabled", "off") == "on"
        )
        if forward:
            return ParticipationMethod.FORWARD

        return ParticipationMethod.KEYWORD

    def _get_keyword(self, event: LotteryEvent) -> str:
        """从 lottery_cache 获取对应的参与关键词"""
        for info in self._lottery_cache.values():
            if info.get("msg_id") == event.message_id:
                return str(info.get("keyword", ""))
        return ""

    async def _do_send(
        self,
        event: LotteryEvent,
        keyword: str,
        method: ParticipationMethod,
    ) -> int:
        """
        执行发送操作，返回消息 ID（失败返回 -1）

        注：FORWARD / FORWARD_FIRST 方式需要 MessageSender 支持转发，
        此处统一 fallback 为文字发送（适配层可根据 method 实现转发）。
        """
        reply_to = event.reply_to_id
        try:
            sent_id = await self._sender.send_text(
                chat_id=event.group_id,
                text=keyword,
                reply_to_message_id=reply_to,
            )
            return sent_id
        except Exception:
            return -1

    # ------------------------------------------------------------------ #
    # 开关 / 时间窗口                                                     #
    # ------------------------------------------------------------------ #

    def _is_enabled(self) -> bool:
        return self._state.get(_SECTION, "auto_lottery_enabled", "off") == "on"

    def _in_active_window(self) -> bool:
        """检查当前时间是否在允许参与的时间窗口内"""
        if not self._config.active_windows:
            return True  # 未配置则全天可用
        now_hour = datetime.now().hour
        for start, end in self._config.active_windows:
            if start <= now_hour < end:
                return True
        return False

    def _get_wait_seconds(self) -> int:
        try:
            wait_min = int(
                self._state.get(_SECTION_TIME, "lottery_wait_time_min", str(self._config.wait_min))
            )
            wait_max = int(
                self._state.get(_SECTION_TIME, "lottery_wait_time_max", str(self._config.wait_max))
            )
        except (ValueError, TypeError):
            wait_min, wait_max = self._config.wait_min, self._config.wait_max
        return randint(wait_min, wait_max)


# ------------------------------------------------------------------ #
# 工具函数                                                            #
# ------------------------------------------------------------------ #

def _has_markdown_format(text: str) -> bool:
    """检测文本是否包含需要转发的特殊格式（@、/ 等）"""
    if not text:
        return False
    patterns = [
        r"\*\*", r"__", r"(?<!\*)\*(?!\*)", r"(?<!_)_(?!_)",
        r"`", r"~~", r"^/", r"\s/", r"^@", r"\s@",
        r"^#", r"\s#", r"\[.+\]\(.+\)",
    ]
    for pattern in patterns:
        if re.search(pattern, text):
            return True
    return False


def extract_lottery_id(message_text: str) -> Optional[str]:
    """从消息文字提取抽奖 ID"""
    match = re.search(r"抽奖 ID[：:]\s*(.+?)[\n\r]", message_text)
    return match.group(1).strip() if match else None


@dataclass
class LotteryDecision:
    """pre_check 的返回结果"""
    should_participate: bool
    matched_prize: str = ""
    wait_seconds: int = 0
    reason: str = ""
    is_trap: bool = False
