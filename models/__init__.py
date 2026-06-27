# 标准库
from pathlib import Path
from urllib.parse import quote_plus

# 第三方库
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession as _AsyncSession,
)

# 自定义模块
from config.config import DB_INFO
from models.database import Base
from models.ai_db_model import AiMessageModel



# SQLite配置路径
db_path = Path("db_file/SQLite/tgbot.db")
db_path.parent.mkdir(parents=True, exist_ok=True)

# 根据配置选择数据库
if DB_INFO["dbset"] == "SQLite":
    DATABASE_URL = f"sqlite+aiosqlite:///{db_path}"

elif DB_INFO["dbset"] == "mySQL":
    password = quote_plus(DB_INFO["password"])
    DATABASE_URL = (
        f"mysql+aiomysql://{DB_INFO['user']}:{password}"
        f"@{DB_INFO['address']}:{DB_INFO['port']}/{DB_INFO['db_name']}"
    )

elif DB_INFO["dbset"] == "PostgreSQL":
    password = quote_plus(DB_INFO["password"])
    DATABASE_URL = (
        f"postgresql+asyncpg://{DB_INFO['user']}:{password}"
        f"@{DB_INFO['address']}:{DB_INFO['port']}/{DB_INFO['db_name']}"
    )

# SQLite 和 MySQL/PostgreSQL 的连接配置
if DB_INFO["dbset"] == "SQLite":
    # SQLite 不使用连接池
    async_engine = create_async_engine(DATABASE_URL, echo=False)


else:
    # MySQL/PostgreSQL 使用连接池
    async_engine = create_async_engine(
        DATABASE_URL,
        echo=False,  # 关闭SQL日志以提高性能
        pool_size=10,  # MySQL/PostgreSQL 使用连接池
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=3600,
    )


class AsyncSession(_AsyncSession):
    def begin(self):
        if not self.in_transaction():
            return super().begin()
        else:
            return self.begin_nested()


# 创建 sessionmaker
#
# 注意：此前使用 async_scoped_session(..., asyncio.current_task) 包装，
# 但全项目所有调用点都是 `async with async_session_maker() as session`
# （即用即建、用完即关），从未依赖 scoped_session 的「同 task 复用同一 session」特性。
# 而 scoped_session 以 task 为 key 维护内部 registry，本 bot 对每条消息/回调都
# create_task，task 结束后 registry 条目从不清除，导致 registry 与 task 对象单调累积
# （内存只增不减）。因此改用普通 async_sessionmaker，行为等价且无 registry 泄漏。
async_session_maker = async_sessionmaker(
    bind=async_engine, expire_on_commit=False, class_=AsyncSession
)


async def create_all():
    async with async_engine.begin() as conn:

        if DB_INFO["dbset"] == "SQLite":
            await conn.execute(text("PRAGMA journal_mode=WAL;"))

        await conn.run_sync(Base.metadata.create_all)
