from typing import List
import base64
import openai
from core.domain.ai import AiMessage
from core.ports.ai import AiEnginePort
import logging

logger = logging.getLogger(__name__)

class OpenAIAdapter(AiEnginePort):
    """OpenAI 兼容接口适配器"""
    
    def __init__(self, api_key: str, base_url: str = None):
        self.api_key = api_key
        self.base_url = base_url
        self.client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def generate_response(
        self, 
        messages: List[AiMessage], 
        model: str,
        temperature: float = 0.7,
        image_bytes: bytes | None = None
    ) -> str:
        try:
            # 转换为 OpenAI 要求的格式
            formatted_messages = [
                {"role": m.role, "content": m.content} for m in messages
            ]

            if image_bytes and formatted_messages:
                last_idx = -1
                for i in range(len(formatted_messages) - 1, -1, -1):
                    if formatted_messages[i].get("role") == "user":
                        last_idx = i
                        break
                if last_idx != -1:
                    text_content = str(formatted_messages[last_idx].get("content", "")).strip() or "请解释这张图片表达的内容。"
                    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                    formatted_messages[last_idx]["content"] = [
                        {"type": "text", "text": text_content},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ]
            
            response = await self.client.chat.completions.create(
                model=model,
                messages=formatted_messages,
                temperature=temperature
            )
            
            if response.choices:
                return response.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenAI API 调用失败: {e}")
            raise e
        return ""
