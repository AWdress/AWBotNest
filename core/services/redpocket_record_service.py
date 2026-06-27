"""
core/services/redpocket_record_service.py
红包记录业务服务

职责：
- 提供统一的红包记录写入入口
- 委托 RedpocketRepository 完成持久化
- 不依赖 SQLAlchemy / models 层

依赖：
    core/ports/storage.py  -> RedpocketRepository
"""
from __future__ import annotations


class RedpocketRecordService:
    """
    红包记录服务

    用于替代 handler 层直接调用 Redpocket.add_redpocket_record()。
    调用方：
        ptvicomo/redpocket.py
        user_scripts/zhuque/redpocket_pie_zhuque.py
    """

    def __init__(self, repo) -> None:
        self._repo = repo

    async def record(self, website: str, gamemode: str, bonus: float) -> None:
        """记录一次红包收益/支出"""
        await self._repo.save(website, gamemode, bonus)
