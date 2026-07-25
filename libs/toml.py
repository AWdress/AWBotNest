# 标准库
import os
import tempfile
import tomllib
from typing import Dict

# 第三方库
import toml



def toml_read_state(file_path) -> dict:
    """读取 toml 文件；文件损坏（半截/非法）时返回空 dict，避免一处坏文件拖垮后续全部读写。"""
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError, ValueError):
        return {}


def _atomic_write_toml(data: dict, file_path) -> None:
    """原子写入 TOML：先写同目录临时文件再 os.replace 覆盖。
    避免 open(path,'w') 立即截断后写一半崩溃，留下半截文件导致后续读取全部崩溃。"""
    file_path = os.fspath(file_path)
    directory = os.path.dirname(file_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".state_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            toml.dump(data, f)
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
    安全写入整个状态字典，保留原结构，合并后原子写入（UTF-8）。
    """
    original = toml_read_state(file_path)
    merged = deep_merge(original, data)
    _atomic_write_toml(merged, file_path)


def toml_write_section(section: str, section_data: dict, file_path) -> None:
    """
    单独写入指定 section 表头的数据，保留其它表头内容（原子写入）。
    """
    full_data = toml_read_state(file_path)
    full_data[section] = deep_merge(full_data.get(section, {}), section_data)
    _atomic_write_toml(full_data, file_path)
