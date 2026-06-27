"""
core/ports/storage.py
存储层接口 - Protocol 定义

依赖方向：ports 只知道 domain，不知道 SQLAlchemy / SQLite / MySQL
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

from core.domain.transfer import TransferRecord, LeaderboardEntry, RaidRecord
from core.domain.lottery import PrizeRecord
from core.domain.ydx import YdxRecord


@runtime_checkable
class TransferRepository(Protocol):
    """转账记录仓库接口"""

    async def save(self, record: TransferRecord) -> TransferRecord:
        """保存转账记录，返回含 id 的记录"""
        ...

    async def save_nouser(self, user_id: int, website: str, amount: float) -> None:
        """保存匿名转账记录（无用户名/方向信息）"""
        ...

    async def get_leaderboard(
        self,
        website: str,
        direction: str,
        limit: int = 10,
    ) -> list[LeaderboardEntry]:
        """获取排行榜"""
        ...

    async def get_user_total(
        self,
        website: str,
        user_id: int,
        direction: str,
    ) -> Decimal:
        """获取用户某方向的总转账金额"""
        ...

    async def get_user_count(
        self,
        website: str,
        user_id: int,
        direction: str,
    ) -> int:
        """获取用户某方向的总转账次数"""
        ...

    async def get_user_rank(
        self,
        website: str,
        user_id: int,
        direction: str,
    ) -> int:
        """获取用户某方向的总排行榜名次"""
        ...


@runtime_checkable
class RaidRepository(Protocol):
    """Raid 记录仓库接口"""

    async def save(self, record: RaidRecord) -> RaidRecord:
        ...

    async def get_latest(
        self, website: str, action: str
    ) -> Optional[tuple[datetime, int]]:
        """获取最新一条记录的时间和次数"""
        ...


@runtime_checkable
class StateRepository(Protocol):
    """配置状态仓库接口（替代 TOML StateManager）"""

    def get(self, section: str, key: str, default: object = None) -> object:
        """读取配置项"""
        ...

    def set(self, section: str, key: str, value: object) -> None:
        """写入配置项"""
        ...

    def get_section(self, section: str) -> dict:
        """获取整个 section"""
        ...


@runtime_checkable
class PrizeRepository(Protocol):
    """中奖记录仓库接口"""
    ...

@runtime_checkable
class AiRepository(Protocol):
    """AI 对话历史仓库接口"""
    async def save_message(self, chat_id: int, role: str, content: str) -> None:
        """保存单条消息"""
        ...

    async def get_history(self, chat_id: int, limit: int = 10) -> list[tuple[str, str]]:
        """获取最近的历史记录"""
        ...

    async def clear_history(self, chat_id: int) -> None:
        """清除历史记录"""
        ...

    async def save(self, record: PrizeRecord) -> PrizeRecord:
        ...

    async def get_unsent(self, limit: int = 50) -> list[PrizeRecord]:
        """获取未发送通知的中奖记录"""
        ...

    async def mark_sent(self, record_id: int) -> None:
        ...


@runtime_checkable
class RedpocketRepository(Protocol):
    """红包记录仓库接口（替代 Redpocket.add_redpocket_record()）"""

    async def save(self, website: str, gamemode: str, bonus: float) -> None:
        """保存红包记录"""
        ...


@runtime_checkable
class YdxRepository(Protocol):
    """YDX 游戏记录仓库接口（替代 Zhuqueydx.add_zhuque_ydx_result_record()）"""

    async def save(self, record: YdxRecord) -> None:
        """保存 YDX 投注记录"""
        ...
