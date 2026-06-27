from typing import List, Optional
from pydantic import BaseModel, Field

class AiMessage(BaseModel):
    """单条 AI 对话消息"""
    role: str  # system, user, assistant
    content: str

class AiConversation(BaseModel):
    """AI 对话上下文"""
    chat_id: int
    messages: List[AiMessage] = Field(default_factory=list)
    
    def add_message(self, role: str, content: str, max_history: int = 10):
        self.messages.append(AiMessage(role=role, content=content))
        # 裁剪历史记录，保留系统提示词和最近的对话
        if len(self.messages) > max_history:
            system_msgs = [m for m in self.messages if m.role == "system"]
            other_msgs = [m for m in self.messages if m.role != "system"]
            self.messages = system_msgs + other_msgs[-(max_history - len(system_msgs)):]

class AiConfig(BaseModel):
    """AI 全局配置领域模型"""
    enabled: bool = False
    provider: str = "openai"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: str = "gpt-3.5-turbo"
    system_prompt: str = "你是一个有用的助手。"
    explain_prompt: str = (
        "你是一个群聊消息解读助手。请根据用户【回复的消息内容】进行解释与答疑，简明清晰。\n"
        "输出结构：\n"
        "1) 这句话/这段话的主要意思\n"
        "2) 语气/态度（例如：调侃、质疑、吐槽、命令、求助等）\n"
        "3) 可能的隐含信息或上下文（没有就写‘无’）\n\n"
        "需要解释的消息内容：{content}"
    )
    max_history: int = 10
    white_list_chats: List[int] = Field(default_factory=list)
    enable_private_chat: bool = True
    enable_group_chat: bool = True
    enable_explain_command: bool = True
    enable_explain_prompt: bool = False
