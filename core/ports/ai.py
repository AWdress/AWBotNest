from typing import List, Protocol, runtime_checkable
from core.domain.ai import AiMessage

@runtime_checkable
class AiEnginePort(Protocol):
    """AI 引擎接口定义"""
    async def generate_response(
        self,
        messages: List[AiMessage],
        model: str,
        temperature: float = 0.7,
        image_bytes: bytes | None = None
    ) -> str:
        """生成 AI 回复"""
        ...
