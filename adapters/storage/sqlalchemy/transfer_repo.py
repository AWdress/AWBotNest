"""
adapters/storage/sqlalchemy/transfer_repo.py
转账记录仓库 - SQLAlchemy 2.0 async 实现

实现 core/ports/storage.py::TransferRepository
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.domain.transfer import (
    TransferDirection, TransferRecord, LeaderboardEntry, RaidRecord,
)


class SqlAlchemyTransferRepository:
    """
    TransferRepository 的 SQLAlchemy 实现

    复用现有 models/transform_db_modle.py 中的 ORM 模型，
    但通过 Repository 接口访问，解耦业务逻辑与 ORM。
    """

    def __init__(self, session_maker: async_sessionmaker) -> None:
        self._session_maker = session_maker

    async def save(self, record: TransferRecord) -> TransferRecord:
        from models.transform_db_modle import Transform, User  # noqa: PLC0415

        # 预处理：截断超长用户名，防止数据库写入失败 (User.name 限制为 String(32))
        safe_name = record.user_name or f"用户{record.user_id}"
        if len(safe_name) > 30:
            safe_name = safe_name[:27] + "..."

        async with self._session_maker() as session, session.begin():
            # 1. 保存转账记录
            row = Transform(
                website=record.website,
                user_id=record.user_id,
                bonus=float(record.amount),
            )
            session.add(row)

            # 2. 更新/保存用户名 (与 User.add_user 等效)
            user_row = await session.get(User, record.user_id)
            if user_row:
                user_row.name = safe_name
            else:
                session.add(User(user_id=record.user_id, name=safe_name))

            await session.flush()
            record.id = row.id
        return record

    async def save_nouser(self, user_id: int, website: str, amount: float) -> None:
        """保存匿名转账记录（与 Transform.add_transform_nouser 等效）"""
        from models.transform_db_modle import Transform  # noqa: PLC0415
        async with self._session_maker() as session, session.begin():
            row = Transform(
                website=website,
                user_id=user_id,
                bonus=float(amount),
            )
            session.add(row)
            await session.flush()

    async def get_leaderboard(
        self,
        website: str,
        direction: str,
        limit: int = 10,
    ) -> list[LeaderboardEntry]:
        from models.transform_db_modle import Transform, User  # noqa: PLC0415
        async with self._session_maker() as session:
            # 兼容旧逻辑：判断 bonus 正负。direction 可能是 "get" 或 "send"
            # 注意：TransferDirection.OUT 是 "send"
            if direction == "get":
                cond = Transform.bonus > 0
            else:
                cond = Transform.bonus < 0

            stmt = (
                select(
                    Transform.user_id,
                    User.name.label("user_name"),
                    func.sum(Transform.bonus).label("total"),
                    func.count(Transform.id).label("cnt"),
                )
                .join(User, Transform.user_id == User.user_id, isouter=True)
                .where(Transform.website == website, cond)
                .group_by(Transform.user_id, User.name)
                .order_by(desc(func.abs(func.sum(Transform.bonus))))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()

        from libs.log import logger
        logger.info(f"[TransferRepo] get_leaderboard 查询完成: website={website}, direction={direction}, 结果数={len(rows)}")

        entries: list[LeaderboardEntry] = []
        for i, row in enumerate(rows):
            entries.append(
                LeaderboardEntry(
                    rank=i + 1,
                    user_id=row.user_id,
                    user_name=row.user_name or str(row.user_id),
                    total_amount=Decimal(str(abs(row.total))),
                    count=row.cnt,
                    website=website,
                    bonus_name="",  # 由调用方补充
                )
            )
        return entries

    async def get_user_total(
        self,
        website: str,
        user_id: int,
        direction: str,
    ) -> Decimal:
        from models.transform_db_modle import Transform  # noqa: PLC0415
        async with self._session_maker() as session:
            if direction == "get":
                cond = Transform.bonus > 0
            else:
                cond = Transform.bonus < 0

            stmt = (
                select(func.sum(Transform.bonus))
                .where(
                    Transform.website == website,
                    Transform.user_id == user_id,
                    cond,
                )
            )
            result = (await session.execute(stmt)).scalar()
        return Decimal(str(abs(result))) if result else Decimal("0")


    async def get_user_count(
        self,
        website: str,
        user_id: int,
        direction: str,
    ) -> int:
        from models.transform_db_modle import Transform  # noqa: PLC0415
        async with self._session_maker() as session:
            if direction == "get":
                cond = Transform.bonus > 0
            else:
                cond = Transform.bonus < 0

            stmt = (
                select(func.count(Transform.id))
                .where(
                    Transform.website == website,
                    Transform.user_id == user_id,
                    cond,
                )
            )
            result = (await session.execute(stmt)).scalar()
        return result or 0

    async def get_user_rank(
        self,
        website: str,
        user_id: int,
        direction: str,
    ) -> int:
        from models.transform_db_modle import Transform  # noqa: PLC0415
        async with self._session_maker() as session:
            if direction == "get":
                cond = Transform.bonus > 0
            else:
                cond = Transform.bonus < 0

            # 按用户分组求和并排序，找出名次
            stmt = (
                select(Transform.user_id, func.sum(Transform.bonus).label("total"))
                .where(Transform.website == website, cond)
                .group_by(Transform.user_id)
                .order_by(desc(func.abs(func.sum(Transform.bonus))))
            )
            rows = (await session.execute(stmt)).all()

        for i, row in enumerate(rows):
            if row.user_id == user_id:
                return i + 1
        return 0


class SqlAlchemyRaidRepository:
    """RaidRepository 的 SQLAlchemy 实现"""

    def __init__(self, session_maker: async_sessionmaker) -> None:
        self._session_maker = session_maker

    async def save(self, record: RaidRecord) -> RaidRecord:
        from models.transform_db_modle import Raiding  # noqa: PLC0415
        async with self._session_maker() as session, session.begin():
            row = Raiding(
                website=record.website,
                user_id=record.user_id,
                action=record.action,
                raidcount=record.raidcount,
                bonus=float(record.bonus),
            )
            session.add(row)
            await session.flush()
            record.id = row.id
        return record

    async def get_latest(
        self, website: str, action: str
    ) -> Optional[tuple[datetime, int]]:
        from models.transform_db_modle import Raiding  # noqa: PLC0415
        async with self._session_maker() as session:
            stmt = (
                select(Raiding.create_time, Raiding.raidcount)
                .where(Raiding.website == website, Raiding.action == action)
                .order_by(desc(Raiding.create_time))
                .limit(1)
            )
            row = (await session.execute(stmt)).one_or_none()
        if row:
            return row.create_time, row.raidcount
        return None
