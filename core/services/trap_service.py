"""
core/services/trap_service.py
陷阱检测服务 - 从 auto_lottery_for_xiaocai.py 迁移

依赖：core/ports/storage.py (StateRepository)
不依赖任何 Telegram / 数据库具体实现
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from core.ports.storage import StateRepository


@dataclass
class TrapDetectionConfig:
    """陷阱检测配置（从 config.py TRAP_LOTTERY_DETECTION 读入）"""
    case_sensitive: bool = False
    enable_prize_pattern_check: bool = True
    enable_creator_blacklist: bool = True
    enable_participant_check: bool = True
    max_participants: int = 1
    blacklist_creator_ids: list[int] = field(default_factory=list)
    suspicious_keywords: list[str] = field(default_factory=list)
    min_prize_amount: int = 500
    enable_prize_amount_check: bool = True


@dataclass
class TrapCheckResult:
    """陷阱检测结果"""
    is_trap: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.is_trap


class TrapService:
    """
    陷阱检测服务

    职责：
    - 关键词检测（奖品名、消息文字、参与关键词、简介）
    - 创建者黑名单检测
    - 参与人数限制检测
    - 奖品金额阈值检测

    全部由 StateRepository 读取运行时配置，可在 Bot 菜单中动态调整。
    """

    def __init__(
        self,
        config: TrapDetectionConfig,
        state: StateRepository,
    ) -> None:
        self._config = config
        self._state = state

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def check(self, message_text: str, lottery_info: Optional[dict] = None) -> TrapCheckResult:
        """
        检测是否为陷阱抽奖

        Args:
            message_text: 抽奖消息全文
            lottery_info: 解析后的抽奖信息字典（可选），含：
                - prize / all_prizes
                - boss_ID
                - keyword（参与关键词）
                - description（简介）
                - max_participants

        Returns:
            TrapCheckResult
        """
        reasons: list[str] = []

        # 1. 奖品金额检测（独立开关，不受总开关影响）
        if lottery_info and self._config.enable_prize_amount_check:
            self._check_prize_amount(lottery_info, reasons)

        # 2. 奖品名称 / 参与关键词 / 简介关键词检测（独立开关）
        if lottery_info and self._config.enable_prize_pattern_check:
            self._check_prize_keywords(lottery_info, reasons)

        # 总开关检查
        trap_total_enabled = (
            self._state.get("TRAP_DETECTION", "trap_detection_enabled", "on") == "on"
        )

        if not trap_total_enabled:
            return TrapCheckResult(is_trap=bool(reasons), reasons=reasons)

        # 3. 全文关键词检测
        self._check_message_keywords(message_text, reasons)

        if lottery_info:
            # 4. 创建者黑名单
            if self._config.enable_creator_blacklist:
                self._check_creator_blacklist(lottery_info, reasons)

            # 5. 参与人数限制
            if self._config.enable_participant_check:
                self._check_participants(message_text, lottery_info, reasons)

        return TrapCheckResult(is_trap=bool(reasons), reasons=reasons)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _get_keywords(self) -> list[str]:
        """读取关键词列表（优先 state 自定义，否则 config 默认）"""
        saved = str(self._state.get("TRAP_DETECTION", "custom_keywords", "") or "")
        if saved.strip():
            return [k.strip() for k in saved.split(",") if k.strip()]
        return self._config.suspicious_keywords

    def _keyword_match(self, text: str, keywords: list[str]) -> Optional[str]:
        """返回第一个匹配的关键词，没有则 None"""
        if self._config.case_sensitive:
            for kw in keywords:
                if kw in text:
                    return kw
        else:
            text_lower = text.lower()
            for kw in keywords:
                if kw.lower() in text_lower:
                    return kw
        return None

    def _check_prize_amount(self, lottery_info: dict, reasons: list[str]) -> None:
        enabled = self._state.get("TRAP_DETECTION", "enable_prize_amount_check", "on") == "on"
        if not enabled:
            return
        try:
            min_amount = int(
                self._state.get(
                    "TRAP_DETECTION", "min_prize_amount",
                    str(self._config.min_prize_amount),
                )
            )
        except (ValueError, TypeError):
            min_amount = self._config.min_prize_amount

        all_prizes: list[str] = lottery_info.get("all_prizes") or []
        if not all_prizes:
            single = lottery_info.get("prize", "")
            all_prizes = [single] if single else []

        amounts = [self._parse_prize_amount(p) for p in all_prizes]
        amounts = [a for a in amounts if a is not None]
        if amounts and all(a < min_amount for a in amounts):
            reasons.append(
                f"奖品金额过低: 最高 {max(amounts)} (最小阈值: {min_amount})"
            )

    def _check_prize_keywords(self, lottery_info: dict, reasons: list[str]) -> None:
        enabled = self._state.get("TRAP_DETECTION", "enable_prize_pattern_check", "on") == "on"
        if not enabled:
            return
        keywords = self._get_keywords()
        if not keywords:
            return

        for field_name, label in [
            ("prize", "奖品名称"),
            ("keyword", "参与关键词"),
            ("description", "简介"),
        ]:
            text = lottery_info.get(field_name, "") or ""
            if text:
                matched = self._keyword_match(text, keywords)
                if matched:
                    reasons.append(f"{label}可疑: 「{text}」含关键词「{matched}」")

    def _check_message_keywords(self, message_text: str, reasons: list[str]) -> None:
        keywords = self._get_keywords()
        if not keywords:
            return
        matched = self._keyword_match(message_text, keywords)
        if matched:
            reasons.append(f"消息关键词匹配: 「{matched}」")

    def _check_creator_blacklist(self, lottery_info: dict, reasons: list[str]) -> None:
        boss_id = str(lottery_info.get("boss_ID", ""))
        blacklist = [str(bid) for bid in self._config.blacklist_creator_ids]
        if boss_id and boss_id in blacklist:
            reasons.append(f"创建者在黑名单中: {boss_id}")

    def _check_participants(
        self, message_text: str, lottery_info: dict, reasons: list[str]
    ) -> None:
        try:
            config_max = int(
                self._state.get(
                    "TRAP_DETECTION", "max_participants",
                    str(self._config.max_participants),
                )
            )
        except (ValueError, TypeError):
            config_max = self._config.max_participants

        max_p = lottery_info.get("max_participants")
        if max_p is None:
            m = re.search(r"中奖概率[：:]\s*\d+/(\d+)", message_text)
            if m:
                max_p = int(m.group(1))

        if max_p is not None and max_p <= config_max:
            reasons.append(f"参与人数限制过低: {max_p} (阈值: {config_max})")

    @staticmethod
    def _parse_prize_amount(prize: str) -> Optional[int]:
        '''从奖品字符串提取金额（仅解析 * 前文本，避免把份数当金额）。'''
        if not prize:
            return None

        before_star = prize.split("*")[0].strip()
        if not before_star:
            return None

        multipliers = {"万": 10000, "千": 1000, "百": 100, "十": 10, "w": 10000, "k": 1000, "m": 1000000}

        # 1) 纯数字
        if before_star.isdigit():
            return int(before_star)

        # 3) 开头数字 + 倍率字符（如 2万电力 / 50w魔力）
        m = re.match(r"^(\d+)([万千百十wWkKmM])", before_star)
        if m:
            amount = int(m.group(1))
            unit = m.group(2).lower()
            if unit in multipliers:
                amount *= multipliers[unit]
            return amount

        # 2) 开头数字 + 空格/中文/字母（如 500 魔力 / 100jasmine）
        m = re.match(r"^(\d+)(?=\s|[\u4e00-\u9fffA-Za-z_]|$)", before_star)
        if m:
            return int(m.group(1))

        # 4) 中文/字母前缀后数字+倍率（如 茉莉2万 / 茉莉50w）
        m = re.search(r"[\u4e00-\u9fffA-Za-z_](\d+)([万千百十wWkKmM])", before_star)
        if m:
            amount = int(m.group(1))
            unit = m.group(2).lower()
            if unit in multipliers:
                amount *= multipliers[unit]
            return amount

        # 5) 字符+数字（结尾）(如 "茉莉12349")
        m = re.search(r"[\u4e00-\u9fffA-Za-z_](\d+)$", before_star)
        if m:
            return int(m.group(1))

        return None
