from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.machinery
import logging
import multiprocessing
import sys
import threading
import types
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable


logger = logging.getLogger("awbotnest.services.ocr")

_WORKER_MODELS: dict[str, Any] = {}
_ALLOWED_MODELS = {"default", "old", "beta", "detection", "full"}
_ALLOWED_OPERATIONS = {"classification", "classification_many", "detection", "slide_match"}


def _worker_model(name: str) -> Any:
    """Create OCR models only inside the disposable worker process."""
    if name not in _ALLOWED_MODELS:
        raise ValueError(f"不支持的 OCR 模型：{name}")
    model = _WORKER_MODELS.get(name)
    if model is not None:
        return model

    import ddddocr

    if name == "old":
        model = ddddocr.DdddOcr(show_ad=False, old=True)
    elif name == "beta":
        model = ddddocr.DdddOcr(show_ad=False, beta=True)
    elif name == "detection":
        model = ddddocr.DdddOcr(det=True, ocr=False, show_ad=False)
    elif name == "full":
        model = ddddocr.DdddOcr(det=True, ocr=True, show_ad=False)
    else:
        model = ddddocr.DdddOcr(show_ad=False)
    _WORKER_MODELS[name] = model
    return model


def _worker_call(model_name: str, operation: str, args: tuple[Any, ...],
                 kwargs: dict[str, Any]) -> Any:
    if operation not in _ALLOWED_OPERATIONS:
        raise ValueError(f"不支持的 OCR 操作：{operation}")
    model = _worker_model(model_name)
    if operation == "classification_many":
        images = args[0] if args else ()
        return [model.classification(image) for image in images]
    return getattr(model, operation)(*args, **kwargs)


class OcrClient:
    """Synchronous ddddocr-compatible facade for code already running in a worker thread."""

    def __init__(self, service: OcrService, model: str) -> None:
        self._service = service
        self._model = model

    def classification(self, image: bytes) -> str:
        return self._service.call_sync(self._model, "classification", image)

    def classification_many(self, images: list[bytes] | tuple[bytes, ...]) -> list[str]:
        return self._service.call_sync(self._model, "classification_many", list(images))

    def detection(self, image: bytes) -> Any:
        return self._service.call_sync(self._model, "detection", image)

    def slide_match(self, target: bytes, background: bytes, **kwargs: Any) -> Any:
        return self._service.call_sync(
            self._model, "slide_match", target, background, **kwargs,
        )


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

        def create_model(*args: Any, **kwargs: Any) -> OcrClient:
            if args:
                raise TypeError("系统托管 OCR 只支持关键字参数")
            unsupported = set(kwargs) - {"show_ad", "old", "det", "ocr", "beta"}
            if unsupported:
                raise TypeError("系统托管 OCR 不支持参数：" + "、".join(sorted(unsupported)))
            if kwargs.get("old"):
                model = "old"
            elif kwargs.get("det"):
                model = "full" if kwargs.get("ocr", True) else "detection"
            elif kwargs.get("beta"):
                model = "beta"
            else:
                model = "default"
            return service.client(model)

        facade.DdddOcr = create_model
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
                  timeout: float = 60.0, **kwargs: Any) -> Any:
        if model not in _ALLOWED_MODELS or operation not in _ALLOWED_OPERATIONS:
            raise ValueError("OCR 调用参数不受支持")
        if not self.is_available():
            raise RuntimeError("OCR 依赖尚未安装")

        with self._call_lock:
            with self._state_lock:
                self._cancel_idle_timer_locked()
                executor = self._ensure_executor_locked()
                self._busy = True
            try:
                future = executor.submit(_worker_call, model, operation, args, kwargs)
                return future.result(timeout=max(1.0, float(timeout)))
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

    async def call(self, model: str, operation: str, *args: Any,
                   timeout: float = 60.0, **kwargs: Any) -> Any:
        return await asyncio.to_thread(
            self.call_sync, model, operation, *args, timeout=timeout, **kwargs,
        )

    async def classification(self, image: bytes, *, model: str = "default",
                             timeout: float = 60.0) -> str:
        return await self.call(model, "classification", image, timeout=timeout)

    async def classification_many(self, images: list[bytes] | tuple[bytes, ...], *,
                                  model: str = "default", timeout: float = 60.0) -> list[str]:
        return await self.call(
            model, "classification_many", list(images), timeout=timeout,
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
