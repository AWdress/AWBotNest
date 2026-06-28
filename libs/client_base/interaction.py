# 标准库
import asyncio
from typing import Union, Optional

# 第三方库
from core.telegram import Message, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply
from libs.log import logger

class InteractionMixin:
    """
    处理与用户的交互逻辑，包括等待用户回复 (ask 模式)。
    """
    def __init_interaction__(self):
        self._waiting_for_answers = {}  # {chat_id: list[Future]}

    async def ask(
        self,
        chat_id: Union[int, str],
        text: str,
        reply_markup: Union[InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply] = None,
        timeout: int = 120
    ) -> Optional[Message]:
        """
        发送消息并等待用户的下一次回复。
        """
        await self.send_message(chat_id, text, reply_markup=reply_markup)

        # 统一将 chat_id 转为 int 处理，确保匹配
        key = int(chat_id) if isinstance(chat_id, (int, str)) and str(chat_id).replace("-", "").isdigit() else chat_id

        future = asyncio.get_event_loop().create_future()
        wait_queue = self._waiting_for_answers.setdefault(key, [])
        wait_queue.append(future)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Interaction: 等待 {key} 回复超时")
            return None
        finally:
            wait_queue = self._waiting_for_answers.get(key)
            if wait_queue and future in wait_queue:
                wait_queue.remove(future)
            if wait_queue == []:
                self._waiting_for_answers.pop(key, None)

    async def _ask_handler(self, client, message: Message):
        """
        内部 Handler：拦截正在等待的 ask 回复。
        """
        # 只拦截对方回复，不拦截自己的消息（避免误吞命令）
        from_user = getattr(message, "from_user", None)
        if getattr(message, "outgoing", False) or (from_user and getattr(from_user, "is_self", False)):
            return

        chat_id = message.chat.id
        if chat_id in self._waiting_for_answers:
            wait_queue = self._waiting_for_answers[chat_id]
            future = wait_queue.pop(0) if wait_queue else None
            if future and (not future.done()):
                future.set_result(message)
                message.stop_propagation()
                return # 拦截，不传播给其他插件
            if wait_queue == []:
                self._waiting_for_answers.pop(chat_id, None)

        return
