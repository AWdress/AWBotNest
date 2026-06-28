# 标准库
import asyncio
from typing import Union, Optional

# 第三方库
from core.telegram import (
    Client as _Client,
    filters as tg_filters,
    MessageHandler,
    Message,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ForceReply,
    PeerIdInvalid,
)

# 自定义模块
from libs.log import logger

# 导入 Mixins
from libs.client_base.peers import PeerManagerMixin
from libs.client_base.session import SessionManagerMixin
from libs.client_base.interaction import InteractionMixin
from libs.client_base.invoke import InvokeMixin


class Client(_Client, PeerManagerMixin, SessionManagerMixin, InteractionMixin, InvokeMixin):
    """
    Telegram Client 封装类。
    继承多个 Mixin 以实现功能解耦，确保单个文件不超过 300 行。
    """
    def __init__(self, *args, skip_invalid_peer_replies=True, **kwargs):
        super().__init__(*args, **kwargs)

        # 初始化 Mixins 中的状态变量
        self.__init_peer_manager__()
        self.__init_interaction__()

        self._skip_invalid_peer_replies = skip_invalid_peer_replies

    async def start(self, *args, invoke_retries: int = 5, max_pool: int = 10, **kargs):
        """
        重写 start 方法：初始化调用钩子并注册交互处理器。
        """
        max_start_retries = 3
        last_error = None
        for attempt in range(1, max_start_retries + 1):
            try:
                await super().start(*args, **kargs)
                self._invoke_retries = invoke_retries
                self._pool_semaphore = asyncio.Semaphore(max_pool)

                # 劫持会话 invoke
                self._session_invoke = self.session.invoke
                self.session.invoke = self._custom_invoke

                # 统计「插件出站动作」：包裹常用发送方法，
                # 在当前 handler 归属的插件上记一次活跃（仅插件真正发消息/回复/编辑时）。
                self._install_activity_hooks()

                # 注册交互式 ask 处理器
                self.add_handler(
                    MessageHandler(self._ask_handler, tg_filters.private),
                    group=-99
                )
                return
            except Exception as e:
                last_error = e
                err_msg = str(e).lower()
                is_db_locked = "database is locked" in err_msg
                # 仅对可重试错误(数据库锁)打完整 traceback；其它(如缺凭据)由上层处理，简洁记录
                logger.error(
                    f"客户端启动失败（第 {attempt}/{max_start_retries} 次）: {e!r}",
                    exc_info=is_db_locked,
                )
                await self._cleanup_session()
                if is_db_locked and attempt < max_start_retries:
                    await asyncio.sleep(attempt)
                    continue
                raise

        if last_error:
            raise last_error

    def _install_activity_hooks(self) -> None:
        """包裹常用出站发送方法，调用时给「当前插件」记一次活跃。
        覆盖 ctx.bot.send / ctx.user.send 与 message.reply/edit（它们内部都走这些方法）。
        幂等：重复 start 不会重复包裹。"""
        if getattr(self, "_activity_hooked", False):
            return
        self._activity_hooked = True
        import functools
        try:
            from kernel import activity
        except Exception:  # noqa: BLE001 - 无内核(裸用)时跳过
            return

        methods = [
            "send_message", "send_photo", "send_document", "send_video",
            "send_animation", "send_audio", "send_voice", "send_sticker",
            "edit_message_text", "edit_message_caption", "send_media_group",
        ]
        for name in methods:
            orig = getattr(self, name, None)
            if orig is None or not callable(orig):
                continue

            def make(fn):
                @functools.wraps(fn)
                async def wrapped(*args, **kwargs):
                    try:
                        activity.record_current()
                    except Exception:  # noqa: BLE001 - 统计绝不影响发送
                        pass
                    return await fn(*args, **kwargs)
                return wrapped

            setattr(self, name, make(orig))

    async def resolve_peer(self, peer_id: Union[int, str]):
        """
        重载 resolve_peer：在解析前先检查黑名单，避免重复无效请求。
        """
        peer_id_int = None
        try:
            if isinstance(peer_id, str) and peer_id.startswith("-100"):
                peer_id_int = int(peer_id)
            elif isinstance(peer_id, int):
                peer_id_int = peer_id

            if self._skip_invalid_peer_replies and peer_id_int and self._is_peer_invalid(peer_id_int):
                raise PeerIdInvalid()
        except PeerIdInvalid:
            raise
        except Exception:
            pass

        try:
            return await super().resolve_peer(peer_id)
        except PeerIdInvalid:
            if peer_id_int and not self._is_peer_invalid(peer_id_int):
                self._add_invalid_peer(peer_id_int)
            raise

    async def get_messages(self, *args, **kwargs):
        """
        重载 get_messages：静默处理 PeerIdInvalid 错误。
        """
        try:
            return await super().get_messages(*args, **kwargs)
        except PeerIdInvalid:
            return None

    @classmethod
    def bot_command(cls, command: str, description: str = "", filters=None):
        """
        装饰器：注册 Bot 指令。
        """
        cmd_filter = tg_filters.command(command)
        if filters:
            cmd_filter = cmd_filter & filters
        return _Client.on_message(cmd_filter)
