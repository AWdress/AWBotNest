"""
core/ports/ocr.py
OCR 接口 - Protocol 定义

依赖方向：ports 只知道 domain，不知道 ddddocr / PaddleOCR
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.domain.red_packet import OcrResult


@runtime_checkable
class OcrPort(Protocol):
    """OCR 识别接口 - 所有 OCR 适配器必须实现此协议"""

    async def recognize(self, image_bytes: bytes) -> OcrResult:
        """
        识别图片中的文字（口令）

        Args:
            image_bytes: 原始图片字节数据（JPEG/PNG）

        Returns:
            OcrResult: 识别结果，含 keyword 和 success 标志
        """
        ...

    def is_available(self) -> bool:
        """检查 OCR 引擎是否可用"""
        ...
