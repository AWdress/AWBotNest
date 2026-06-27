"""
core/services/__init__.py
业务服务包
"""
from core.services.trap_service import TrapService, TrapDetectionConfig, TrapCheckResult
from core.services.red_packet_service import RedPacketService
from core.services.lottery_service import LotteryService, LotteryConfig, extract_lottery_id
from core.services.transfer_service import TransferService
from core.services.prize_service import PrizeService
from core.services.ydx_service import YdxService
from core.services.redpocket_record_service import RedpocketRecordService
from core.services.ai_service import AiService

__all__ = [
    "TrapService", "TrapDetectionConfig", "TrapCheckResult",
    "RedPacketService",
    "LotteryService", "LotteryConfig", "extract_lottery_id",
    "TransferService",
    "AiService",
]
