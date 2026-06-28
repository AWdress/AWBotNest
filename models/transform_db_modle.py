# 标准库
import hashlib
from datetime import datetime

# 第三方库
from core import Message
from sqlalchemy import String, Integer, BigInteger, Numeric, DateTime, func, desc, select
from sqlalchemy.orm import mapped_column, Mapped

# 自定义模块
from models import async_session_maker
from models.database import Base, TimeBase


class Raiding(Base):
    __tablename__ = "raiding"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    website: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(32))
    raidcount: Mapped[int] = mapped_column(Integer)
    bonus: Mapped[float] = mapped_column(Numeric(16, 2))

    @classmethod
    async def get_latest_raiding_createtime(
        cls, website: str, action: str
    ) -> datetime | None:
        """
        查询数据库中指定网站和操作类型的最新一条记录的创建时间和 raidcount。

        参数:
            website (str): 需要查询的站点标识。
            action (str): 需要查询的操作类型。

        返回:
            tuple[datetime, Any] | None: 返回包含最新记录的创建时间和 raidcount 的元组，
            如果未找到记录则返回 None。
        """
        async with async_session_maker() as session, session.begin():
            stmt = (
                select(cls.create_time, cls.raidcount)
                .where(cls.website == website, cls.action == action)
                .order_by(desc(cls.create_time))
                .limit(1)
            )
            result_date = (await session.execute(stmt)).one_or_none()
            if result_date:
                create_time, raidcount = result_date
                return create_time, raidcount
            else:
                return None


class Transform(Base):
    __tablename__ = "transform"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    website: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int] = mapped_column(BigInteger)
    bonus: Mapped[float] = mapped_column(Numeric(16, 2))
    @classmethod
    async def add_transform_nouser(cls, user_id: int, website: str, bonus: float):
        """
        新增一条bonus记录

        参数:
            user_id (int): 用户ID
            website (str): 站点名称
            bonus (float): bonus数额
        """
        async with async_session_maker() as session, session.begin():
            transform = cls(website=website, user_id=user_id, bonus=bonus)
            session.add(transform)

    @classmethod
    async def get_latest_transform_createtime(cls, website: str, Direction: str="pay") -> datetime | None:
        """
        查询数据库中指定网站和操作类型的最新一条记录的创建时间

        参数:Direction: str
            website (str): 需要查询的站点标识。
            bonus (float): 需要查询的操作类型。

        返回:
            tuple[datetime, Any] | None: 返回包含最新记录的创建时间
            如果未找到记录则返回 None。
        """
        async with async_session_maker() as session, session.begin():
            if Direction == "pay":
                flag = Transform.bonus < 0
            else:
                flag = Transform.bonus > 0

            stmt = (
                select(cls.create_time)
                .where(
                    cls.website == website,
                    flag,
                )
                .order_by(desc(cls.create_time))
                .limit(1)
            )
            create_time = (await session.execute(stmt)).scalar_one_or_none()
            return create_time


