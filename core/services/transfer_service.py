"""
core/services/transfer_service.py
转账业务逻辑服务

职责：
- 记录转账数据（收/发）
- 生成排行榜
- 发送转账通知
- 不依赖 Pyrogram 具体实现

依赖：
    core/ports/storage.py    -> TransferRepository, RaidRepository, StateRepository
    core/ports/messaging.py  -> NotificationPort
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from core.domain.transfer import (
    TransferDirection, TransferRecord, LeaderboardEntry, RaidRecord,
)
from core.ports.messaging import NotificationPort, MessageSender
from core.ports.storage import TransferRepository, RaidRepository, StateRepository
from core.ports.leaderboard import LeaderboardGenerator
from libs import others


_DEFAULT_LEADERBOARD_SIZE = 5


class TransferService:
    """
    转账记录与排行榜服务

    使用方式：
        service = TransferService(transfer_repo, raid_repo, state, notifier, notify_chat_id)
        await service.record(website, direction, user_id, name, amount, bonus_name, group_id, msg_id)
    """

    def __init__(
        self,
        transfer_repo: TransferRepository,
        raid_repo: RaidRepository,
        state: StateRepository,
        notifier: NotificationPort,
        notify_chat_id: int,
        leaderboard_generator: LeaderboardGenerator,
        sender: MessageSender,
    ) -> None:
        self._transfer_repo = transfer_repo
        self._raid_repo = raid_repo
        self._state = state
        self._notifier = notifier
        self._notify_chat_id = notify_chat_id
        self._leaderboard_generator = leaderboard_generator
        self._sender = sender

    # ------------------------------------------------------------------ #
    # 主入口                                                               #
    # ------------------------------------------------------------------ #

    async def record(
        self,
        website: str,
        direction: TransferDirection,
        user_id: int,
        user_name: str,
        amount: Decimal,
        bonus_name: str,
        group_id: int,
        message_id: int,
        me_name: str = "",
    ) -> TransferRecord:
        """记录一笔转账，并按配置发送通知/排行榜"""
        record = TransferRecord(
            website=website,
            direction=direction,
            user_id=user_id,
            user_name=user_name,
            amount=amount,
            bonus_name=bonus_name,
            group_id=group_id,
            message_id=message_id,
        )
        saved = await self._transfer_repo.save(record)

        section = website.upper()
        # 转入方向检查 leaderboard 开关，转出方向检查 payleaderboard 开关
        is_out = direction == TransferDirection.OUT
        lb_key = "payleaderboard" if is_out else "leaderboard"
        leaderboard_enabled = self._state.get(section, lb_key, "off") == "on"
        notification_enabled = self._state.get(section, "notification", "off") == "on"

        from libs.log import logger
        logger.debug(f"[TransferService] {website} 检查开关: leaderboard={leaderboard_enabled}, notification={notification_enabled}, section={section}")

        if notification_enabled:
            logger.debug(f"[TransferService] 准备发送组合通知: {website}")
            await self._send_combined_notification(saved, bonus_name, group_id, leaderboard_enabled)
        else:
            logger.debug(f"[TransferService] {website} notification 为 off，不发送任何消息")

        return saved

    async def record_raid(
        self,
        website: str,
        user_id: int,
        action: str,
        raidcount: int,
        bonus: Decimal,
    ) -> RaidRecord:
        """记录 Raid 活动数据"""
        record = RaidRecord(
            website=website,
            user_id=user_id,
            action=action,
            raidcount=raidcount,
            bonus=bonus,
        )
        return await self._raid_repo.save(record)

    async def record_nouser(
        self,
        user_id: int,
        website: str,
        amount: float,
    ) -> None:
        """
        记录匿名转账（无用户名、无方向信息）。
        对应旧版 Transform.add_transform_nouser()。
        用于 u2_dmhy 等站点的赠礼/奖励记录。
        """
        await self._transfer_repo.save_nouser(user_id, website, amount)

    async def get_raid_latest(
        self,
        website: str,
        action: str,
    ):
        """获取最近一次 Raid 记录的时间和次数，供冷却时间计算"""
        return await self._raid_repo.get_latest(website, action)

    async def get_leaderboard(
        self,
        website: str,
        direction: str,
        limit: int = _DEFAULT_LEADERBOARD_SIZE,
    ) -> list[LeaderboardEntry]:
        return await self._transfer_repo.get_leaderboard(website, direction, limit)

    # ------------------------------------------------------------------ #
    # 私有方法                                                             #
    # ------------------------------------------------------------------ #

    async def _send_combined_notification(
        self,
        record: TransferRecord,
        bonus_name: str,
        group_id: int,
        leaderboard_enabled: bool,
    ) -> None:
        from libs.log import logger
        try:
            direction_emoji = "📥" if record.direction == TransferDirection.IN else "📤"
            direction_label = "收到转账" if record.direction == TransferDirection.IN else "发出转账"
            
            # 获取用户的总转账金额和次数
            sum_total = await self._transfer_repo.get_user_total(record.website, record.user_id, record.direction.value)
            sum_count = await self._transfer_repo.get_user_count(record.website, record.user_id, record.direction.value)
            user_rank = await self._transfer_repo.get_user_rank(record.website, record.user_id, record.direction.value)

            # 脱敏/格式化用户ID
            uid_str = str(record.user_id)
            masked_id = uid_str
            if len(uid_str) > 4:
                masked_id = uid_str[:2] + '*' * (len(uid_str) - 4) + uid_str[-2:]
            
            from config.config import MY_NAME
            _owner = me_name or MY_NAME

            user_display = others.build_user_html_link(record.user_id, record.user_name or f'用户{masked_id}')
            amount_display = f"<b>{abs(record.amount):,} {bonus_name}</b>"
            
            if record.direction == TransferDirection.IN:
                text = (
                    f"👤 {user_display} 大佬，感谢打赏！\n"
                    f"💰 本次收到：{amount_display}\n"
                    f"<blockquote>📊 累计打赏：{sum_count} 次，共 {sum_total} {bonus_name}\n"
                    f"🏆 打赏总榜：第 {user_rank} 名</blockquote>"
                )
            else:
                text = (
                    f"👤 {user_display}\n"
                    f"🎁 这是赏赐你的 {amount_display}，拿去花！\n"
                    f"<blockquote>📊 累计赏赐：{sum_count} 次，共 {sum_total} {bonus_name}\n"
                    f"🏆 赏赐总榜：第 {user_rank} 名</blockquote>"
                )
            
            # 基础文字
            reply_text = text

            if leaderboard_enabled:
                entries = await self._transfer_repo.get_leaderboard(
                    record.website, record.direction.value, _DEFAULT_LEADERBOARD_SIZE
                )
                if entries:
                    lb_size = len(entries)
                    table_title = "打赏" if record.direction == TransferDirection.IN else "赏赐"
                    extra = f"\n<i>✨ 当前 {_owner} 个人{table_title}总榜 TOP{lb_size} 如图所示</i>"

                    for e in entries:
                        e.bonus_name = bonus_name

                    photo_path = await self._leaderboard_generator.generate(entries, record.direction.value, _owner)
                    
                    caption = text + extra

                    if photo_path:
                        logger.debug(f"[TransferService] 排行榜图片已生成: {photo_path}")
                        
                        # 发送带图片的合并通知
                        sent_id = await self._sender.send_photo(
                            group_id, 
                            photo_path, 
                            caption=caption, 
                            reply_to_message_id=record.message_id
                        )
                        
                        if sent_id > 0:
                            import asyncio
                            asyncio.create_task(self._sender.delete_message(group_id, sent_id, delay=15))
                        
                        if os.path.exists(photo_path):
                            os.unlink(photo_path)
                        return
                    else:
                        # 改进文字排行榜排版 - 更加美观整齐，标题与图片版对齐
                        table_title = "打赏" if record.direction == TransferDirection.IN else "赏赐"
                        title = f"🌟 {_owner} 的{table_title}数据终端 🌟"
                        subtitle = f">>> TOP{len(entries)} 排行榜 <<<"
                        border = "━" * 25
                        
                        lines = [
                            border,
                            title.center(22),
                            subtitle.center(22),
                            border
                        ]
                        
                        medals = ["🥇", "🥈", "🥉"]
                        for i, e in enumerate(entries):
                            medal = medals[i] if i < 3 else f"{i+1:2d}."
                            # 格式化金额：加逗号，对齐
                            amt_str = f"{float(e.total_amount):,.1f}".rjust(10)
                            # 名字截断处理，防止撑破排版
                            display_name = (e.user_name[:10] + "..") if len(e.user_name) > 10 else e.user_name.ljust(10)
                            lines.append(f"{medal} {display_name} {amt_str} {bonus_name}")
                        
                        lines.append(border)
                        lines.append(f"💡 {extra.strip()}")
                        
                        text_lb = chr(10).join(lines)
                        reply_text = f"{text}\n<blockquote>{text_lb}</blockquote>"
                        logger.info(f"[TransferService] 图片生成失败，发送精修版文字排行榜到 {group_id}")

            # 如果不需要排行榜或图片/排行榜获取失败，发纯文字
            logger.info(f"[TransferService] 发送文字通知到 {group_id}")
            sent_id = await self._sender.send_text(group_id, reply_text, reply_to_message_id=record.message_id)
            if sent_id > 0:
                import asyncio
                asyncio.create_task(self._sender.delete_message(group_id, sent_id, delay=15))
        except Exception as e:
            logger.error(f"[TransferService] 发送通知失败: {e}", exc_info=True)


