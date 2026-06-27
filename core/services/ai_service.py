from typing import Dict, List, Optional
from core.domain.ai import AiConfig, AiConversation, AiMessage
from core.ports.ai import AiEnginePort
from core.ports.storage import StateRepository, AiRepository
import logging

logger = logging.getLogger(__name__)

class AiService:
    """AI 业务逻辑服务"""
    
    def __init__(self, engine: AiEnginePort, state_repo: StateRepository, ai_repo: AiRepository):
        self.engine = engine
        self.state_repo = state_repo
        self.ai_repo = ai_repo
        self._sessions: Dict[int, AiConversation] = {}
        
    def _get_config(self) -> AiConfig:
        """从状态仓库读取配置"""
        data = self.state_repo.get_section("ai")
        if not data:
            return AiConfig()
        return AiConfig(**data)

    @staticmethod
    def _classify_engine_error(err: Exception) -> dict:
        '''对上游/SDK 异常做轻量分类，避免把异常文本当作正常 AI 回复返回。'''
        msg = str(err) or err.__class__.__name__
        lower = msg.lower()
        # 503 / 服务不可用
        is_503 = ("503" in lower) or ("service unavailable" in lower)
        # 401/403 / 鉴权
        is_auth = ("401" in lower) or ("403" in lower) or ("unauthorized" in lower) or ("forbidden" in lower)
        # 429 / 限流
        is_rate_limited = ("429" in lower) or ("rate limit" in lower) or ("too many requests" in lower)
        is_model_not_found = ("model_not_found" in lower) or ("no available channel" in lower) or ("model not found" in lower)
        return {
            "message": msg,
            "is_503": is_503,
            "is_auth": is_auth,
            "is_rate_limited": is_rate_limited,
            "is_model_not_found": is_model_not_found,
        }

    @staticmethod
    def _public_error_message(info: dict) -> str:
        '''将引擎异常转换成可展示给用户的错误提示（脱敏、截断）。'''
        raw = str(info.get("message") or "").strip()
        # 简单脱敏：避免把 key/token 意外打到群里（就算 SDK 报错里带了）
        lowered = raw.lower()
        if "api_key" in lowered or "authorization" in lowered or "bearer" in lowered:
            raw = "(错误信息已脱敏)"

        # 截断，避免刷屏
        if len(raw) > 300:
            raw = raw[:300] + "..."

        if info.get("is_model_not_found"):
            return f"❌ AI 模型不可用（model_not_found）：{raw}"
        if info.get("is_auth"):
            return f"❌ AI 鉴权失败（401/403）：{raw}"
        if info.get("is_rate_limited"):
            return f"❌ AI 请求过于频繁（429）：{raw}"
        if info.get("is_503"):
            return f"❌ AI 服务暂时不可用（503）：{raw}"
        return f"❌ AI 调用失败：{raw}"

    async def chat(self, chat_id: int, user_content: str, bypass_dispatch_and_whitelist: bool = False) -> Optional[str]:
        """处理对话并返回 AI 回复"""
        config = self._get_config()
        if not config.enabled:
            return None

        if not bypass_dispatch_and_whitelist:
            # 分发开关（Telegram: 私聊 chat_id > 0，群组/超级群 chat_id < 0）
            if chat_id > 0 and not getattr(config, "enable_private_chat", True):
                return None
            if chat_id < 0 and not getattr(config, "enable_group_chat", True):
                return None

            # 权限校验（白名单）
            if config.white_list_chats and chat_id not in config.white_list_chats:
                return None

        # 获取或从数据库恢复会话
        if chat_id not in self._sessions:
            history = await self.ai_repo.get_history(chat_id, limit=config.max_history)
            session = AiConversation(chat_id=chat_id)
            # 始终确保有系统提示词
            session.add_message("system", config.system_prompt)
            # 恢复历史（排除重复的 system 消息）
            for role, content in history:
                if role != "system":
                    session.add_message(role, content)
            self._sessions[chat_id] = session
            
        session = self._sessions[chat_id]
        
        # 保存用户消息到数据库
        await self.ai_repo.save_message(chat_id, "user", user_content)
        session.add_message("user", user_content, max_history=config.max_history)
        
        try:
            response = await self.engine.generate_response(
                messages=session.messages,
                model=config.model
            )
            if response:
                # 保存 AI 回复到数据库
                await self.ai_repo.save_message(chat_id, "assistant", response)
                session.add_message("assistant", response, max_history=config.max_history)
                return response
        except Exception as e:
            info = self._classify_engine_error(e)
            # 记录完整异常，返回值交由上层按“失败”处理
            logger.exception("AI 对话生成失败: %s", info["message"])
            return self._public_error_message(info)
            
        return None

    async def chat_with_image(
        self,
        chat_id: int,
        user_content: str,
        image_bytes: bytes,
        bypass_dispatch_and_whitelist: bool = False,
    ) -> Optional[str]:
        '''处理图片+文本对话并返回 AI 回复'''
        config = self._get_config()
        if not config.enabled:
            return None

        if not bypass_dispatch_and_whitelist:
            if chat_id > 0 and not getattr(config, "enable_private_chat", True):
                return None
            if chat_id < 0 and not getattr(config, "enable_group_chat", True):
                return None
            if config.white_list_chats and chat_id not in config.white_list_chats:
                return None

        if chat_id not in self._sessions:
            history = await self.ai_repo.get_history(chat_id, limit=config.max_history)
            session = AiConversation(chat_id=chat_id)
            session.add_message("system", config.system_prompt)
            for role, content in history:
                if role != "system":
                    session.add_message(role, content)
            self._sessions[chat_id] = session

        session = self._sessions[chat_id]

        await self.ai_repo.save_message(chat_id, "user", user_content)
        session.add_message("user", user_content, max_history=config.max_history)

        try:
            response = await self.engine.generate_response(
                messages=session.messages,
                model=config.model,
                image_bytes=image_bytes,
            )
            if response:
                await self.ai_repo.save_message(chat_id, "assistant", response)
                session.add_message("assistant", response, max_history=config.max_history)
                return response
        except Exception as e:
            info = self._classify_engine_error(e)
            logger.exception("AI 图片对话生成失败: %s", info["message"])
            return self._public_error_message(info)

        return None

    def update_system_prompt(self, new_prompt: str):
        """更新系统提示词并重置所有会话的系统消息"""
        self.state_repo.set("ai", "system_prompt", new_prompt)
        for session in self._sessions.values():
            # 简单的做法是重置会话，或者只替换第一条 system 消息
            if session.messages and session.messages[0].role == "system":
                session.messages[0].content = new_prompt
            else:
                session.messages.insert(0, AiMessage(role="system", content=new_prompt))

    def toggle_ai(self, enabled: bool):
        """开关 AI 功能"""
        self.state_repo.set("ai", "enabled", enabled)
