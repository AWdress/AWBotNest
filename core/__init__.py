"""
core/__init__.py
核心层统一出口 - 所有外部模块通过此包引用核心组件

使用方式：
    from core import Client, filters, Message, logger, config
    from core.domain import LotteryEvent, TransferRecord
    from core.services import TransferService, RedPacketService
"""

from __future__ import annotations

# ──────────────────────────────────────────────
# 日志（libs/log.py 过渡 → infra/logging.py）
# ──────────────────────────────────────────────
from libs.log import logger

# ──────────────────────────────────────────────
# Pyrogram 类型 / 过滤器（转发 pyrogram 标准导出）
# ──────────────────────────────────────────────
from core.telegram import (
    Client as _BaseClient,
    filters, enums, idle, StopPropagation, PYROGRAM_VERSION,
    create, Filter, MessageHandler,
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, User,
    WebAppInfo, BotCommand, BotCommandScopeAllPrivateChats,
    InlineQueryResultArticle, InputTextMessageContent,
    InlineQuery, ChosenInlineResult,
    InputMediaDocument, InputMediaPhoto, LinkPreviewOptions,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply,
    PeerIdInvalid, RPCError, FloodWait,
    AuthKeyInvalid, Unauthorized, Forbidden, MessageNotModified, BadRequest,
    SQLiteStorage,
)

# 兼容兜底：在导入封装 Client 前先暴露基础 Client，避免旧代码 `from core import Client` 触发循环导入失败
Client = _BaseClient

# ──────────────────────────────────────────────
# Telegram Client 封装（libs/custom_client.py）
# ──────────────────────────────────────────────
from libs.custom_client import Client
from core.manager import manager

# ──────────────────────────────────────────────
# 配置（infra/config.py 作为统一入口）
# ──────────────────────────────────────────────
from infra.config import get_settings, AppSettings

config = get_settings()
API_ID = config.telegram.api_id
API_HASH = config.telegram.api_hash.get_secret_value()
BOT_TOKEN = config.telegram.bot_token.get_secret_value()
MY_TGID = config.telegram.my_tgid

# ──────────────────────────────────────────────
# 领域实体（core/domain/）
# ──────────────────────────────────────────────
from core.domain import (
    LotteryEvent, ParticipationResult, LotteryStatus,
    RedPacketMessage, SnatchTarget, SnatchResult, SnatchStatus,
    TransferRecord, TransferDirection, LeaderboardEntry,
    BombGameState, GamePhase,
    TelegramUser,
    AiMessage, AiConversation, AiConfig,
)

# ──────────────────────────────────────────────
# 业务服务（core/services/）
# ──────────────────────────────────────────────
from core.services import (
    LotteryService, TrapService, RedPacketService,
    TransferService, PrizeService, AiService,
)

# ──────────────────────────────────────────────
# 便捷函数
# ──────────────────────────────────────────────

def get_container():
    """获取 DI 容器实例"""
    from infra.container import get_container as _gc
    return _gc()


__all__ = [
    # 核心基础
    "Client", "filters", "enums", "Message", "CallbackQuery", "User", "WebAppInfo",
    "BotCommand", "BotCommandScopeAllPrivateChats",
    "InlineKeyboardMarkup", "InlineKeyboardButton", "StopPropagation",
    "InlineQueryResultArticle", "InputTextMessageContent",
    "InlineQuery", "ChosenInlineResult",
    "InputMediaDocument", "InputMediaPhoto", "LinkPreviewOptions",
    "ReplyKeyboardMarkup", "ReplyKeyboardRemove", "ForceReply",
    "PeerIdInvalid", "RPCError", "FloodWait",
    "AuthKeyInvalid", "Unauthorized", "Forbidden", "MessageNotModified", "BadRequest",
    "create", "Filter", "MessageHandler", "SQLiteStorage", "idle", "PYROGRAM_VERSION",
    "logger", "config", "get_settings",
    "manager", "API_ID", "API_HASH", "BOT_TOKEN", "MY_TGID",
    # 领域实体
    "LotteryEvent", "ParticipationResult", "LotteryStatus",
    "RedPacketMessage", "SnatchTarget", "SnatchResult", "SnatchStatus",
    "TransferRecord", "TransferDirection", "LeaderboardEntry",
    "BombGameState", "GamePhase",
    "TelegramUser",
    "AiMessage", "AiConversation", "AiConfig",
    # 服务
    "LotteryService", "TrapService", "RedPacketService",
    "TransferService", "PrizeService", "AiService",
    # 基础设施
    "get_container",
]

