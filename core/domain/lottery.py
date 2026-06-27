"""
core/domain/lottery.py
抽奖领域模型 - 框架无关的纯 Python 数据类

依赖方向：domain 层不依赖任何外部框架
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class LotteryStatus(str, Enum):
    """抽奖状态"""
    PENDING = "pending"        # 等待参与
    JOINED = "joined"          # 已参与
    WON = "won"                # 已中奖
    SKIPPED = "skipped"        # 跳过（非目标奖品）
    BLOCKED = "blocked"        # 拦截（陷阱检测）
    FAILED = "failed"          # 失败（发送错误）
    EXPIRED = "expired"        # 过期（超时未参与）


class ParticipationMethod(str, Enum):
    """参与方式"""
    DIRECT = "direct"              # 直接回复参与
    FORWARD_FIRST = "forward"      # 转发第一个参与者
    KEYWORD = "keyword"            # 关键词回复


@dataclass
class LotteryEvent:
    """抽奖事件 - 从 Telegram 消息解析而来"""
    group_id: int
    message_id: int
    creator_id: int
    creator_name: str
    prize_text: str              # 原始奖品文字
    matched_prize: Optional[str]  # 匹配到的奖品关键词
    raw_text: str                # 原始消息文本
    created_at: datetime = field(default_factory=datetime.now)
    reply_to_id: Optional[int] = None  # 需要回复的消息ID


@dataclass
class ParticipationResult:
    """参与结果"""
    lottery: LotteryEvent
    status: LotteryStatus
    method: Optional[ParticipationMethod] = None
    reason: str = ""
    sent_message_id: Optional[int] = None
    participated_at: Optional[datetime] = None


@dataclass
class PrizeRecord:
    """中奖记录"""
    group_id: int
    message_id: int
    prize_name: str
    winner_id: int
    winner_name: str
    won_at: datetime = field(default_factory=datetime.now)
    sent: bool = False
    sent_at: Optional[datetime] = None
