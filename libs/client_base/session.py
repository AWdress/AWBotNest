# 标准库
import asyncio
from libs.log import logger

class SessionManagerMixin:
    """
    处理 Telegram 会话的生命周期管理，包括数据库清理、强制重启等逻辑。
    """
    async def _cleanup_session(self):
        """清理损坏的会话文件"""
        try:
            if hasattr(self, 'session') and hasattr(self.session, 'storage'):
                if hasattr(self.session.storage, 'conn') and self.session.storage.conn:
                    try:
                        self.session.storage.conn.close()
                    except Exception as e:
                        logger.warning(f"关闭存储连接时出错: {e}")

                if hasattr(self.session.storage, 'init'):
                    try:
                        await self.session.storage.init()
                    except Exception as e:
                        logger.warning(f"重新初始化存储时出错: {e}")
        except Exception as e:
            logger.warning(f"清理会话文件时出错: {e}")

    async def _force_cleanup_session(self):
        """强制清理会话文件，用于处理严重的数据库连接问题"""
        try:
            if hasattr(self, 'is_connected') and self.is_connected:
                try:
                    await self.stop()
                except Exception:
                    pass

            if hasattr(self, 'session') and hasattr(self.session, 'storage'):
                try:
                    if hasattr(self.session.storage, 'conn') and self.session.storage.conn:
                        self.session.storage.conn.close()
                    from core.telegram import SQLiteStorage
                    self.session.storage = SQLiteStorage(self.session.name, self.session.workdir)
                except Exception:
                    pass
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"强制清理会话文件失败: {e}")
            raise

    async def restart_session(self):
        """重启会话，处理数据库连接问题"""
        try:
            if hasattr(self, 'is_connected') and self.is_connected:
                await self.stop()
            await self._cleanup_session()
            await asyncio.sleep(1)
            await self.start()
        except Exception as e:
            logger.error(f"会话重启失败: {e}")
            try:
                await self._force_cleanup_session()
                await self.start()
            except Exception as force_cleanup_error:
                logger.error(f"强制清理后重启仍失败: {force_cleanup_error}")
                raise
