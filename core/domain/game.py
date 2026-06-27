"""
core/domain/game.py
炸弹游戏领域模型

依赖方向：domain 层不依赖任何外部框架
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class GamePhase(str, Enum):
    """游戏阶段"""
    WAITING = "waiting"          # 等待开始
    ACTIVE = "active"            # 进行中
    EXPLODED = "exploded"        # 已爆炸
    SAFE = "safe"                # 安全结束


class GuessResult(str, Enum):
    """猜测结果"""
    SAFE = "safe"                # 安全
    BOOM = "boom"                # 爆炸
    INVALID = "invalid"          # 无效（超出范围等）
    SKIPPED = "skipped"          # 跳过（已猜过）


@dataclass
class BombGameState:
    """炸弹游戏状态"""
    group_id: int
    game_id: int                 # 消息 ID 作为游戏 ID
    bomb_number: int             # 炸弹数字（不对外暴露）
    range_min: int
    range_max: int
    phase: GamePhase = GamePhase.ACTIVE
    participants: list[int] = field(default_factory=list)  # 参与者 user_ids
    guessed_numbers: list[int] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None
    loser_id: Optional[int] = None     # 踩到炸弹的用户


@dataclass
class GuessRecord:
    """猜测记录"""
    game_id: int
    user_id: int
    user_name: str
    number: int
    result: GuessResult
    guessed_at: datetime = field(default_factory=datetime.now)
