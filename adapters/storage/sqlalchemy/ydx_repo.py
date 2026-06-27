"""
adapters/storage/sqlalchemy/ydx_repo.py
YDX 游戏记录 SQLAlchemy 仓库实现

委托 models.ydx_db_modle.Zhuqueydx 完成数据库操作。
"""
from __future__ import annotations

from core.domain.ydx import YdxRecord


class SqlAlchemyYdxRepository:
    """实现 YdxRepository 协议"""

    async def save(self, record: YdxRecord) -> None:
        """保存 YDX 投注记录"""
        from models.ydx_db_modle import Zhuqueydx  # noqa: PLC0415
        await Zhuqueydx.add_zhuque_ydx_result_record(
            website=record.website,
            die_point=record.die_point,
            lottery_result=record.lottery_result,
            consecutive_count=record.consecutive_count,
            bet_side=record.bet_side,
            bet_count=record.bet_count,
            bet_amount=record.bet_amount,
            win_amount=record.win_amount,
        )
