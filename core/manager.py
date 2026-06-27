import os
import asyncio
from pathlib import Path
from typing import Optional

from libs.log import logger
from libs.custom_client import Client
from infra.config import get_settings

config = get_settings()

class Manager:
    def __init__(self):
        self.user_apps: list[Client] = []   # 所有用户账号实例列表
        self.bot_app: Optional[Client] = None
        self.prefix = "." # 默认前缀
        self.owner_id = config.telegram.my_tgid
        self.workdir = Path("sessions")
        self.workdir.mkdir(parents=True, exist_ok=True)

        # 代理配置从 config 中获取
        if config.proxy.proxy_enable:
            self.proxy = {
                "scheme": config.proxy.scheme,
                "hostname": config.proxy.hostname,
                "port": config.proxy.port,
                "username": config.proxy.username,
                "password": config.proxy.password.get_secret_value()
            }
        else:
            self.proxy = None

    @property
    def user_app(self) -> Optional[Client]:
        """兼容旧代码：返回主账号（第一个）"""
        return self.user_apps[0] if self.user_apps else None

    @user_app.setter
    def user_app(self, val: Optional[Client]) -> None:
        """兼容旧代码：设置主账号"""
        if val is None:
            self.user_apps = []
        elif not self.user_apps:
            self.user_apps = [val]
        else:
            self.user_apps[0] = val

    async def start_userbot(
        self,
        session_string: Optional[str] = None,
        session_name: str = "",
    ) -> bool:
        # 默认启动主账号（ACCOUNTS[0]["session"]）
        if not session_name:
            try:
                from config.config import ACCOUNTS
                session_name = ACCOUNTS[0]["session"] if isinstance(ACCOUNTS[0], dict) else ACCOUNTS[0]
            except Exception:
                session_name = "user_account"
        """
        启动指定账号的 userbot。
        session_name: 对应 ACCOUNTS 列表中的账号标识。
        session_string: 若提供则使用内存 session，否则使用 SQLite 持久化。
        """
        try:
            # 找到并停止已运行的同名实例
            target = next((a for a in self.user_apps if a.name == session_name), None)
            if target and target.is_connected:
                await target.stop()

            new_client = Client(
                session_name,
                api_id=config.telegram.api_id,
                api_hash=config.telegram.api_hash.get_secret_value(),
                session_string=session_string,
                workdir=str(self.workdir.resolve()),
                proxy=self.proxy,
                plugins=dict(root="plugins/user"),
            )

            if target is not None:
                self.user_apps[self.user_apps.index(target)] = new_client
            else:
                self.user_apps.append(new_client)

            await new_client.start()

            # 主账号重绑 DI 容器
            if self.user_apps and self.user_apps[0].name == session_name:
                try:
                    from infra.container import rebind_user_client
                    rebind_user_client(new_client)
                except Exception:
                    logger.warning("更新 DI 容器 user_client 失败（容器可能尚未初始化）")

            return True
        except Exception:
            logger.exception("启动 Userbot [%s] 失败", session_name)
            return False

    async def get_bot(self) -> Client:
        if not self.bot_app:
            self.bot_app = Client(
                "bot_account",
                api_id=config.telegram.api_id,
                api_hash=config.telegram.api_hash.get_secret_value(),
                bot_token=config.telegram.bot_token.get_secret_value(),
                workdir=str(self.workdir.resolve()),
                proxy=self.proxy,
                plugins=dict(root="plugins/bot"),
            )
        return self.bot_app

manager = Manager()
