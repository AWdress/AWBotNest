from typing import List, Tuple
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import async_sessionmaker
from core.ports.storage import AiRepository
from models.ai_db_model import AiMessageModel

class SqlAlchemyAiRepository(AiRepository):
    """AI 存储的 SQLAlchemy 实现"""
    
    def __init__(self, session_maker: async_sessionmaker):
        self.session_maker = session_maker

    async def save_message(self, chat_id: int, role: str, content: str) -> None:
        async with self.session_maker() as session:
            msg = AiMessageModel(chat_id=chat_id, role=role, content=content)
            session.add(msg)
            await session.commit()

    async def get_history(self, chat_id: int, limit: int = 10) -> List[Tuple[str, str]]:
        async with self.session_maker() as session:
            # 获取最近的消息并按时间正序排列
            stmt = (
                select(AiMessageModel.role, AiMessageModel.content)
                .where(AiMessageModel.chat_id == chat_id)
                .order_by(AiMessageModel.create_time.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            # 转回 (role, content) 列表，并恢复正序
            history = [(r, c) for r, c in result.all()]
            return history[::-1]

    async def clear_history(self, chat_id: int) -> None:
        async with self.session_maker() as session:
            stmt = delete(AiMessageModel).where(AiMessageModel.chat_id == chat_id)
            await session.execute(stmt)
            await session.commit()
