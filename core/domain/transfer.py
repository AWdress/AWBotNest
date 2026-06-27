"""
core/domain/transfer.py
转账领域模型

依赖方向：domain 层不依赖任何外部框架
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class TransferDirection(str, Enum):
    """转账方向"""
    IN = "get"       # 收到转账
    OUT = "send"     # 发出转账


class LeaderboardType(str, Enum):
    """排行榜类型"""
    GET = "get"
    SEND = "send"
    ALL = "all"


@dataclass
class TransferRecord:
    """转账记录 - 对应数据库 transform 表"""
    website: str                 # 站点名称（zhuque, audiences, etc）
    direction: TransferDirection
    user_id: int
    user_name: str
    amount: Decimal
    bonus_name: str              # 货币单位（灵石/魔力/茉莉等）
    group_id: int
    message_id: int
    recorded_at: datetime = field(default_factory=datetime.now)
    id: Optional[int] = None


@dataclass
class LeaderboardEntry:
    """排行榜条目"""
    rank: int
    user_id: int
    user_name: str
    total_amount: Decimal
    count: int
    website: str
    bonus_name: str


@dataclass
class RaidRecord:
    """Raid 记录（集体活动）"""
    website: str
    user_id: int
    action: str
    raidcount: int
    bonus: Decimal
    created_at: datetime = field(default_factory=datetime.now)
    id: Optional[int] = None
