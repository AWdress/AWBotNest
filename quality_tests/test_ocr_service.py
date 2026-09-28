from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from awbotnest.services.ocr import OcrClient, OcrService


class _ImmediateExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []
        self.shutdown_event = threading.Event()

    def submit(self, function, *args):
        self.calls.append((function, args))
        future = Future()
        operation = args[1]
        if operation == "classification":
            future.set_result("ABC123")
        elif operation == "classification_many":
            future.set_result(["A", "ABC123"])
        else:
            future.set_result({"target": [12, 8]})
        return future

    def shutdown(self, *, wait=True, cancel_futures=True):
        self.shutdown_event.set()


class OcrServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_module = sys.modules.pop("ddddocr", None)

    def tearDown(self) -> None:
        sys.modules.pop("ddddocr", None)
        if self.original_module is not None:
            sys.modules["ddddocr"] = self.original_module

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.5.6")
    def test_existing_plugin_imports_receive_managed_client(self, _version) -> None:
        executor = _ImmediateExecutor()
        service = OcrService(idle_seconds=60, executor_factory=lambda: executor)
        service.install_module_facade()

        import ddddocr

        model = ddddocr.DdddOcr(show_ad=False, old=True)
        self.assertIsInstance(model, OcrClient)
        self.assertEqual(model.classification(b"image"), "ABC123")
        self.assertEqual(executor.calls[0][1][0:2], ("old", "classification"))
        service.close_sync()
        self.assertTrue(executor.shutdown_event.is_set())

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.5.6")
    def test_worker_exits_after_idle_period(self, _version) -> None:
        executor = _ImmediateExecutor()
        service = OcrService(idle_seconds=0.02, executor_factory=lambda: executor)

        self.assertEqual(
            service.call_sync("default", "classification", b"image"),
            "ABC123",
        )
        self.assertTrue(executor.shutdown_event.wait(1))
        self.assertIsNone(service._executor)
        service.close_sync()

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.5.6")
    async def test_async_batch_reuses_one_worker(self, _version) -> None:
        executor = _ImmediateExecutor()
        service = OcrService(idle_seconds=60, executor_factory=lambda: executor)

        result = await service.classification_many([b"one", b"two"])

        self.assertEqual(result, ["A", "ABC123"])
        self.assertEqual(len(executor.calls), 1)
        await service.close()

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.5.6")
    async def test_real_worker_process_returns_memory_after_idle(self, _version) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "ddddocr.py").write_text(
                "class DdddOcr:\n"
                "    def __init__(self, **kwargs): self.old = kwargs.get('old', False)\n"
                "    def classification(self, image): return 'OLD' if self.old else 'MAIN'\n",
                encoding="utf-8",
            )
            sys.path.insert(0, temporary)
            try:
                service = OcrService(idle_seconds=0.05)
                self.assertEqual(await service.classification(b"image"), "MAIN")
                await asyncio.sleep(0.3)
                self.assertIsNone(service._executor)
                await service.close()
            finally:
                sys.path.remove(temporary)


if __name__ == "__main__":
    unittest.main()
