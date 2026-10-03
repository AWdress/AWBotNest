from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import importlib.machinery
import logging
import multiprocessing
import sys
import threading
import time
import types
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable


logger = logging.getLogger("awbotnest.services.ocr")

_WORKER_MODELS: OrderedDict[Any, Any] = OrderedDict()
_WORKER_RANGES: dict[Any, Any] = {}
_ALLOWED_MODELS = {"default", "old", "beta", "detection", "full", "slider"}
_ALLOWED_OPERATIONS = {
    "classification", "classification_many", "detection", "slide_match",
    "slide_comparison", "set_ranges", "get_charset", "get_model_info",
}


def _model_options(name: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"show_ad": False}
    if name == "old":
        result["old"] = True
    elif name == "beta":
        result["beta"] = True
    elif name in {"detection", "full"}:
        result.update(det=True, ocr=name == "full")
    elif name == "slider":
        result.update(det=False, ocr=False)
    result.update(options or {})
    # Ads are not useful in the shared system log.
    result["show_ad"] = False
    return result


def _model_key(name: str, options: dict[str, Any] | None = None) -> Any:
    return name, tuple(sorted(_model_options(name, options).items()))


def _worker_model(name: str, options: dict[str, Any] | None = None) -> Any:
    """Create OCR models only inside the disposable worker process."""
    if name not in _ALLOWED_MODELS:
        raise ValueError(f"不支持的 OCR 模型：{name}")
    key = _model_key(name, options)
    model = _WORKER_MODELS.get(key)
    if model is not None:
        _WORKER_MODELS.move_to_end(key)
        return model

    import ddddocr

    # Custom models/device choices must not grow this cache without a bound.
    if len(_WORKER_MODELS) >= 3:
        old_key, old_model = _WORKER_MODELS.popitem(last=False)
        _WORKER_RANGES.pop(old_key, None)
        cleanup = getattr(old_model, "cleanup", None)
        if cleanup is not None:
            cleanup()
        del old_model
    model = ddddocr.DdddOcr(**_model_options(name, options))
    _WORKER_MODELS[key] = model
    return model


def _worker_call(model_name: str, operation: str, args: tuple[Any, ...],
                 kwargs: dict[str, Any], options: dict[str, Any] | None = None,
                 charset_range: Any = None) -> Any:
    if operation not in _ALLOWED_OPERATIONS:
        raise ValueError(f"不支持的 OCR 操作：{operation}")
    model = _worker_model(model_name, options)
    key = _model_key(model_name, options)
    if operation in {"classification", "classification_many", "get_charset"}:
        previous_range = _WORKER_RANGES.get(key)
        if charset_range is not None:
            model.set_ranges(charset_range)
            _WORKER_RANGES[key] = copy.deepcopy(charset_range)
        elif previous_range is not None:
            # Never let one plugin's character restriction affect another.
            get_charset = getattr(model, "get_charset", None)
            charset = get_charset() if get_charset else getattr(model, "_DdddOcr__charset", None)
            if charset is not None:
                model.set_ranges(charset)
            else:
                _WORKER_MODELS.pop(key, None)
                cleanup = getattr(model, "cleanup", None)
                if cleanup:
                    cleanup()
                model = _worker_model(model_name, options)
            _WORKER_RANGES.pop(key, None)
    if operation == "set_ranges":
        result = model.set_ranges(*args, **kwargs)
        _WORKER_RANGES[key] = copy.deepcopy(args[0])
        return result
    if operation == "classification_many":
        images = args[0] if args else ()
        return [model.classification(image, **kwargs) for image in images]
    return getattr(model, operation)(*args, **kwargs)


