"""
config/config.py
配置加载垫片（shim）。

真正的配置数据存在 data/config.json —— 这是唯一数据源，前端通过 /api/settings
读写它。config/ 目录只放代码（本垫片），data/ 才是要做卷映射的运行时数据目录。
本模块在导入时读取 data/config.json，把各项暴露成模块级变量（API_ID /
ACCOUNTS / proxy_set / DB_INFO 等），使所有旧代码的 `import config.config as cfg`
+ `getattr(cfg, ...)` 无改动继续可用。

平台级配置全部在前端「设置」页修改，保存即写回 config.json。
（部分关键项如 API 凭据需重启平台生效。）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path("data") / "config.json"

# 平台级默认值（首次运行 / 缺字段时兜底）
_DEFAULTS: dict[str, Any] = {
    "API_ID": 0,
    "API_HASH": "",
    "BOT_TOKEN": "",
    "ACCOUNTS": [],
    "WEB_UI_URL": "",
    "WEB_UI_PORT": 18001,
    "NGROK_ENABLE": False,
    "NGROK_TOKEN": "",
    "proxy_set": {
        "proxy_enable": False,
        "proxy": {"scheme": "http", "hostname": "127.0.0.1", "port": 7890, "username": "", "password": ""},
        "PROXY_URL": "",
    },
    "DB_INFO": {
        "dbset": "SQLite", "address": "127.0.0.1", "db_name": "tgbot",
        "port": 3306, "user": "", "password": "",
    },
    # 插件仓库自动同步（定时从 GitHub 仓库拉取插件列表到「插件商店」，按需下载）
    "PLUGIN_REPO_ENABLE": False,
    "PLUGIN_REPOS": [],          # 多仓库：[{"url": "owner/repo", "token": ""}, ...]
    "PLUGIN_REPO_INTERVAL": 20,  # 轮询间隔（分钟）：刷新商店列表 + 检查已装插件更新
}

# 允许前端读写的字段（白名单，防止写入任意键）
ALLOWED_KEYS = tuple(_DEFAULTS.keys())


def load() -> dict[str, Any]:
    """读取 config.json，缺失字段用默认值补齐（顶层 dict 字段做二级合并补子键）。"""
    data: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    merged = {**_DEFAULTS, **(data or {})}
    # 二级合并：proxy_set / DB_INFO 等嵌套 dict，补齐用户配置缺失的子键，
    # 避免旧配置只有部分子键时整块覆盖掉默认值导致 KeyError。
    for k, default_v in _DEFAULTS.items():
        if isinstance(default_v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**default_v, **merged[k]}
            # proxy_set.proxy 再下一层
            if k == "proxy_set" and isinstance(default_v.get("proxy"), dict) \
                    and isinstance(merged[k].get("proxy"), dict):
                merged[k]["proxy"] = {**default_v["proxy"], **merged[k]["proxy"]}
    return merged


def save(new_values: dict[str, Any]) -> dict[str, Any]:
    """
    合并写回 config.json（只接受白名单键），并刷新本模块的模块级变量。
    原子写：先写临时文件再 os.replace，避免写到一半崩溃导致 JSON 损坏。
    返回写回后的完整配置。
    """
    import os
    import tempfile
    current = load()
    for k, v in (new_values or {}).items():
        if k in ALLOWED_KEYS:
            current[k] = v
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(current, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(_CONFIG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _CONFIG_PATH)  # 原子替换
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _apply(current)
    return current


def reload() -> None:
    """重新从磁盘加载并刷新模块级变量。"""
    _apply(load())


def _apply(cfg: dict[str, Any]) -> None:
    """把配置字典铺到模块级变量 + 派生项。"""
    g = globals()
    for k in ALLOWED_KEYS:
        g[k] = cfg.get(k, _DEFAULTS[k])

    # 从主账号派生（兼容旧代码）
    accounts = cfg.get("ACCOUNTS") or []
    first = accounts[0] if accounts else {}
    g["MY_NAME"] = first.get("name", "") if isinstance(first, dict) else ""
    g["MY_TGID"] = first.get("tgid", 0) if isinstance(first, dict) else 0
    g["NY_USERNAME"] = (first.get("session", "") if isinstance(first, dict) else first) or ""


# 导入时立即加载
_apply(load())
