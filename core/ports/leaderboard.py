"""
core/ports/leaderboard.py
排行榜图片生成接口 - Protocol 定义
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable
from core.domain.transfer import LeaderboardEntry


@runtime_checkable
class LeaderboardGenerator(Protocol):
    """排行榜图片生成器接口"""

    async def generate(
        self,
        entries: list[LeaderboardEntry],
        direction: str,
        owner_name: str = "",
    ) -> str | None:
        """
        生成排行榜图片，返回图片本地路径或 None
        """
        ...
