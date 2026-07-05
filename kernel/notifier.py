"""
kernel/notifier.py
平台通知中心 —— 插件不直接发通知，而是「提交」给平台；
平台统一分类（按级别打标签 + 插件名 + 可选分类）、套统一格式，再通过 Bot
发给平台管理员（Bot 不可用时回退主账号「收藏夹」）。

为什么集中到平台：通知的「发给谁、什么格式、怎么投递」是平台策略，不该让每个
插件各自实现。插件只提供「内容 + 级别 + 分类」，其余交给平台。
"""
from __future__ import annotations

import time
import threading
from collections import deque
from typing import Any, Optional

from core import logger

# 级别 → 图标标签（分类的可视化）
_LEVEL_CN = {
    "info": "通知",
    "success": "成功",
    "warning": "警告",
    "error": "错误",
}

# 通知中心历史环形缓冲（最近 200 条），供将来 UI / 排查用
_HISTORY: deque[dict] = deque(maxlen=200)
_HISTORY_LOCK = threading.Lock()  # append(事件循环线程) 与 history()(Web 线程) 跨线程互斥


def _account_label(account: Any) -> Optional[str]:
    """
    从插件传入的 account 解析出可读账号名。
    account 可为 Pyrogram Client（取 me.first_name → session 名，与账号管理页一致）
    或直接传字符串标签。None 则不标注账号。
    """
    if account is None:
        return None
    if isinstance(account, str):
        return account.strip() or None
    me = getattr(account, "me", None)
    if me is not None:
        name = getattr(me, "first_name", None)
        if name:
            return name
    return getattr(account, "name", None) or None


def _format(plugin_name: str, text: str, level: str, category: Optional[str],
            account_label: Optional[str]) -> str:
    """统一格式：【图标 插件名 · 级别(· 分类)】(换行 账号)换行正文。"""
    level_cn = _LEVEL_CN.get(level, level)
    head = f"【{level_cn}】{plugin_name}"
    if category:
        head += f" · {category}"
    lines = [head]
    if account_label:
        lines.append(f"账号：{account_label}")
    lines.append(text)
    return "\n".join(lines)


def _owner_id() -> int:
    try:
        import config.config as _cfg
        return int(getattr(_cfg, "MY_TGID", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


async def submit(
    accounts: Any,
    plugin_id: str,
    plugin_name: str,
    text: str,
    level: str = "info",
    category: Optional[str] = None,
    account: Any = None,
    **send_kwargs,
) -> Any:
    """
    接收一条插件通知，分类 + 统一格式 + 投递给平台管理员。

    accounts: AccountManager（取 bot_app / primary_user_app）
    level: info | success | warning | error（决定图标与中文标签）
    category: 可选业务分类，如「订单」「签到」
    account: 可选，触发该通知的账号（Pyrogram Client 或字符串）。多账号场景下
             用它标明「这条是哪个账号的」——插件在 handler 里把 client 传进来即可。
    返回投递结果；无可用账号时抛 RuntimeError。
    """
    level = level if level in _LEVEL_CN else "info"
    account_label = _account_label(account)
    body = _format(plugin_name, text, level, category, account_label)

    # 记入通知中心历史
    with _HISTORY_LOCK:
        _HISTORY.append({
            "t": time.time(),
            "plugin_id": plugin_id,
            "plugin_name": plugin_name,
            "level": level,
            "category": category,
            "account": account_label,
            "text": text,
        })
    # 同时进运行日志（前端日志页可见，带插件名 + 账号）
    acc_tag = f"[{account_label}]" if account_label else ""
    logger.info("[通知][%s]%s %s%s", plugin_name, acc_tag, f"({category}) " if category else "", text)

    # 投递：优先本插件被平台分配的 Bot 私聊管理员，回退主账号收藏夹
    oid = _owner_id()
    bot = _resolve_plugin_bot(accounts, plugin_id)
    if bot and getattr(bot, "is_connected", False) and oid:
        return await bot.send_message(oid, body, **send_kwargs)
    user = getattr(accounts, "primary_user_app", None)
    if user and getattr(user, "is_connected", False):
        return await user.send_message("me", body, **send_kwargs)
    raise RuntimeError("无可用账号投递通知（Bot 未连接且无在线用户账号）")


def _resolve_plugin_bot(accounts: Any, plugin_id: str) -> Any:
    """按插件在「系统设置 → 通知」的推送路由取 Bot；未分配 / 取不到则回退默认 Bot。"""
    get_bot = getattr(accounts, "get_bot", None)
    if not callable(get_bot):
        # 兼容极旧的 accounts 对象（无多 Bot 能力）
        return getattr(accounts, "bot_app", None)
    try:
        from kernel.registry import registry
        bot_id = registry.get_bot_choice(plugin_id)
    except Exception:  # noqa: BLE001
        bot_id = ""
    return get_bot(bot_id)


def history() -> list[dict]:
    """返回通知中心历史（最近在前）。"""
    with _HISTORY_LOCK:
        return list(reversed(_HISTORY))
