"""
kernel/browser.py
平台级浏览器自动化能力，供插件通过 ctx.browser 使用（无需自己装浏览器）。

引擎选择：
- 优先 CloakBrowser（停用 Chromium，过 Cloudflare/指纹检测），是 Playwright 的 drop-in 替代。
- CloakBrowser 不可用（未装成/内核未下载）时自动回退平台内置的 Playwright Chromium。

安装策略（镜像不烤浏览器二进制以减小体积；浏览器是可选插件能力，故懒加载）：
- 镜像只装 Chromium 运行所需的系统库（Dockerfile 的 `playwright install-deps chromium`）。
- 浏览器内核**不在启动时下载**，而是在插件**首次调用 ctx.browser 时**才下到 data/browser_cache
  （随卷持久化，容器重建不必重下）：优先备 CloakBrowser（pip 安装 + `python -m cloakbrowser
  install`）；不可用时改下 Playwright chromium 兜底。不用浏览器的部署零开销、不占额外磁盘。

对插件暴露（ctx.browser，均为 async，内部在线程里跑同步浏览器 API）：
    html = await ctx.browser.page_source(url, cookies=?, ua=?, headless=True, timeout=60)
    result = await ctx.browser.run(url, callback, ...)   # callback(page) 为同步函数，收到同步 page
    ctx.browser.engine   # 当前可用引擎："cloakbrowser" | "playwright" | None
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from libs.log import logger

# 运行时只使用已经安装的包装器和内核，避免每次启动浏览器时联网检查更新。
os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")

# 浏览器内核缓存目录（随 data/ 卷持久化；main.py 已把 HOME 指到这里，
# 故 cloakbrowser 的 ~/.cloakbrowser 实际落在此目录下，容器重建不必重下）。
BROWSER_CACHE_DIR = Path(os.getcwd()) / "data" / "browser_cache"

_cloak_kernel_ready = False   # cloakbrowser 内核是否已下载就绪
_pw_chromium_ready = False    # playwright chromium 二进制是否已下载就绪（兜底）

# 浏览器进程启动很重。短时间复用同一进程，并在一个专用线程中串行操作，既遵守
# Playwright 的线程约束，也避免小内存服务器同时拉起多个 Chromium。
try:
    BROWSER_IDLE_TIMEOUT = max(30, int(os.getenv("AWBOT_BROWSER_IDLE_TIMEOUT", "120")))
except (TypeError, ValueError):
    BROWSER_IDLE_TIMEOUT = 120


def _cloak_importable() -> bool:
    try:
        import cloakbrowser  # noqa: F401
        return True
    except Exception:
        return False


def _playwright_importable() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────
# 启动预热：装 cloakbrowser + 下内核（后台、容错）
# ──────────────────────────────────────────────
def _subprocess_env() -> dict:
    """构造装内核子进程的环境变量：
    - PYTHONPATH 带上 data/plugin_deps —— cloakbrowser 是用 `pip --target` 装到那里的，
      新起的 `python -m cloakbrowser` 子进程默认看不到，必须显式加进 PYTHONPATH，
      否则报 "No module named cloakbrowser"。
    - 出站套平台代理（墙内拉内核需要）。
    """
    env = dict(os.environ)
    try:
        from kernel import deps
        target = str(deps.PLUGIN_DEPS_DIR.resolve())
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = target + (os.pathsep + prev if prev else "")
    except Exception:  # noqa: BLE001 - 取不到目录也不致命
        pass
    try:
        from libs.proxy import proxy_url
        px = proxy_url()
        if px:
            env.setdefault("HTTPS_PROXY", px)
            env.setdefault("https_proxy", px)
    except Exception:  # noqa: BLE001
        pass
    return env


def _ensure_cloakbrowser_sync() -> None:
    """同步安装 cloakbrowser（缺失则 pip 装到 plugin_deps）并下载内核。全程容错。"""
    global _cloak_kernel_ready
    BROWSER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 1) pip 安装 cloakbrowser 包（复用插件依赖安装器：装进 data/plugin_deps 持久化目录）
    if not _cloak_importable():
        try:
            from kernel import deps
            ok, out = deps._pip_install(["cloakbrowser"])
            if not ok:
                tail = " | ".join((out or "").strip().splitlines()[-3:]) or "无输出"
                logger.warning("cloakbrowser 安装失败，浏览器将回退 Playwright：%s", tail)
                return
            import importlib
            importlib.invalidate_caches()
        except Exception as e:  # noqa: BLE001
            logger.warning("cloakbrowser 安装异常，浏览器将回退 Playwright：%r", e)
            return

    # 2) 下载 CloakBrowser 内核（等价 `python -m cloakbrowser install`）。
    #    HOME 已在 main.py 指向 data/browser_cache，内核落在卷内、可持久化。
    #    PYTHONPATH 必须带上 plugin_deps，否则子进程找不到刚 --target 装的 cloakbrowser。
    try:
        env = _subprocess_env()
        logger.info("正在下载 CloakBrowser 内核，可能需要几分钟…")
        proc = subprocess.run(
            [sys.executable, "-m", "cloakbrowser", "install"],
            capture_output=True, text=True, timeout=600, env=env,  # 降至 10 分钟
        )
        if proc.returncode == 0:
            _cloak_kernel_ready = True
            logger.info("CloakBrowser 内核已就绪（浏览器优先使用 CloakBrowser）")
        else:
            tail = ((proc.stderr or proc.stdout) or "")[-300:]
            logger.warning("CloakBrowser 内核下载失败，浏览器将回退 Playwright：%s", tail)
    except subprocess.TimeoutExpired:
        logger.warning("CloakBrowser 内核下载超时（10 分钟），浏览器将回退 Playwright")
    except Exception as e:  # noqa: BLE001
        logger.warning("CloakBrowser 内核下载异常，浏览器将回退 Playwright：%r", e)


def _ensure_playwright_chromium_sync() -> None:
    """下载 Playwright chromium 二进制到 PLAYWRIGHT_BROWSERS_PATH（兜底引擎）。
    镜像不再内置该二进制，故首次需要时下载；已存在则 `playwright install` 快速跳过。"""
    global _pw_chromium_ready
    if not _playwright_importable():
        return
    try:
        env = _subprocess_env()
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=1800, env=env,
        )
        if proc.returncode == 0:
            _pw_chromium_ready = True
            logger.info("Playwright chromium 内核已就绪（浏览器兜底引擎）")
        else:
            tail = ((proc.stderr or proc.stdout) or "")[-300:]
            logger.warning("Playwright chromium 下载失败：%s", tail)
    except Exception as e:  # noqa: BLE001
        logger.warning("Playwright chromium 下载异常：%r", e)


def _ensure_browser_sync() -> None:
    """启动预热：优先备好 CloakBrowser；若不可用，再下载 Playwright chromium 兜底。
    只会下载其中一个（CloakBrowser 成则不下 Playwright），尽量省带宽与磁盘。"""
    _ensure_cloakbrowser_sync()
    if _cloak_kernel_ready:
        return
    logger.info("CloakBrowser 不可用，改备 Playwright chromium 兜底引擎")
    _ensure_playwright_chromium_sync()


_ensure_lock = threading.Lock()
_ensure_attempted = False


def _ensure_browser_once_sync() -> None:
    """懒加载：插件首次用浏览器时准备内核，仅完整尝试一次（避免每次调用都重跑安装）。
    尝试后即便未就绪也不再自动重跑 CloakBrowser 安装；Playwright 兜底仍会在
    浏览器进程管理器中按需补下，保证浏览器最终可用。"""
    global _ensure_attempted
    if _ensure_attempted:
        return
    with _ensure_lock:
        if _ensure_attempted:
            return
        logger.info("插件首次使用浏览器，开始准备内核…")
        _ensure_browser_sync()
        _ensure_attempted = True


# ──────────────────────────────────────────────
# 浏览器进程复用（同步）：优先 cloakbrowser，回退 playwright
# ──────────────────────────────────────────────
def _proxy_signature(proxy: Optional[Any]) -> str:
    if isinstance(proxy, dict):
        return repr(sorted((str(key), repr(value)) for key, value in proxy.items()))
    return repr(proxy)


class _BrowserWorker:
    """只在专用线程中使用的浏览器进程管理器。"""

    def __init__(self) -> None:
        self.engine: Optional[str] = None
        self.browser: Optional[Any] = None
        self.playwright: Optional[Any] = None
        self.signature: Optional[tuple[bool, str]] = None
        self.last_used = 0.0
        self.cloak_retry_at = 0.0

    def _alive(self) -> bool:
        if self.browser is None:
            return False
        checker = getattr(self.browser, "is_connected", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - 异常连接按已断开处理
            return False

    def _close(self) -> None:
        browser, playwright = self.browser, self.playwright
        self.browser = None
        self.playwright = None
        self.engine = None
        self.signature = None
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - 关闭时尽力释放
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
                pass

    def _launch_playwright(self, headless: bool, proxy: Optional[Any]) -> None:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        try:
            launch_kw: dict[str, Any] = {"headless": headless}
            if proxy:
                launch_kw["proxy"] = {"server": proxy} if isinstance(proxy, str) else proxy
            browser = playwright.chromium.launch(**launch_kw)
        except Exception:
            playwright.stop()
            raise
        self.engine = "playwright"
        self.browser = browser
        self.playwright = playwright

    def _ensure_process(self, headless: bool, proxy: Optional[Any]) -> None:
        _ensure_browser_once_sync()
        signature = (bool(headless), _proxy_signature(proxy))
        if self.signature == signature and self._alive():
            return
        self._close()

        if _cloak_importable() and time.monotonic() >= self.cloak_retry_at:
            try:
                from cloakbrowser import launch

                launch_kw: dict[str, Any] = {"headless": headless}
                if proxy:
                    launch_kw["proxy"] = proxy
                self.browser = launch(**launch_kw)
                self.engine = "cloakbrowser"
                self.signature = signature
                self.cloak_retry_at = 0.0
                logger.info("CloakBrowser 已启动，后续浏览器任务将复用当前进程")
                return
            except Exception as exc:  # noqa: BLE001 - 启动失败时回退
                self._close()
                self.cloak_retry_at = time.monotonic() + 600
                logger.warning(
                    "CloakBrowser 启动失败，10 分钟内直接使用 Playwright，避免重复启动拖慢平台（%s）",
                    type(exc).__name__,
                )

        if not _playwright_importable():
            raise RuntimeError("浏览器不可用：CloakBrowser 未就绪且 Playwright 未安装")
        try:
            self._launch_playwright(headless, proxy)
        except Exception as exc:  # noqa: BLE001 - 多半是 Chromium 尚未下载
            if _pw_chromium_ready:
                raise
            logger.info("Playwright Chromium 未就绪，正在按需下载后重试：%r", exc)
            _ensure_playwright_chromium_sync()
            self._launch_playwright(headless, proxy)
        self.signature = signature
        logger.info("Playwright Chromium 已启动，后续浏览器任务将复用当前进程")

    def run(self, url: str, action: Callable[[Any], Any], *,
            cookies: Optional[str], user_agent: Optional[str],
            headless: bool, timeout: int, proxy: Optional[Any],
            wait_network_idle: bool) -> Any:
        """复用浏览器进程，为单次任务创建隔离上下文和页面。"""
        self._ensure_process(headless, proxy)
        if self.browser is None:
            raise RuntimeError("浏览器启动失败")

        context = None
        page = None
        try:
            context_kw: dict[str, Any] = {}
            if user_agent:
                context_kw["user_agent"] = user_agent
            try:
                context = self.browser.new_context(**context_kw)
            except Exception:
                if self._alive():
                    raise
                # 浏览器可能在空闲复用期间被系统回收；上下文尚未创建，没有业务副作用，
                # 可以安全重启一次后继续本次任务。
                self._close()
                self._ensure_process(headless, proxy)
                if self.browser is None:
                    raise RuntimeError("浏览器重启失败")
                context = self.browser.new_context(**context_kw)
            page = context.new_page()
            if hasattr(page, "set_default_timeout"):
                page.set_default_timeout(int(timeout) * 1000)
            if cookies:
                page.set_extra_http_headers({"cookie": cookies})
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout) * 1000)
            if wait_network_idle:
                try:
                    page.wait_for_load_state("networkidle", timeout=min(int(timeout), 15) * 1000)
                except Exception:  # noqa: BLE001 - networkidle 超时不算失败
                    pass
            return action(page)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
            self.last_used = time.monotonic()

    def close_if_idle(self) -> None:
        if self.browser is None:
            return
        if time.monotonic() - self.last_used < BROWSER_IDLE_TIMEOUT:
            return
        logger.info("浏览器已空闲 %s 秒，释放后台进程", BROWSER_IDLE_TIMEOUT)
        self._close()

    def close(self) -> None:
        self._close()


_browser_worker = _BrowserWorker()
_browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="awbot-browser")


class BrowserHelper:
    """插件用的浏览器封装。任务相互隔离，并复用短时存活的浏览器进程。"""

    def __init__(self) -> None:
        self._idle_task: Optional[asyncio.Task] = None

    async def _close_after_idle(self) -> None:
        try:
            await asyncio.sleep(BROWSER_IDLE_TIMEOUT)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_browser_executor, _browser_worker.close_if_idle)
        except asyncio.CancelledError:
            pass

    def _schedule_idle_close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._close_after_idle())

    async def _run(self, url: str, action: Callable[[Any], Any], *,
                   cookies: Optional[str], user_agent: Optional[str],
                   headless: bool, timeout: int, proxy: Optional[Any],
                   wait_network_idle: bool = False) -> Any:
        loop = asyncio.get_running_loop()
        call = functools.partial(
            _browser_worker.run, url, action,
            cookies=cookies, user_agent=user_agent, headless=headless,
            timeout=timeout, proxy=proxy, wait_network_idle=wait_network_idle,
        )
        try:
            return await loop.run_in_executor(_browser_executor, call)
        finally:
            self._schedule_idle_close()

    @property
    def engine(self) -> Optional[str]:
        """已就绪的引擎名（"cloakbrowser" / "playwright"）。
        懒加载：插件还没用过浏览器（内核未下载）时返回 None。"""
        if _browser_worker.engine:
            return _browser_worker.engine
        if _cloak_kernel_ready:
            return "cloakbrowser"
        if _pw_chromium_ready:
            return "playwright"
        return None

    async def page_source(self, url: str, *, cookies: Optional[str] = None,
                          ua: Optional[str] = None, headless: bool = True,
                          timeout: int = 60, proxy: Optional[Any] = None) -> str:
        """打开 url 并返回渲染后的 HTML 源码。"""
        if proxy is None:
            from libs.proxy import proxy_url
            proxy = proxy_url()
        return await self._run(
            url, lambda p: p.content(),
            cookies=cookies, user_agent=ua, headless=headless, timeout=timeout, proxy=proxy,
            wait_network_idle=True,
        )

    async def run(self, url: str, action: Callable[[Any], Any], *,
                  cookies: Optional[str] = None, ua: Optional[str] = None,
                  headless: bool = True, timeout: int = 60, proxy: Optional[Any] = None) -> Any:
        """打开 url 后，在浏览器线程内执行 action(page)（同步函数，收到同步 page 对象），返回其结果。

            def grab(page):
                page.click("#login")
                return page.inner_text("#result")
            text = await ctx.browser.run(url, grab)
        """
        if proxy is None:
            from libs.proxy import proxy_url
            proxy = proxy_url()
        return await self._run(
            url, action,
            cookies=cookies, user_agent=ua, headless=headless, timeout=timeout, proxy=proxy,
        )

    async def shutdown(self) -> None:
        """平台退出时主动关闭浏览器进程。"""
        idle_task, self._idle_task = self._idle_task, None
        if idle_task is not None:
            idle_task.cancel()
            try:
                await idle_task
            except asyncio.CancelledError:
                pass
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_browser_executor, _browser_worker.close)


# 平台级单例；浏览器对象只在专用工作线程中访问。
browser = BrowserHelper()
