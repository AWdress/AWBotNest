"""
core/domain/__init__.py
领域模型包 - 导出所有核心实体
"""
from core.domain.game import (
    GamePhase,
    GuessResult,
    BombGameState,
    GuessRecord,
)
from core.domain.user import (
    UserRole,
    TelegramUser,
)

__all__ = [
    # Game
    "GamePhase", "GuessResult", "BombGameState", "GuessRecord",
    # User
    "UserRole", "TelegramUser",
]