class User(TimeBase):
    __tablename__ = "user_name"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(32))

    async def get_bonus_sum_for_website(self, site_name: str) -> float:
        """
        获取当前用户在指定站点的 bonus 总和 正负相加的结果。
        参数:
            site_name (str): 站点名称
        返回:
            float: bonus 总和
        """
        async with async_session_maker() as session, session.begin():

            stmt = select(func.sum(Transform.bonus)).where(
                Transform.user_id == self.user_id, Transform.website == site_name
            )
            bonus_sum = (await session.execute(stmt)).scalar_one_or_none()
            return bonus_sum if bonus_sum is not None else 0


    async def get_pay_bonus_count_sum_for_website(self, site_name: str, Direction: str) -> tuple[str, str]:
        """
        获取当前用户在指定站点的我发送的 bonus 总和。
        参数:
            site_name (str): 站点名称
        返回:
            tuple[str, str]: (发送次数, 发送总额字符串)
        """
        async with async_session_maker() as session, session.begin():
            if Direction == "pay":
                flag = Transform.bonus < 0
            else:
                flag = Transform.bonus > 0
            stmt = select(func.sum(Transform.bonus), func.count()).where(
                Transform.user_id == self.user_id,
                Transform.website == site_name,
                flag,
            )
            result = await session.execute(stmt)
            bonus_sum, bonus_count = result.one_or_none() or (0, 0)
            bonus_sum = bonus_sum or 0
            bonus_count = bonus_count or 0
            return f"{bonus_count:,}", f"{abs(bonus_sum):,.2f}"

    async def get_pay_bonus_leaderboard_by_website(
        self, site_name: str, Direction: str, top_n: int = 10
    ):
        """
        获取排行榜
        参数:
            site_name (str): 站点名称
            Direction (str): 排行榜方向，"pay"为发放榜，其他为接收榜
            top_n (int): 排名前N名，默认10

        返回:
            list: 排行榜数据
        """
        async with async_session_maker() as session, session.begin():
            if Direction == "pay":
                flag = Transform.bonus < 0
                sort_expr = desc(func.abs(func.sum(Transform.bonus)))
            else:
                flag = Transform.bonus > 0
                sort_expr = desc(func.sum(Transform.bonus))

            stmt = (
                select(
                    User.user_id,
                    User.name,
                    func.count().label("bonus_count"),
                    func.sum(Transform.bonus).label("bonus_sum"),
                )
                .join(User, Transform.user_id == User.user_id)
                .where(flag, Transform.website == site_name)
                .group_by(User.user_id, User.name)
                .order_by(sort_expr)
                .limit(top_n)
            )
            result = await session.execute(stmt)
            rows = result.all()
            return [
                [i + 1, user_id if user_id is not None and user_id != "None" else "未知", name if name is not None else "未知用户", f"{(count or 0):,}", f"{abs(bonus_sum or 0):,.2f}"]
                for i, (user_id, name, count, bonus_sum) in enumerate(rows)
            ]

    async def get_pay_user_bonus_rank(self, website: str, Direction: str = "get") -> int:
        """
        获取当前用户在某网站上的 bonus 总和排名（降序）。

        参数:
            website (str): 站点名称
            Direction (str): 排名方向，"pay"为发放榜，其他为接收榜

        返回:
            int: 排名（未找到返回-1）
        """
        async with async_session_maker() as session, session.begin():
            if Direction == "pay":
                flag = Transform.bonus < 0
                sort_expr = desc(func.abs(func.sum(Transform.bonus)))
            else:
                flag = Transform.bonus > 0
                sort_expr = desc(func.sum(Transform.bonus))

            stmt = (
                select(
                    Transform.user_id, func.sum(Transform.bonus).label("total_bonus")
                )
                .where(flag, Transform.website == website)
                .group_by(Transform.user_id)
                .order_by(sort_expr)
            )

            result = await session.execute(stmt)
            rows = result.all()

            # 遍历查找当前 user_id 的排名
            for rank, (uid, _) in enumerate(rows, start=1):
                if uid == self.user_id:
                    return rank
            return -1  # 没找到

    @classmethod
    async def get(cls, transform_message: Message | str | None = None):
        """
        获取或创建用户对象

        参数:
            transform_message (Message | str | None): Telegram消息对象、"me"字符串或None

        返回:
            User: 用户对象
        """
        async with async_session_maker() as session, session.begin():
            if isinstance(transform_message, Message) and transform_message.from_user:
                tg_user = transform_message.from_user

                # 改进用户名构建逻辑，确保英文用户名也能正确显示
                name_parts = []
                if tg_user.first_name and tg_user.first_name != 'None':
                    name_parts.append(tg_user.first_name)
                if tg_user.last_name and tg_user.last_name != 'None':
                    name_parts.append(tg_user.last_name)

                if name_parts:
                    username = " ".join(name_parts).strip()
                else:
                    # 如果first_name和last_name都为空，尝试使用username
                    username = getattr(tg_user, 'username', None) or "未知用户"

                user_id = tg_user.id

                # 检查用户ID是否为None
                if user_id is None:
                    user_id = generate_user_id_from_username(username)

            elif isinstance(transform_message, Message):  # 匿名或频道消息
                username = transform_message.author_signature or "匿名用户"
                user_id = generate_user_id_from_username(username)

            else:
                raise ValueError("不支持的 transform_message 类型")

            username = username[:32]
            user = await session.get(cls, user_id)

            if user:
                if user.name != username:
                    user.name = username
            else:
                user = cls(user_id=user_id, name=username)
                session.add(user)
        return user

    async def add_transform_record(self, website: str, bonus: float):
        """
        新增一条bonus记录

        参数:
            website (str): 站点名称
            bonus (float): bonus数额
        """
        async with async_session_maker() as session, session.begin():
            transform = Transform(website=website, user_id=self.user_id, bonus=bonus)
            session.add(transform)

    #########################zhuquerob表调用#######################################

    async def add_raiding_record(
        self, website: str, action: str, raidcount: int, bonus: float
    ):
        """
        向表内写入一条打劫记录
        参数:
            website (str): 站点名称
            action (str): 行为（打劫/被打劫）
            raidcount (int): 次数
            bonus (float): 金额
        """
        async with async_session_maker() as session, session.begin():
            raiding = Raiding(
                website=website,
                user_id=self.user_id,
                action=action,
                raidcount=raidcount,
                bonus=bonus,
            )
            session.add(raiding)


##################英文字母或者中文的转sha码###############################


def generate_user_id_from_username(username: str) -> int:
    # clean_name = clean_str_safe(username)
    hash_hex = hashlib.sha1(username.encode("utf-8")).hexdigest()
    return int(str(int(hash_hex, 16))[:18])  # 转为整数


##################UTF8###############################

"""
def clean_str_safe(s) -> str:
    if not isinstance(s, str):
        s = str(s)
    # 编码/解码以清除非法 surrogate
    try:
        s = s.encode('utf-16', 'surrogatepass').decode('utf-16', 'ignore')
    except Exception:
        s = s.encode('utf-8', 'ignore').decode('utf-8', 'ignore')
    # 去除控制字符和不可见字符
    s = ''.join(c for c in s if unicodedata.category(c)[0] != 'C')
    # 去除无效的 Unicode 字符（替代符号、未定义、保留等）
    s = re.sub(r'[\ud800-\udfff]', '', s)
    # 去除不可识别的特殊字符（如某些 emoji 或 Telegram 特有字符）
    s = ''.join(c for c in s if c.isprintable())
    # 限制长度 + 去除前后空格
    return s.strip()[:100]
"""
