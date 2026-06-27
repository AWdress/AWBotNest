"""
core/domain/ydx.py
YDX 游戏领域模型
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class YdxRecord:
    """YDX 投注记录 — 对应 zhuque_ydx 表"""
    website: str
    die_point: int
    lottery_result: str
    consecutive_count: int
    bet_side: str
    bet_count: int
    bet_amount: float
    win_amount: float
    recorded_at: datetime = field(default_factory=datetime.now)
