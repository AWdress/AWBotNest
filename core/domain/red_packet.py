"""
core/domain/red_packet.py
红包领域模型

依赖方向：domain 层不依赖任何外部框架
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SnatchStatus(str, Enum):
    """抢红包状态"""
    SENT = "sent"                 # 已发送口令
    BLOCKED = "blocked"           # 陷阱拦截
    OCR_FAILED = "ocr_failed"     # OCR 识别失败（退回复制模式）
    PENDING_COPY = "pending_copy" # 等待他人参与后复制
    CONFIRMED = "confirmed"       # 已确认抢到
    FAILED = "failed"             # 发送失败
    EXPIRED = "expired"           # 超时清理


class OcrMode(str, Enum):
    """OCR 模式"""
    ON = "on"
    OFF = "off"


@dataclass
class OcrResult:
    """OCR 识别结果"""
    raw_bytes: bytes
    keyword: str
    confidence: float = 0.0       # 0.0~1.0 置信度（可选，ddddocr 不提供）
    model_used: str = "default"   # "default" | "old"
    threshold: Optional[int] = None  # 二值化阈值（None=原图）
    elapsed_ms: float = 0.0
    success: bool = True
    error: str = ""


@dataclass
class RedPacketMessage:
    """红包消息 - 从 Telegram 消息解析"""
    group_id: int
    message_id: int
    sender_id: int
    sender_name: str
    caption: str                  # 消息说明文字（含"口令红包"等）
    has_photo: bool = False
    has_document: bool = False
    account_id: int = 0            # 发起抢包的账号 user_id（多账号去重用）
    created_at: datetime = field(default_factory=datetime.now)
    # OCR 识别的口令
    ocr_keyword: Optional[str] = None
    ocr_result: Optional[OcrResult] = None


@dataclass
class SnatchTarget:
    """抢红包目标配置"""
    group_id: int
    user_id: int                  # 目标用户 UID
    join_delay: float = 0.0       # 参与延迟（秒）


@dataclass
class SnatchResult:
    """抢红包结果"""
    packet: RedPacketMessage
    target: SnatchTarget
    status: SnatchStatus
    keyword: Optional[str] = None
    sent_message_id: Optional[int] = None
    reason: str = ""
    is_trap: bool = False
    snatched_at: Optional[datetime] = None
