"""
core/domain/__init__.py
领域模型包 - 导出所有核心实体
"""
from core.domain.lottery import (
    LotteryStatus,
    ParticipationMethod,
    LotteryEvent,
    ParticipationResult,
    PrizeRecord,
)
from core.domain.red_packet import (
    SnatchStatus,
    OcrMode,
    OcrResult,
    RedPacketMessage,
    SnatchTarget,
    SnatchResult,
)
from core.domain.transfer import (
    TransferDirection,
    LeaderboardType,
    TransferRecord,
    LeaderboardEntry,
    RaidRecord,
)
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
from core.domain.ai import (
    AiMessage,
    AiConversation,
    AiConfig,
)

__all__ = [
    # Lottery
    "LotteryStatus", "ParticipationMethod", "LotteryEvent",
    "ParticipationResult", "PrizeRecord",
    # Red Packet
    "SnatchStatus", "OcrMode", "OcrResult", "RedPacketMessage",
    "SnatchTarget", "SnatchResult",
    # Transfer
    "TransferDirection", "LeaderboardType", "TransferRecord",
    "LeaderboardEntry", "RaidRecord",
    # Game
    "GamePhase", "GuessResult", "BombGameState", "GuessRecord",
    # User
    "UserRole", "TelegramUser",
    # AI
    "AiMessage", "AiConversation", "AiConfig",
]
