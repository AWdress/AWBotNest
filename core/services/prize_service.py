"""
core/services/prize_service.py
发奖状态管理服务

职责：
- 管理待发奖的抽奖记录（内存状态）
- 提供 add_pending / get_pending / remove_pending / find_by_prefix 操作
- Singleton 生命周期，DI 容器管理

注意：
- pending 数据包含 Pyrogram Message 对象，不可序列化，仅保存在内存中
- 实际发奖（Telegram 发消息）逻辑保留在 handler 层
"""
from __future__ import annotations

from typing import Optional


class PrizeService:
    """
    待发奖状态管理服务

    内存中维护 pending_prizes 字典，替代 auto_prize_sender.py 中的模块级全局变量。
    """

    def __init__(self) -> None:
        # lottery_id -> prize_data dict（含 Pyrogram Message 对象）
        self._pending: dict[str, dict] = {}

    def add_pending(self, lottery_id: str, data: dict) -> None:
        """添加一个待发奖抽奖"""
        self._pending[lottery_id] = data

    def get_pending(self, lottery_id: str) -> Optional[dict]:
        """按 lottery_id 获取待发奖信息，不存在返回 None"""
        return self._pending.get(lottery_id)

    def list_all(self) -> dict[str, dict]:
        """返回所有待发奖记录的副本"""
        return dict(self._pending)

    def remove_pending(self, lottery_id: str) -> None:
        """发奖完成后移除记录"""
        self._pending.pop(lottery_id, None)

    def find_by_prefix(self, prefix: str) -> list[dict]:
        """
        按 lottery_id 前缀查找待发奖记录。
        prefix == 'all' 时返回所有记录。
        """
        if prefix.lower() == "all":
            return list(self._pending.values())
        return [v for k, v in self._pending.items() if k.startswith(prefix)]

    def count(self) -> int:
        """返回待发奖记录数"""
        return len(self._pending)
