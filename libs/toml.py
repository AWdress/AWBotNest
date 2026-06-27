# 标准库
import os
import tomllib
from typing import Dict

# 第三方库
import toml



def toml_read_state(file_path) -> dict:
    """读取 toml 文件"""
    if not os.path.exists(file_path):
        return {}
    with open(file_path, "rb") as f:
        return tomllib.load(f)


def deep_merge(dict1: dict, dict2: dict) -> dict:
    """深度合并 dict2 到 dict1，修改并返回 dict1"""
    for key, value in dict2.items():
        if (
            key in dict1
            and isinstance(dict1[key], dict)
            and isinstance(value, dict)
        ):
            deep_merge(dict1[key], value)
        else:
            dict1[key] = value
    return dict1


def toml_write_state(data: dict, file_path) -> None:
    """
    安全写入整个状态字典，保留原结构，合并后以 UTF-8 编码写入。
    """
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            original = tomllib.load(f)
    else:
        original = {}

    merged = deep_merge(original, data)

    # ✅ 强制写入为 UTF-8，防止下次读取报错
    with open(file_path, "w", encoding="utf-8") as f:
        toml.dump(merged, f)


def toml_write_section(section: str, section_data: dict, file_path) -> None:
    """
    单独写入指定 section 表头的数据，保留其它表头内容。
    """
    full_data = toml_read_state(file_path)
    full_data[section] = deep_merge(full_data.get(section, {}), section_data)

    # ✅ 强制写入为 UTF-8
    with open(file_path, "w", encoding="utf-8") as f:
        toml.dump(full_data, f)
