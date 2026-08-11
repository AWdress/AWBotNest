"""
kernel/notifier.py
平台通知中心 —— 插件不直接发通知，而是「提交」给平台；
平台统一分类（按级别打标签 + 插件名 + 可选分类）、套统一格式，再通过 Bot
发给管理员（Bot 不可用时回退主账号「收藏夹」）。

为什么集中到平台：通知的「发给谁、什么格式、怎么投递」是平台策略，不该让每个
插件各自实现。插件只提供「内容 + 级别 + 分类」，其余交给平台。
"""
from __future__ import annotations

import time
import threading
import json
import os
import html
from collections import deque
from pathlib import Path
from typing import Any, Optional

from core import logger
from kernel.rich_text import (
    rich_html_to_plain,
    sanitize_rich_html,
    text_to_notification_rich_html,
)

# 级别 → 图标标签（分类的可视化）
_LEVEL_CN = {
    "info": "通知",
    "success": "成功",
    "warning": "警告",
    "error": "错误",
}
_LEVEL_ICON = {
    "info": "🔔",
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
}

# 通知中心历史保存在 data 卷中，容器更新后仍可恢复。
_HISTORY_LIMIT = 100
_HISTORY_MAX_AGE = 30 * 24 * 3600
_HISTORY_FILE = Path("data") / "webui" / "notifications.json"
_HISTORY: deque[dict] = deque(maxlen=_HISTORY_LIMIT)
_HISTORY_LOCK = threading.Lock()  # append(事件循环线程) 与 history()(Web 线程) 跨线程互斥
_HISTORY_LOADED = False


