"""
adapters/storage/sqlalchemy/redpocket_repo.py
红包记录 SQLAlchemy 仓库实现

委托 models.redpocket_db_modle.Redpocket 完成数据库操作，
隔离 models 层与核心业务层。
"""
from __future__ import annotations

from models.redpocket_db_modle import Redpocket


class SqlAlchemyRedpocketRepository:
    """实现 core/ports/storage.py::RedpocketRepository 协议"""

    async def save(self, website: str, gamemode: str, bonus: float) -> None:
        """保存红包记录"""
        await Redpocket.add_redpocket_record(website, gamemode, bonus)
