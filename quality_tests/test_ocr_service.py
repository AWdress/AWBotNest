from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from awbotnest.services.ocr import OcrClient, OcrService
from awbotnest.services import ocr as ocr_module


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

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.6.0")
    def test_compatibility_parameters_and_methods(self, _version) -> None:
        executor = _ImmediateExecutor()
        service = OcrService(executor_factory=lambda: executor)
        self.addCleanup(service.close_sync)
        service.install_module_facade()
        import ddddocr

        client = ddddocr.DdddOcr(True, False, False, False, False, 0, False,
                                "custom.onnx", "charset.json")
        self.assertIsInstance(client, ddddocr.DdddOcr)
        client.set_ranges("0123")
        client.classification(b"image", png_fix=True, probability=True,
                              color_filter_colors=["red"])
        submitted = executor.calls[-1][1]
        self.assertEqual(submitted[3], {
            "png_fix": True, "probability": True, "color_filter_colors": ["red"],
        })
        self.assertEqual(submitted[4]["import_onnx_path"], "custom.onnx")
        self.assertEqual(submitted[4]["charsets_path"], "charset.json")
        self.assertEqual(submitted[5], "0123")
        ranges = [((0, 50, 50), (10, 255, 255))]
        client.classification(b"image", False, False, ["red"], ranges)
        self.assertEqual(executor.calls[-1][1][3], {
            "color_filter_colors": ["red"], "color_filter_custom_ranges": ranges,
        })
        client.slide_comparison(b"target", b"background")
        self.assertEqual(executor.calls[-1][1][1], "slide_comparison")
        client.slide_match(b"target", b"background", True)
        self.assertEqual(executor.calls[-1][1][3], {"simple_target": True})
        client.get_charset()
        client.get_model_info()
        client.switch_device(True, 1)
        client.detection(b"image")
        self.assertEqual(executor.calls[-1][1][4]["device_id"], 1)

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.6.0")
    def test_pure_slider_does_not_request_ocr_model(self, _version) -> None:
        executor = _ImmediateExecutor()
        service = OcrService(executor_factory=lambda: executor)
        self.addCleanup(service.close_sync)
        service.install_module_facade()
        import ddddocr

        slider = ddddocr.DdddOcr(ocr=False, det=False)
        slider.slide_match(b"target", b"background")
        self.assertEqual(executor.calls[-1][1][0], "slider")
        self.assertFalse(executor.calls[-1][1][4]["ocr"])
        with patch.dict(sys.modules, {"ddddocr": types.SimpleNamespace(
            DdddOcr=lambda **kwargs: types.SimpleNamespace(options=kwargs),
        )}), patch.object(ocr_module, "_WORKER_MODELS", ocr_module.OrderedDict()):
            worker = ocr_module._worker_model("slider")
            self.assertFalse(worker.options["ocr"])
            self.assertFalse(worker.options["det"])

    def test_character_ranges_do_not_cross_clients_and_models_are_reused(self) -> None:
        class Model:
            def __init__(self, **kwargs):
                self.range = "all"
            def set_ranges(self, value):
                self.range = value
            def get_charset(self):
                return ["a", "1", ""]
            def classification(self, image, **kwargs):
                return self.range

        with patch.dict(sys.modules, {"ddddocr": types.SimpleNamespace(DdddOcr=Model)}), \
             patch.object(ocr_module, "_WORKER_MODELS", ocr_module.OrderedDict()), \
             patch.object(ocr_module, "_WORKER_RANGES", {}):
            call = ocr_module._worker_call
            self.assertEqual(call("default", "classification", (b"a",), {}, charset_range=0), 0)
            self.assertEqual(call("default", "classification", (b"b",), {}), ["a", "1", ""])
            self.assertEqual(call("default", "classification", (b"c",), {}, charset_range=1), 1)
            self.assertEqual(len(ocr_module._WORKER_MODELS), 1)

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.6.0")
    async def test_cancelled_queued_call_is_never_submitted(self, _version) -> None:
        executor = _ImmediateExecutor()
        active_future = Future()
        started = threading.Event()
        original_submit = executor.submit

        def submit(function, *args):
            if args[2] == (b"active",):
                executor.calls.append((function, args))
                started.set()
                return active_future
            return original_submit(function, *args)

        executor.submit = submit
        service = OcrService(executor_factory=lambda: executor)
        self.addAsyncCleanup(service.close)
        active = asyncio.create_task(service.classification(b"active"))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        queued = asyncio.create_task(service.classification(b"cancelled"))
        await asyncio.sleep(0.02)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        active_future.set_result("OK")
        self.assertEqual(await active, "OK")
        self.assertEqual([args[2] for _, args in executor.calls], [(b"active",)])

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.6.0")
    async def test_queue_wait_is_included_in_timeout(self, _version) -> None:
        executor = _ImmediateExecutor()
        active_future = Future()
        started = threading.Event()

        def submit(function, *args):
            executor.calls.append((function, args))
            started.set()
            return active_future

        executor.submit = submit
        service = OcrService(executor_factory=lambda: executor)
        self.addAsyncCleanup(service.close)
        active = asyncio.create_task(service.classification(b"active"))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        with self.assertRaisesRegex(TimeoutError, "OCR"):
            await service.classification(b"queued", timeout=0.05)
        active_future.set_result("OK")
        await active
        self.assertEqual(len(executor.calls), 1)

    @patch("awbotnest.services.ocr.importlib.metadata.version", return_value="1.6.0")
    async def test_running_cancel_and_timeout_discard_worker(self, _version) -> None:
        for cancel in (False, True):
            executor = _ImmediateExecutor()
            started = threading.Event()
            def submit(function, *args):
                started.set()
                return Future()
            executor.submit = submit
            service = OcrService(executor_factory=lambda: executor)
            task = asyncio.create_task(service.classification(b"image", timeout=0.05 if not cancel else 10))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            if cancel:
                task.cancel()
            with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                await asyncio.wait_for(task, 1)
            self.assertIsNone(service._executor)
            self.assertTrue(executor.shutdown_event.is_set())
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
