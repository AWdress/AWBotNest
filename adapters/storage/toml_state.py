"""
adapters/storage/toml_state.py
TOML StateManager 适配器 - 实现 core/ports/storage.py::StateRepository

包装现有 libs/state.py StateManager，提供 StateRepository 接口兼容层。
这是过渡期适配器，最终将被 SQLAlchemy 版本替换。
"""
from __future__ import annotations

from typing import Any


class TomlStateRepository:
    """
    StateRepository 的 TOML 实现

    直接包装现有 StateManager，不做额外改动，
    保证在重构过程中现有 TOML 状态文件继续正常工作。
    """

    def __init__(self, state_manager: object) -> None:
        self._sm = state_manager

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """读取配置项（代理到 StateManager.get_item）"""
        return self._sm.get_item(section, key, default)  # type: ignore[attr-defined]

    def set(self, section: str, key: str, value: Any) -> None:
        """写入配置项（代理到 StateManager.set_item）"""
        self._sm.set_item(section, key, value)  # type: ignore[attr-defined]

    def get_section(self, section: str) -> dict:
        """获取整个 section（代理到 StateManager.get_section）"""
        try:
            return self._sm.get_section(section)  # type: ignore[attr-defined]
        except AttributeError:
            # 兼容旧版 StateManager 未实现 get_section 的情况
            state = self._sm.read_state()  # type: ignore[attr-defined]
            return state.get(section, {})
