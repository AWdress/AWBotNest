"""
core/domain/user.py
用户领域模型

依赖方向：domain 层不依赖任何外部框架
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class UserRole(str, Enum):
    """用户角色"""
    OWNER = "owner"       # 机器人拥有者
    ADMIN = "admin"       # 管理员
    MEMBER = "member"     # 普通成员
    BLOCKED = "blocked"   # 黑名单


@dataclass
class TelegramUser:
    """Telegram 用户"""
    user_id: int
    username: Optional[str] = None
    first_name: str = ""
    last_name: str = ""
    is_bot: bool = False
    role: UserRole = UserRole.MEMBER
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def display_name(self) -> str:
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name or self.username or str(self.user_id)
