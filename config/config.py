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
    # BOT_TOKEN 对应的内置 Bot 显示名，以及当前作为默认出口的 Bot id。
    "BOT_NAME": "主要通知渠道",
    "DEFAULT_BOT_ID": "default",
    # 默认 Bot 的通知目标 Chat ID（用户/群/频道）。留空=发给平台管理员（现有行为）。
    "DEFAULT_BOT_CHAT_ID": "",
    # 额外 Bot（多 Bot 通知推送用）。默认 Bot 仍由 BOT_TOKEN 表示（id="default"）。
    # 每项：{"id": "<唯一id>", "name": "<显示名>", "token": "<Bot Token>", "chat_id": "<可选通知目标>"}
    "BOTS": [],
    "ACCOUNTS": [],
    "WEB_UI_URL": "",
    "WEB_UI_PORT": 18001,
    # 平台级 webhook 密钥：外部服务 POST /api/v1/webhook?apikey=<此值> 即可把内容
    # 推给平台管理员（经默认 Bot，回退主账号收藏夹）。留空=关闭平台 webhook。
    "WEBHOOK_SECRET": "",
    # 通知渠道配置（支持多个通知渠道）
    # 每项：{"id": "<唯一id>", "name": "<名称>", "type": "telegram|wechat|bark", "enabled": bool, "config": {...}}
    "NOTIFICATION_CHANNELS": [],
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
    "PLUGIN_REPOS": [],          # 公开仓库列表：[{"url": "AWdress/AWBotNest-Plugins"}, ...]
    "PLUGIN_REPO_INTERVAL": 20,  # 轮询间隔（分钟）：刷新商店列表 + 检查已装插件更新
    # 插件依赖安装用的 pip 镜像源。默认清华源（境内直连、不经墙，开箱可用）；
    # 留空则走官方 pypi（此时若配了平台代理会自动用代理出墙）。
    "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
}

# 允许前端读写的字段（白名单，防止写入任意键）
ALLOWED_KEYS = tuple(_DEFAULTS.keys())


def normalize_default_bot_id(value: Any, bots: Any) -> str:
    """只允许内置 Bot 或现有额外 Bot 成为默认项。"""
    selected = str(value or "default").strip()
    valid_ids = {"default"}
    for bot in bots or []:
        if isinstance(bot, dict):
            bot_id = str(bot.get("id") or "").strip()
            if bot_id and bot_id != "default":
                valid_ids.add(bot_id)
    return selected if selected in valid_ids else "default"


def normalize_plugin_repo(value: Any) -> str:
    """把 GitHub 链接或简写统一成 owner/repo。"""
    import re

    source = str(value or "").strip()
    source = re.sub(r"^https?://(?:www\.)?github\.com/", "", source, flags=re.IGNORECASE)
    source = source.split("#", 1)[0].split("?", 1)[0].strip("/")
    parts = source.split("/")
    if len(parts) < 2:
        return ""
    owner = parts[0].strip()
    repo = re.sub(r"\.git$", "", parts[1].strip(), flags=re.IGNORECASE)
    valid_part = re.compile(r"^[A-Za-z0-9_.-]+$")
    if (not valid_part.fullmatch(owner)
            or repo.casefold() != "awbotnest-plugins"):
        return ""
    return f"{owner}/AWBotNest-Plugins"


def _clean_plugin_repos(value: Any) -> list[dict[str, str]]:
    """只保留公开仓库地址，统一格式、去重并删除旧版私有仓库凭据。"""
    cleaned = []
    seen = set()
    for repo in value or []:
        if not isinstance(repo, dict):
            continue
        url = normalize_plugin_repo(repo.get("url"))
        key = url.casefold()
        if not url or key in seen:
            continue
        seen.add(key)
        cleaned.append({"url": url})
    return cleaned


def _write_config(values: dict[str, Any]) -> None:
    """原子写入配置文件。"""
    import os
    import tempfile
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(values, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(_CONFIG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict[str, Any]:
    """读取 config.json，缺失字段用默认值补齐（顶层 dict 字段做二级合并补子键）。"""
    data: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    cleaned_repos = _clean_plugin_repos(data.get("PLUGIN_REPOS"))
    if "PLUGIN_REPOS" in data and data.get("PLUGIN_REPOS") != cleaned_repos:
        data["PLUGIN_REPOS"] = cleaned_repos
        try:
            _write_config(data)
        except OSError:
            pass  # 配置只读时仍使用清理后的内存值，不让旧 token 进入后端。
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
    # 私有仓库已不再支持，只保留公开仓库地址并丢弃旧配置里的 token。
    merged["PLUGIN_REPOS"] = _clean_plugin_repos(merged.get("PLUGIN_REPOS"))
    merged["BOT_NAME"] = str(merged.get("BOT_NAME") or "").strip() or "主要通知渠道"
    merged["DEFAULT_BOT_ID"] = normalize_default_bot_id(
        merged.get("DEFAULT_BOT_ID"), merged.get("BOTS")
    )
    return merged


def save(new_values: dict[str, Any]) -> dict[str, Any]:
    """
    合并写回 config.json（只接受白名单键），并刷新本模块的模块级变量。
    原子写：先写临时文件再 os.replace，避免写到一半崩溃导致 JSON 损坏。
    返回写回后的完整配置。
    """
    current = load()
    for k, v in (new_values or {}).items():
        if k in ALLOWED_KEYS:
            current[k] = v
    current["BOT_NAME"] = str(current.get("BOT_NAME") or "").strip() or "主要通知渠道"
    current["DEFAULT_BOT_ID"] = normalize_default_bot_id(
        current.get("DEFAULT_BOT_ID"), current.get("BOTS")
    )
    _write_config(current)
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
