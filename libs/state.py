# 标准库
from pathlib import Path
from copy import deepcopy

# 自定义模块
from libs import toml


state_path = Path("data/state.toml")
state_path.parent.mkdir(parents=True, exist_ok=True)


class StateManager:
    """
    状态管理器，负责读取、写入和操作状态数据。
    """

    def __init__(self, path=state_path):
        self.path = path
        self.state = self._read_state_from_file()

    def _read_state_from_file(self) -> dict:
        """从文件读取状态"""
        return toml.toml_read_state(self.path)

    def read_state(self) -> dict:
        """
        重新从文件读取状态
        Returns:
            dict: 状态数据
        """
        self.state = self._read_state_from_file()
        return self.state

    def write_state(self) -> None:
        """
        将当前状态写入文件（安全合并写入）
        """
        toml.toml_write_state(self.state, self.path)

    def get(self, key: str, default=None):
        """
        获取状态数据中的特定键的值
        Args:
            key (str): 键名
            default: 默认值，如果键不存在则返回
        Returns:
            值或默认值
        """
        return self.state.get(key, default)

    def set(self, key: str, value) -> None:
        """
        设置状态数据中的特定键的值，并写入文件（整份写入，安全合并）
        Args:
            key (str): 键名
            value: 要设置的值
        """
        self.state[key] = value
        self.write_state()

    def get_section(self, section: str, default={}) -> dict:
        """
        获取指定表头（section）的数据
        """
        return self.state.get(section, default)

    def get_item(self, section: str, key: str, default=None):
        """
        获取指定表头下的某个键的值
        Args:
            section (str): 表头名（一级键）
            key (str): 项目名（二级键）
            default: 如果不存在则返回默认值
        Returns:
            值或默认值
        """
        return self.state.get(section, {}).get(key, default)

    def set_section(self, section: str, section_data: dict) -> None:
        """
        安全更新指定表头的数据，写入文件
        """
        current_section = deepcopy(self.state.get(section, {}))

        # 深度合并
        def deep_merge(d1, d2):
            for k, v in d2.items():
                if k in d1 and isinstance(d1[k], dict) and isinstance(v, dict):
                    deep_merge(d1[k], v)
                else:
                    d1[k] = v
            return d1

        merged_section = deep_merge(current_section, section_data)
        self.state[section] = merged_section
        toml.toml_write_section(section, section_data, self.path)
        # 重新读取保证同步
        self.read_state()

    def set_item(self, section: str, key: str, value) -> None:
        """
        设置指定表头下某个键的值，并写入文件
        Args:
            section (str): 表头名（一级键）
            key (str): 项目名（二级键）
            value: 要设置的值
        """
        self.set_section(section, {key: value})

    def toggle_item(self, section: str, key: str) -> None:
        """
        切换指定表头下的某个键的值
        Args:
            section (str): 表头名（一级键）
            key (str): 项目名（二级键）
        """
        current_value = self.get_item(section, key, "off")
        if current_value not in ["on", "off"]:
            raise ValueError(f"Invalid value for {section}.{key}: {current_value}")
        new_value = "on" if current_value == "off" else "off"
        self.set_section(section, {key: new_value})
        return new_value


# 实例化全局状态管理对象
state_manager = StateManager(state_path)
