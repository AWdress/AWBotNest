from typing import List
import base64
import openai
from core.domain.ai import AiMessage
from core.ports.ai import AiEnginePort
import logging

logger = logging.getLogger(__name__)


def _platform_proxy():
    """读取平台代理 URL（启用时），供 AI 客户端使用。"""
    try:
        import config.config as _cfg
        _cfg.reload()
        ps = getattr(_cfg, "proxy_set", {}) or {}
        if not ps.get("proxy_enable"):
            return None
        url = (ps.get("PROXY_URL") or "").strip()
        if url:
            return url
        px = ps.get("proxy", {}) or {}
        host, port = px.get("hostname"), px.get("port")
        if host and port:
            scheme = px.get("scheme", "http")
            user, pwd = px.get("username", ""), px.get("password", "")
            auth = f"{user}:{pwd}@" if user else ""
            return f"{scheme}://{auth}{host}:{port}"
    except Exception:  # noqa: BLE001
        pass
    return None

class OpenAIAdapter(AiEnginePort):
    """OpenAI 兼容接口适配器"""

    def __init__(self, api_key: str, base_url: str = None):
        self.api_key = api_key
        self.base_url = base_url
        # AI 接口走平台代理（启用时）：墙内访问 OpenAI/第三方需要
        http_client = None
        try:
            proxy_url = _platform_proxy()
            if proxy_url:
                import httpx
                http_client = httpx.AsyncClient(proxy=proxy_url, timeout=60)
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 代理初始化失败，将直连: %r", e)
        self.client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

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
