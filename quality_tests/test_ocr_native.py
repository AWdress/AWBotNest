"""Optional real ONNX smoke test; never install plugin dependencies in CI.

Install ddddocr in the test environment and set AWBOTNEST_TEST_NATIVE_OCR=1.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import unittest

from awbotnest.services.ocr import OcrService


@unittest.skipUnless(os.getenv("AWBOTNEST_TEST_NATIVE_OCR") == "1", "需显式启用真实 OCR 依赖测试")
class NativeOcrTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_model_parameters_and_idle_process_exit(self):
        from PIL import Image, ImageDraw
        import psutil

        image = Image.new("RGB", (120, 40), "white")
        ImageDraw.Draw(image).text((10, 10), "1234", fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        native_before = "onnxruntime" in sys.modules
        service = OcrService(idle_seconds=0.2)
        self.addAsyncCleanup(service.close)

        batch = await service.classification_many([data, data], png_fix=True)
        self.assertEqual(len(batch), 2)
        self.assertTrue(all(isinstance(result, str) for result in batch))
        probabilities = await service.classification(data, probability=True)
        self.assertIsInstance(probabilities, dict)
        self.assertIn("probabilities", probabilities)
        self.assertEqual("onnxruntime" in sys.modules, native_before)

        executor = service._executor
        worker_pids = [worker.pid for worker in executor._processes.values()]
        self.assertTrue(worker_pids)
        for _ in range(50):
            if service._executor is None and not any(psutil.pid_exists(pid) for pid in worker_pids):
                break
            await asyncio.sleep(0.1)
        self.assertIsNone(service._executor)
        self.assertFalse(any(psutil.pid_exists(pid) for pid in worker_pids))
