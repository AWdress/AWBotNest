"""
libs/transfer_helper.py
转账迁移辅助函数

提供统一的 do_transfer() 入口：
- 通过 TransferService.record()（新架构）记录转账

钩子系统（替代猴子补丁）：
- register_post_record_hook(fn): 注册转账完成后的回调
- 回调签名: async fn(transform_message, bonus, site_name, bonus_name, direction)
"""
from __future__ import annotations

from decimal import Decimal
from time import monotonic
from typing import Callable, Awaitable, Any

# 全局钩子列表（如 bomb game 注册在这里，替代猴子补丁）
_post_record_hooks: list[Callable[..., Awaitable[Any]]] = []
_recent_transfer_keys: dict[tuple[str, str, int, int, str], float] = {}
_TRANSFER_DEDUPE_TTL_SECONDS = 8.0


def register_post_record_hook(fn: Callable[..., Awaitable[Any]]) -> None:
    """注册转账完成后的钩子。回调签名: async fn(transform_message, bonus, site_name, bonus_name, direction)"""
    if fn not in _post_record_hooks:
        _post_record_hooks.append(fn)


async def _call_hooks(transform_message, bonus, site_name, bonus_name, direction) -> None:
    from libs.log import logger
    for hook in _post_record_hooks:
        try:
            await hook(transform_message, bonus, site_name, bonus_name, direction)
        except Exception as e:
            logger.warning(f"[transfer_helper] post-record hook 执行失败: {e}")


def _cleanup_recent_transfer_keys(now: float) -> None:
    expired_keys = [
        key for key, timestamp in _recent_transfer_keys.items()
        if now - timestamp > _TRANSFER_DEDUPE_TTL_SECONDS
    ]
    for key in expired_keys:
        _recent_transfer_keys.pop(key, None)


def _build_transfer_dedupe_key(
    transform_message,
    bonus: Decimal,
    site_name: str,
    direction: str,
) -> tuple[str, str, int, int, str]:
    chat_id = transform_message.chat.id if transform_message else 0
    message_id = transform_message.id if transform_message else 0
    return (site_name, direction, chat_id, message_id, str(bonus))


async def do_transfer(
    transform_message,
    bonus: Decimal,
    site_name: str,
    bonus_name: str,
    direction: str,   # "get" | "pay"
    leaderboard: str = "off",
    payleaderboard: str = "off",
    notification: str = "off",
) -> None:
    """
    统一转账记录入口。

    使用新架构 TransferService.record() 记录转账。
    完成后触发所有注册的 post-record 钩子（如炸弹游戏参与确认）。
    """
    from libs.log import logger
    from infra.container import get_container
    from core.domain.transfer import TransferDirection

    now = monotonic()
    _cleanup_recent_transfer_keys(now)
    dedupe_key = _build_transfer_dedupe_key(transform_message, bonus, site_name, direction)
    last_seen = _recent_transfer_keys.get(dedupe_key)
    if last_seen is not None and now - last_seen <= _TRANSFER_DEDUPE_TTL_SECONDS:
        logger.info(
            f"[{site_name}] 跳过重复转账记录: "
            f"dir={direction}, chat={dedupe_key[2]}, msg={dedupe_key[3]}, amount={bonus}"
        )
        return
    _recent_transfer_keys[dedupe_key] = now

    container = get_container()
    svc = container.transfer_service()

    fu = transform_message.from_user if transform_message else None
    user_id = fu.id if fu else 0
    parts: list[str] = []
    if fu and fu.first_name:
        parts.append(fu.first_name)
    if fu and fu.last_name:
        parts.append(fu.last_name)
    user_name = (
        " ".join(parts).strip()
        if parts
        else (fu.username if fu and fu.username else str(user_id))
    )
    
    # 额外保护：防止显示 Untitled 或空名字
    if not user_name or user_name.lower() in ["untitled", "none", "null"]:
        if fu and fu.username:
            user_name = f"@{fu.username}"
        else:
            user_name = f"用户{user_id}"

    dir_enum = TransferDirection.IN if direction == "get" else TransferDirection.OUT
    await svc.record(
        website=site_name,
        direction=dir_enum,
        user_id=user_id,
        user_name=user_name,
        amount=bonus,
        bonus_name=bonus_name,
        group_id=transform_message.chat.id if transform_message else 0,
        message_id=transform_message.id if transform_message else 0,
    )
    logger.info(
        f"[{site_name}] TransferService.record 成功: "
        f"user={user_name}, amount={bonus}, dir={direction}"
    )
    await _call_hooks(transform_message, bonus, site_name, bonus_name, direction)
