"""
core/ports/storage.py
存储层接口 - Protocol 定义

依赖方向：ports 只知道 domain，不知道 SQLAlchemy / SQLite / MySQL
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StateRepository(Protocol):
    """配置状态仓库接口（替代 TOML StateManager）"""

    def get(self, section: str, key: str, default: object = None) -> object:
        """读取配置项"""
        ...

    def set(self, section: str, key: str, value: object) -> None:
        """写入配置项"""
        ...

    def get_section(self, section: str) -> dict:
        """获取整个 section"""
        ...
