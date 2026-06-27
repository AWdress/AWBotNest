# 标准库
from datetime import datetime, timedelta

# 第三方库
from sqlalchemy import String, Integer, BigInteger, Numeric, DateTime, func, desc, select
from sqlalchemy.orm import mapped_column, Mapped

# 自定义模块
from libs import others
from models.database import Base
from models import async_session_maker



class Redpocket(Base):
    """
    红包等
    """

    __tablename__ = "redpocket"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    website: Mapped[str] = mapped_column(String(32))
    gamemode: Mapped[str] = mapped_column(String(32))
    bonus: Mapped[float] = mapped_column(Numeric(16, 2))

    @classmethod
    async def add_redpocket_record(cls, website: str, gamemode: str, bonus: float):
        """
        红包数据表写入数据

        参数:
            website (str): 站点名称
            gamemode (str): 游戏模式
            bonus (float): 红包金额

        返回:
            None
        """
        async with async_session_maker() as session, session.begin():
            redpocket = cls(website=website, gamemode=gamemode, bonus=bonus)
            session.add(redpocket)
            await session.flush()

    @classmethod
    async def get_today_latest_fire_createtime(
        cls, website: str, gamemode: str
    ) -> datetime | None:
        """
        查询数据库指定站点和模式下最新一笔红包的创建时间

        参数:
            website (str): 站点名称
            gamemode (str): 游戏模式

        返回:
            datetime | None: 最新红包的创建时间，若无则为 None
        """
        async with async_session_maker() as session, session.begin():
            stmt = (
                select(cls.create_time)
                .where(cls.website == website, cls.gamemode == gamemode)
                .order_by(desc(cls.create_time))
                .limit(1)
            )
            result_date = (await session.execute(stmt)).scalar_one_or_none()
            return result_date

    @classmethod
    async def get_bonus_redpocket_for_website(
        cls,
        website: str,
        gamemode: str,
    ) -> float:
        """
        获取当前指定站点、指定模式下红包 bonus 的总和

        参数:
            website (str): 站点名称
            gamemode (str): 游戏模式

        返回:
            float: bonus 的总和，如果不存在则返回 0
        """
        async with async_session_maker() as session, session.begin():
            stmt = select(func.sum(cls.bonus)).where(
                cls.website == website, cls.gamemode == gamemode
            )
            bonus_sum = (await session.execute(stmt)).scalar_one_or_none()
            return bonus_sum if bonus_sum is not None else 0

    @classmethod
    async def get_bonus_count_sum_redpocket_for_website(
        cls,
        website: str,
        gamemode: str,
        status: str,
        start_date=None,
        end_date=None,
    ) -> tuple[int, float]:
        """
        获取指定站点与模式下，指定时间范围内红包的总次数与奖金。

        参数:
            website (str): 站点名称
            gamemode (str): 游戏模式
            status (str): "pay" 查询发放榜（bonus<0），"get" 查询接收榜（bonus>0）
            start_date (str|datetime|None): 起始日期（可选）
            end_date (str|datetime|None): 结束日期（可选）

        返回:
            tuple[int, float]: (红包次数, 红包奖金总和)
        """
        async with async_session_maker() as session, session.begin():
            conditions = [
                cls.website == website,
                cls.gamemode == gamemode,
            ]
            if status == "pay":
                conditions.append(Redpocket.bonus < 0)
            elif status == "get":
                conditions.append(Redpocket.bonus > 0)

            # 时间过滤逻辑
            if start_date or end_date:
                start = others.parse_date_input(start_date or end_date)
                end = others.parse_date_input(end_date or start_date)
                # 若为同一天，则 end 向后推一天以覆盖整天
                if start.date() == end.date():
                    end += timedelta(days=1)
                conditions.extend(
                    [Redpocket.create_time >= start, Redpocket.create_time < end]
                )
            bonus_sum_stmt = select(func.sum(Redpocket.bonus)).where(conditions)
            bonus_count_stmt = select(func.count()).where(conditions)
            # 执行查询
            bonus_sum = (
                await session.execute(bonus_sum_stmt)
            ).scalar_one_or_none() or 0
            bonus_count = (
                await session.execute(bonus_count_stmt)
            ).scalar_one_or_none() or 0
            return bonus_count, float(bonus_sum)
