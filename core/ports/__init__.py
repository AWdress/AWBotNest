"""
core/ports/__init__.py
端口接口包
"""
from core.ports.messaging import MessageSender, NotificationPort
from core.ports.storage import (
    TransferRepository,
    RaidRepository,
    StateRepository,
    PrizeRepository,
)
from core.ports.ocr import OcrPort

__all__ = [
    "MessageSender",
    "NotificationPort",
    "TransferRepository",
    "RaidRepository",
    "StateRepository",
    "PrizeRepository",
    "OcrPort",
]
