"""
adapters/ocr/ddddocr_adapter.py
ddddocr OCR 适配器 - 实现 core/ports/ocr.py::OcrPort

迁移自：user_scripts/games/red_packet.py 中的 OCR 相关函数
改进：
- 正确使用 old=True 旧模型（与默认模型权重不同）
- PIL 二值化预处理（滤除彩色背景噪点）
- 多策略投票（默认/原图、默认/阈值80、默认/阈值110、旧模型/阈值80）
- 全部改为 async（通过线程池执行同步 ddddocr 调用）
"""
from __future__ import annotations

import asyncio
import io
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from core.domain.red_packet import OcrResult


# 线程池：ddddocr 是同步 CPU 密集型操作
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ocr")


class DdddOcrAdapter:
    """
    ddddocr 双模型 OCR 适配器

    策略：
    1. 默认模型 + 原图
    2. 默认模型 + 阈值 80 预处理
    3. 默认模型 + 阈值 110 预处理
    4. 旧版模型 + 阈值 80 预处理（若可用）
    取最长非空结果作为最终口令。
    """

    def __init__(self) -> None:
        self._ocr: object = None
        self._ocr_old: object = None
        self._initialized: bool = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        try:
            import ddddocr  # noqa: PLC0415
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        except ImportError as exc:
            raise RuntimeError("ddddocr 未安装，请执行: pip install ddddocr") from exc

        try:
            import ddddocr  # noqa: PLC0415
            self._ocr_old = ddddocr.DdddOcr(show_ad=False, old=True)
        except Exception:
            self._ocr_old = None

        self._initialized = True

    def is_available(self) -> bool:
        try:
            import ddddocr  # noqa: PLC0415, F401
            return True
        except ImportError:
            return False

    async def recognize(self, image_bytes: bytes) -> OcrResult:
        """异步识别，通过线程池执行同步调用"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _executor,
            self._recognize_sync,
            image_bytes,
        )

    def _recognize_sync(self, image_bytes: bytes) -> OcrResult:
        start = time.monotonic()
        try:
            self._ensure_initialized()
        except RuntimeError as e:
            return OcrResult(raw_bytes=image_bytes, keyword="", success=False, error=str(e))

        candidates: list[str] = []

        # 策略 1: 默认模型 + 原图
        kw = self._try_ocr(self._ocr, image_bytes, "默认/原图")
        candidates.append(kw)

        # 策略 2 & 3: 默认模型 + 预处理（阈值 80 / 110）
        for threshold in (80, 110):
            processed = _preprocess(image_bytes, threshold)
            kw = self._try_ocr(self._ocr, processed, f"默认/阈值{threshold}")
            candidates.append(kw)

        # 策略 4: 旧版模型 + 阈值 80
        if self._ocr_old is not None:
            processed = _preprocess(image_bytes, 80)
            kw = self._try_ocr(self._ocr_old, processed, "旧版/阈值80")
            candidates.append(kw)

        # 取最长非空结果
        best = max(
            (c for c in candidates if c),
            key=len,
            default="",
        )
        elapsed = (time.monotonic() - start) * 1000

        return OcrResult(
            raw_bytes=image_bytes,
            keyword=best,
            model_used="dual",
            elapsed_ms=elapsed,
            success=bool(best),
            error="" if best else "所有策略均未识别到内容",
        )

    @staticmethod
    def _try_ocr(model: object, img_bytes: bytes, label: str) -> str:
        try:
            result = model.classification(img_bytes)  # type: ignore[attr-defined]
            return str(result).strip() if result else ""
        except Exception:
            return ""


# ------------------------------------------------------------------ #
# 图像预处理工具（提取为模块级函数，方便测试）                        #
# ------------------------------------------------------------------ #

def _preprocess(img_bytes: bytes, threshold: int = 85) -> bytes:
    """
    二值化预处理：
    - 转灰度
    - AutoContrast 拉伸
    - 阈值二值化：深色像素（文字）→ 黑，亮色（背景）→ 白

    Args:
        img_bytes: 原始图片字节
        threshold: 灰度阈值，低于此值视为文字（默认 85）

    Returns:
        预处理后的 PNG 字节（失败时返回原图）
    """
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
        img = ImageOps.autocontrast(img, cutoff=1)
        bw = img.point(lambda p: 0 if p < threshold else 255, "L")
        out = io.BytesIO()
        bw.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return img_bytes