class OcrClient:
    """Synchronous ddddocr-compatible facade for code already running in a worker thread."""

    def __init__(self, service: OcrService, model: str,
                 options: dict[str, Any] | None = None) -> None:
        self._service = service
        self._model = model
        self._options = dict(options or {})
        self._ranges: Any = None
        self._closed = False

    def _invoke(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("OCR 对象已清理")
        return self._service.call_sync(
            self._model, operation, *args, _model_config=self._options,
            _charset_range=self._ranges, **kwargs,
        )

    def classification(self, img: Any, png_fix: bool = False,
                       probability: bool = False, color_filter_colors: Any = None,
                       color_filter_custom_ranges: Any = None, **kwargs: Any) -> Any:
        if png_fix:
            kwargs["png_fix"] = png_fix
        if probability:
            kwargs["probability"] = probability
        if color_filter_colors is not None:
            kwargs["color_filter_colors"] = color_filter_colors
        if color_filter_custom_ranges is not None:
            kwargs["color_filter_custom_ranges"] = color_filter_custom_ranges
        return self._invoke("classification", img, **kwargs)

    def classification_many(self, images: list[Any] | tuple[Any, ...], **kwargs: Any) -> list[Any]:
        return self._invoke("classification_many", list(images), **kwargs)

    def detection(self, img: Any) -> Any:
        return self._invoke("detection", img)

    def slide_match(self, target_img: Any, background_img: Any,
                    simple_target: bool = False) -> Any:
        return self._invoke("slide_match", target_img, background_img, simple_target=simple_target)

    def slide_comparison(self, target_img: Any, background_img: Any) -> Any:
        return self._invoke("slide_comparison", target_img, background_img)

    def set_ranges(self, charset_range: Any) -> None:
        self._invoke("set_ranges", charset_range)
        self._ranges = copy.deepcopy(charset_range)

    def get_charset(self) -> list[str]:
        return self._invoke("get_charset")

    def get_model_info(self) -> dict[str, Any]:
        return self._invoke("get_model_info")

    def switch_device(self, use_gpu: bool, device_id: int = 0) -> None:
        self._options.update(use_gpu=use_gpu, device_id=device_id)

    def cleanup(self) -> None:
        # A shared model may still be in use by another client. Idle shutdown
        # owns its actual native resources; cleaning a client never breaks it.
        self._closed = True


class OcrService:
    """Run memory-heavy OCR models in one shared, disposable child process."""

    def __init__(self, *, idle_seconds: float = 60.0,
                 executor_factory: Callable[[], Any] | None = None) -> None:
        self.idle_seconds = max(0.01, float(idle_seconds))
        self._executor_factory = executor_factory or self._create_executor
        self._executor: Any | None = None
        self._idle_timer: threading.Timer | None = None
        self._state_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._module_facade: types.ModuleType | None = None

    @staticmethod
    def _create_executor() -> ProcessPoolExecutor:
        # Spawn avoids forking the multithreaded Telegram/ASGI process. It also
        # keeps ONNX Runtime, OpenCV and their large native allocators out of it.
        return ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        )

    @staticmethod
    def is_available() -> bool:
        # Package metadata can be checked without importing ddddocr/onnxruntime
        # into the main process. Dependencies may be installed after startup.
        try:
            importlib.metadata.version("ddddocr")
            return True
        except importlib.metadata.PackageNotFoundError:
            return False

    def install_module_facade(self) -> None:
        """Make existing plugins use the managed worker without code changes."""
        facade = types.ModuleType("ddddocr")
        facade.__doc__ = "AWBotNest managed ddddocr facade"
        facade.__spec__ = importlib.machinery.ModuleSpec("ddddocr", loader=None)
        try:
            facade.__version__ = importlib.metadata.version("ddddocr")
        except importlib.metadata.PackageNotFoundError:
            facade.__version__ = ""

        service = self

        class DdddOcr(OcrClient):
            def __init__(self, ocr: bool = True, det: bool = False,
                         old: bool = False, beta: bool = False,
                         use_gpu: bool = False, device_id: int = 0,
                         show_ad: bool = True, import_onnx_path: str = "",
                         charsets_path: str = "") -> None:
                if det:
                    model = "full" if ocr else "detection"
                elif not ocr and not import_onnx_path:
                    model = "slider"
                else:
                    model = "old" if old else "beta" if beta else "default"
                options = dict(ocr=ocr, det=det, old=old, beta=beta, use_gpu=use_gpu,
                               device_id=device_id, import_onnx_path=str(import_onnx_path),
                               charsets_path=str(charsets_path))
                super().__init__(service, model, options)
                self.det = self.det_enabled = det
                self.ocr_enabled = ocr
                self.old, self.beta = old, beta

        facade.DdddOcr = DdddOcr
        self._module_facade = facade
        sys.modules["ddddocr"] = facade

    def _remove_module_facade(self) -> None:
        if self._module_facade is not None and sys.modules.get("ddddocr") is self._module_facade:
            sys.modules.pop("ddddocr", None)
        self._module_facade = None

    def client(self, model: str = "default") -> OcrClient:
        if model not in _ALLOWED_MODELS:
            raise ValueError(f"不支持的 OCR 模型：{model}")
        return OcrClient(self, model)

    def _cancel_idle_timer_locked(self) -> None:
        timer = self._idle_timer
        self._idle_timer = None
        if timer is not None:
            timer.cancel()

    def _ensure_executor_locked(self) -> Any:
        if self._closed:
            raise RuntimeError("OCR 服务已停止")
        if self._executor is None:
            self._executor = self._executor_factory()
        return self._executor

    def _schedule_idle_shutdown_locked(self, executor: Any) -> None:
        self._cancel_idle_timer_locked()
        timer = threading.Timer(self.idle_seconds, self._shutdown_if_idle, args=(executor,))
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _shutdown_if_idle(self, expected: Any) -> None:
        with self._state_lock:
            if self._executor is not expected or self._busy:
                return
            self._executor = None
            self._idle_timer = None
        try:
            expected.shutdown(wait=True, cancel_futures=True)
        except Exception:
            logger.debug("OCR 工作进程空闲退出异常", exc_info=True)

    @staticmethod
    def _terminate_executor(executor: Any) -> None:
        # ProcessPoolExecutor has no public terminate API before Python 3.14.
        # Terminating its owned workers is required after a native OCR timeout;
        # otherwise the large ONNX allocation could remain resident forever.
        processes = list((getattr(executor, "_processes", None) or {}).values())
        for process in processes:
            try:
                process.terminate()
            except Exception:
                pass
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _discard_executor(self, executor: Any, *, terminate: bool) -> None:
        with self._state_lock:
            if self._executor is executor:
                self._executor = None
            self._cancel_idle_timer_locked()
        if terminate:
            self._terminate_executor(executor)
        else:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def call_sync(self, model: str, operation: str, *args: Any,
                  timeout: float = 60.0, _cancel_event: threading.Event | None = None,
                  _model_config: dict[str, Any] | None = None,
                  _charset_range: Any = None, **kwargs: Any) -> Any:
        if model not in _ALLOWED_MODELS or operation not in _ALLOWED_OPERATIONS:
            raise ValueError("OCR 调用参数不受支持")
        if not self.is_available():
            raise RuntimeError("OCR 依赖尚未安装")

        deadline = time.monotonic() + max(0.01, float(timeout))

        def remaining() -> float:
            if _cancel_event is not None and _cancel_event.is_set():
                raise asyncio.CancelledError()
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("OCR 识别超时")
            return value

        while not self._call_lock.acquire(timeout=min(0.05, remaining())):
            pass
        try:
            remaining()  # A cancelled waiter must never submit work.
            with self._state_lock:
                self._cancel_idle_timer_locked()
                executor = self._ensure_executor_locked()
                self._busy = True
            try:
                future = executor.submit(
                    _worker_call, model, operation, args, kwargs, _model_config, _charset_range,
                )
                while True:
                    wait_seconds = min(0.1, remaining())
                    try:
                        return future.result(timeout=wait_seconds)
                    except FutureTimeoutError:
                        if future.done():
                            raise
            except asyncio.CancelledError:
                self._discard_executor(executor, terminate=True)
                raise
            except FutureTimeoutError as exc:
                self._discard_executor(executor, terminate=True)
                raise TimeoutError("OCR 识别超时") from exc
            except BrokenProcessPool:
                self._discard_executor(executor, terminate=True)
                raise RuntimeError("OCR 工作进程异常退出") from None
            finally:
                with self._state_lock:
                    self._busy = False
                    if self._executor is executor and not self._closed:
                        self._schedule_idle_shutdown_locked(executor)
        finally:
            self._call_lock.release()

    async def call(self, model: str, operation: str, *args: Any,
                   timeout: float = 60.0, **kwargs: Any) -> Any:
        cancel = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(
            self.call_sync, model, operation, *args, timeout=timeout,
            _cancel_event=cancel, **kwargs,
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def classification(self, image: bytes, *, model: str = "default",
                             timeout: float = 60.0, **kwargs: Any) -> Any:
        return await self.call(model, "classification", image, timeout=timeout, **kwargs)

    async def classification_many(self, images: list[bytes] | tuple[bytes, ...], *,
                                  model: str = "default", timeout: float = 60.0,
                                  **kwargs: Any) -> list[Any]:
        return await self.call(
            model, "classification_many", list(images), timeout=timeout, **kwargs,
        )

    def close_sync(self) -> None:
        with self._state_lock:
            self._closed = True
            executor = self._executor
            busy = self._busy
            self._executor = None
            self._cancel_idle_timer_locked()
        if executor is not None:
            try:
                if busy:
                    self._terminate_executor(executor)
                else:
                    executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                self._terminate_executor(executor)
        self._remove_module_facade()

    async def close(self) -> None:
        await asyncio.to_thread(self.close_sync)
