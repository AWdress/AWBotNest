"""
core/services/ydx_service.py
YDX 游戏业务服务

职责：
- 提供统一的 YDX 投注记录写入入口
- 委托 YdxRepository 完成持久化
- 不依赖 SQLAlchemy / models 层

依赖：
    core/ports/storage.py  -> YdxRepository
    core/domain/ydx.py     -> YdxRecord
"""
from __future__ import annotations

from core.domain.ydx import YdxRecord


class YdxService:
    """
    YDX 游戏投注记录服务

    调用方：
        user_scripts/zhuque/ydx_zhuque.py
    """

    def __init__(self, repo) -> None:
        self._repo = repo

    async def record(
        self,
        website: str,
        die_point: int,
        lottery_result: str,
        consecutive_count: int,
        bet_side: str,
        bet_count: int,
        bet_amount: float,
        win_amount: float,
    ) -> None:
        """记录一局 YDX 游戏结果"""
        rec = YdxRecord(
            website=website,
            die_point=die_point,
            lottery_result=lottery_result,
            consecutive_count=consecutive_count,
            bet_side=bet_side,
            bet_count=bet_count,
            bet_amount=bet_amount,
            win_amount=win_amount,
        )
        await self._repo.save(rec)