def _load_history_locked() -> None:
    global _HISTORY_LOADED
    if _HISTORY_LOADED:
        return
    _HISTORY_LOADED = True
    try:
        values = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(values, list):
        return
    cutoff = time.time() - _HISTORY_MAX_AGE
    for item in values[-_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        try:
            created_at = float(item.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if created_at >= cutoff:
            _HISTORY.append(item)


def _save_history_locked() -> None:
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _HISTORY_FILE.with_suffix(".tmp")
    payload = json.dumps(list(_HISTORY), ensure_ascii=False, separators=(",", ":"))
    temp_path.write_text(payload, encoding="utf-8")
    os.replace(temp_path, _HISTORY_FILE)


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


def _format_plain(plugin_name: str, text: str, level: str, category: Optional[str],
                  account_label: Optional[str]) -> str:
    """为普通账号和非 Telegram 渠道生成层次清晰的兼容文本。"""
    level_cn = _LEVEL_CN.get(level, level)
    icon = _LEVEL_ICON.get(level, "🔔")
    lines = [f"{icon} {plugin_name}"]
    details = [level_cn]
    if category:
        details.append(category)
    if account_label:
        details.append(f"账号：{account_label}")
    lines.extend([" · ".join(details), "────────────", text])
    return "\n".join(lines)


def _format_rich(plugin_name: str, content: str, level: str, category: Optional[str],
                 account_label: Optional[str]) -> str:
    """为 Telegram 原生 Rich Message 生成统一标题和正文。"""
    level_cn = _LEVEL_CN.get(level, level)
    icon = _LEVEL_ICON.get(level, "🔔")
    details = [f"<mark>{html.escape(level_cn)}</mark>"]
    if category:
        details.append(f"<b>{html.escape(category)}</b>")
    if account_label:
        details.append(f"<i>账号：{html.escape(account_label)}</i>")
    return (
        f"<b>{icon} {html.escape(plugin_name)}</b><br>"
        f"{' · '.join(details)}<br><br>{content}"
    )


async def _supports_native_rich(client: Any) -> bool:
    if not client or not getattr(client, "is_connected", False):
        return False
    if not callable(getattr(client, "send_rich_message", None)):
        return False
    me = getattr(client, "me", None)
    if me is None:
        try:
            me = await client.get_me()
        except Exception:  # noqa: BLE001
            return False
    return bool(getattr(me, "is_bot", False) or getattr(me, "is_premium", False))


async def _send_telegram(client: Any, target: Any, rich_body: str, plain_body: str,
                         send_kwargs: dict[str, Any]) -> Any:
    """优先发送原生富文本；普通用户或发送不兼容时自动使用美观文本。"""
    kwargs = dict(send_kwargs)
    kwargs.pop("parse_mode", None)
    if await _supports_native_rich(client):
        try:
            from pyrogram import types
            from pyrogram.errors import RPCError

            message = types.InputRichMessage(html=rich_body)
            return await client.send_rich_message(target, message, **kwargs)
        except (RPCError, TypeError, ValueError) as exc:
            logger.warning("Rich Message 发送失败，已自动改用兼容文本: %r", exc)
    return await client.send_message(target, plain_body, parse_mode=None, **kwargs)


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
    format: str = "text",
    **send_kwargs,
) -> Any:
    """
    接收一条插件通知，分类 + 统一格式 + 投递给管理员。

    accounts: AccountManager（取 bot_app / primary_user_app）
    level: info | success | warning | error（决定图标与中文标签）
    category: 可选业务分类，如「订单」「签到」
    account: 可选，触发该通知的账号（Pyrogram Client 或字符串）。多账号场景下
             用它标明「这条是哪个账号的」——插件在 handler 里把 client 传进来即可。
    返回投递结果；无可用账号时抛 RuntimeError。
    """
    level = level if level in _LEVEL_CN else "info"
    plugin_id = str(plugin_id or "")
    plugin_name = str(plugin_name or plugin_id or "系统")
    text = str(text or "")
    category = str(category).strip() if category is not None else None
    account_label = _account_label(account)
    content_format = str(format or "text").strip().lower()
    if content_format not in {"text", "rich"}:
        raise ValueError("通知格式只支持 text 或 rich")
    if content_format == "rich":
        rich_content = sanitize_rich_html(text)
        plain_content = rich_html_to_plain(rich_content)
    else:
        rich_content = text_to_notification_rich_html(text)
        plain_content = text.strip()
    if not plain_content:
        raise ValueError("通知内容不能为空")
    rich_body = _format_rich(
        plugin_name, rich_content, level, category, account_label,
    )
    plain_body = _format_plain(
        plugin_name, plain_content, level, category, account_label,
    )

    # 记入通知中心历史
    with _HISTORY_LOCK:
        _load_history_locked()
        now = time.time()
        _HISTORY.append({
            "id": str(time.time_ns()),
            "t": now,
            "plugin_id": plugin_id,
            "plugin_name": plugin_name,
            "level": level,
            "category": category,
            "account": account_label,
            "text": plain_content,
        })
        try:
            _save_history_locked()
        except OSError:
            pass
    # 同时进运行日志（前端日志页可见，带插件名 + 账号）
    acc_tag = f"[{account_label}]" if account_label else ""
    logger.info(
        "[通知][%s]%s %s%s",
        plugin_name, acc_tag, f"({category}) " if category else "", plain_content,
    )

    channel_ids = _plugin_channel_ids(plugin_id)
    delivered = False
    for channel_id in channel_ids:
        try:
            if await _send_to_channel(
                accounts, channel_id, rich_body, plain_body, send_kwargs,
            ):
                delivered = True
        except Exception as e:  # noqa: BLE001
            logger.warning("通知渠道 [%s] 发送失败: %r", channel_id or "默认", e)

    if delivered:
        return True

    # 所有渠道都不可用时保留原有兜底：发到主用户账号收藏夹。
    user = getattr(accounts, "primary_user_app", None)
    if accounts.connection_ready(user):
        return await _send_telegram(user, "me", rich_body, plain_body, send_kwargs)
    raise RuntimeError("无可用通知渠道，且没有在线用户账号可用于保底投递")


def _plugin_bot_id(plugin_id: str) -> str:
    """本插件在「系统设置 → 通知」路由到的 Bot id；未分配则回退到默认渠道。"""
    try:
        from kernel.registry import registry
        bot_id = registry.get_bot_choice(plugin_id)
        if bot_id:
            return bot_id
        # 未配置时，回退到默认渠道
        return _get_default_channel_id()
    except Exception:  # noqa: BLE001
        return ""


def _plugin_channel_ids(plugin_id: str) -> list[str]:
    """返回插件选择的渠道列表；没有单独选择时使用默认渠道或旧默认 Bot。"""
    raw = _plugin_bot_id(plugin_id)
    ids = list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    # 返回空列表表示"无配置，使用默认"；空字符串 "" 用于旧 Bot 回退逻辑
    return ids if ids else [""]


async def _send_to_channel(accounts: Any, channel_id: str, rich_body: str,
                           plain_body: str, send_kwargs: dict[str, Any]) -> bool:
    """向单个渠道发送；配置中存在但已禁用的渠道不会走旧 Bot 回退。"""
    channel_config = _get_channel_config(channel_id) if channel_id else None
    if channel_config is not None:
        if not channel_config.get("enabled"):
            return False
        channel_type = str(channel_config.get("type") or "").strip()
        config_data = channel_config.get("config") or {}
        from kernel.notification_channels import send_notification

        if channel_type == "telegram":
            bot = _get_bot(accounts, channel_id, fallback=False)
            if not accounts.connection_ready(bot):
                return False
            target = _parse_chat_id(str(config_data.get("chat_id") or ""))
            target = target or _owner_id() or None
            if not target:
                return False
            await _send_telegram(bot, target, rich_body, plain_body, send_kwargs)
            return True
        if channel_type in {"wechat", "bark"}:
            return await send_notification(
                channel_type=channel_type, config=config_data, message=plain_body,
            )
        return False

    # 没有新渠道配置时按旧 Bot 路由处理；空 id 表示当前默认 Bot。
    bot = _get_bot(accounts, channel_id, fallback=not channel_id)
    if not accounts.connection_ready(bot):
        return False
    resolved_id = _resolved_bot_id(accounts, channel_id)
    target = _bot_chat_id(resolved_id) or _owner_id() or None
    if not target:
        return False
    await _send_telegram(bot, target, rich_body, plain_body, send_kwargs)
    return True


def _get_default_channel_id() -> str:
    """获取标记为默认的通知渠道 ID；没有则返回空（使用内置默认 Bot）。"""
    try:
        import config.config as cfg
        d = cfg.load()
        channels = d.get("NOTIFICATION_CHANNELS", [])
        for ch in channels:
            if isinstance(ch, dict) and ch.get("is_default") and ch.get("enabled"):
                return ch.get("id", "")
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _get_channel_config(channel_id: str) -> Optional[dict]:
    """根据渠道ID获取通知渠道配置。"""
    try:
        import config.config as cfg
        d = cfg.load()
        channels = d.get("NOTIFICATION_CHANNELS", [])
        for ch in channels:
            if isinstance(ch, dict) and ch.get("id") == channel_id:
                return ch
        return None
    except Exception:  # noqa: BLE001
        return None


def _parse_chat_id(chat_id: str) -> Any:
    """解析Chat ID：纯数字转int，否则原样返回（如@username）。"""
    if not chat_id:
        return None
    chat_id = chat_id.strip()
    if chat_id.lstrip("-").isdigit():
        try:
            return int(chat_id)
        except ValueError:
            return chat_id
    return chat_id


def _get_bot(accounts: Any, bot_id: str, fallback: bool = True) -> Any:
    """按 id 取 Bot client；兼容极旧的无多 Bot 能力的 accounts 对象。"""
    bot_apps = getattr(accounts, "bot_apps", None)
    if bot_id and isinstance(bot_apps, dict):
        bot = bot_apps.get(bot_id)
        if bot is not None or not fallback:
            return bot
    get_bot = getattr(accounts, "get_bot", None)
    if callable(get_bot):
        return get_bot(bot_id)
    return getattr(accounts, "bot_app", None)


def _resolved_bot_id(accounts: Any, bot_id: str) -> str:
    """把空路由换成当前默认 Bot id，确保使用对应 Bot 的通知目标。"""
    resolve = getattr(accounts, "resolve_bot_id", None)
    if callable(resolve):
        return resolve(bot_id)
    return bot_id or "default"


def _bot_chat_id(bot_id: str) -> Any:
    """读取该 Bot 配置的通知目标 Chat ID；未配置返回 None。
    纯数字（含负号，群/频道）转 int 供 pyrogram 识别；否则原样（@username）。"""
    try:
        import config.config as cfg
        d = cfg.load()
    except Exception:  # noqa: BLE001
        return None
    raw = ""
    if not bot_id or bot_id == "default":
        raw = str(d.get("DEFAULT_BOT_CHAT_ID") or "").strip()
    else:
        for b in (d.get("BOTS") or []):
            if isinstance(b, dict) and b.get("id") == bot_id:
                raw = str(b.get("chat_id") or "").strip()
                break
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        try:
            return int(raw)
        except ValueError:
            return raw
    return raw


def history() -> list[dict]:
    """返回通知中心历史（最近在前）。"""
    with _HISTORY_LOCK:
        _load_history_locked()
        return list(reversed(_HISTORY))


def clear_history() -> None:
    """清空内存与持久化通知历史。"""
    with _HISTORY_LOCK:
        _load_history_locked()
        _HISTORY.clear()
        try:
            _save_history_locked()
        except OSError:
            pass
