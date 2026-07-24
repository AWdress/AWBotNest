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
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from libs.log import logger

# 浏览器内核缓存目录（随 data/ 卷持久化；main.py 已把 HOME 指到这里，
# 故 cloakbrowser 的 ~/.cloakbrowser 实际落在此目录下，容器重建不必重下）。
BROWSER_CACHE_DIR = Path(os.getcwd()) / "data" / "browser_cache"

_cloak_kernel_ready = False   # cloakbrowser 内核是否已下载就绪
_pw_chromium_ready = False    # playwright chromium 二进制是否已下载就绪（兜底）


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
        proc = subprocess.run(
            [sys.executable, "-m", "cloakbrowser", "install"],
            capture_output=True, text=True, timeout=1800, env=env,
        )
        if proc.returncode == 0:
            _cloak_kernel_ready = True
            logger.info("CloakBrowser 内核已就绪（浏览器优先使用 CloakBrowser）")
        else:
            tail = ((proc.stderr or proc.stdout) or "")[-300:]
            logger.warning("CloakBrowser 内核下载失败，浏览器将回退 Playwright：%s", tail)
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
    _open_context_sync 里按需补下，保证浏览器最终可用。"""
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
# 上下文启动（同步）：优先 cloakbrowser，回退 playwright
# ──────────────────────────────────────────────
def _open_context_sync(headless: bool, user_agent: Optional[str], proxy: Optional[Any]):
    """返回 (engine, context, closers)。closers 逆序调用以彻底释放资源。
    优先 CloakBrowser，失败回退 Playwright；两者的内核都不在镜像里，缺失时按需下载。"""
    # 优先 CloakBrowser（内核就绪或已可导入即尝试）
    if _cloak_importable():
        try:
            from cloakbrowser import launch_context
            kw: dict[str, Any] = {"headless": headless}
            if user_agent:
                kw["user_agent"] = user_agent
            if proxy:
                kw["proxy"] = proxy
            ctx = launch_context(**kw)
            return "cloakbrowser", ctx, [ctx.close]
        except Exception as e:  # noqa: BLE001 - 内核未下载/启动失败则回退
            logger.debug("CloakBrowser 启动失败，回退 Playwright：%r", e)

    # 回退 Playwright。镜像不内置 chromium 二进制，若尚未下载则当场补下一次再启动。
    if not _playwright_importable():
        raise RuntimeError("浏览器不可用：CloakBrowser 未就绪且 Playwright 未安装")

    def _launch_playwright():
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            launch_kw: dict[str, Any] = {"headless": headless}
            if proxy:
                launch_kw["proxy"] = {"server": proxy} if isinstance(proxy, str) else proxy
            browser = pw.chromium.launch(**launch_kw)
        except Exception:
            pw.stop()
            raise
        ctx_kw: dict[str, Any] = {}
        if user_agent:
            ctx_kw["user_agent"] = user_agent
        context = browser.new_context(**ctx_kw)
        return "playwright", context, [context.close, browser.close, pw.stop]

    try:
        return _launch_playwright()
    except Exception as e:  # noqa: BLE001 - 多半是 chromium 二进制未下载
        if _pw_chromium_ready:
            raise
        logger.info("Playwright chromium 未就绪，正在按需下载后重试：%r", e)
        _ensure_playwright_chromium_sync()
        return _launch_playwright()


def _with_page_sync(url: str, action: Callable[[Any], Any], *,
                    cookies: Optional[str], user_agent: Optional[str],
                    headless: bool, timeout: int, proxy: Optional[Any]) -> Any:
    """启动上下文 → 打开页面 → 导航 → 执行 action(page) → 关闭。全在调用线程里同步跑。"""
    _ensure_browser_once_sync()   # 懒加载：首次使用时才准备内核
    engine, ctx, closers = _open_context_sync(headless, user_agent, proxy)
    try:
        page = ctx.new_page()
        if hasattr(page, "set_default_timeout"):
            page.set_default_timeout(int(timeout) * 1000)
        if cookies:
            page.set_extra_http_headers({"cookie": cookies})
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout) * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=min(int(timeout), 15) * 1000)
        except Exception:  # noqa: BLE001 - networkidle 超时不算失败
            pass
        return action(page)
    finally:
        for close in reversed(closers):
            try:
                close()
            except Exception:  # noqa: BLE001 - 释放尽量不抛
                pass


class BrowserHelper:
    """插件用的浏览器封装（ctx.browser）。无状态：每次调用起一个独立上下文用完即关。"""

    @property
    def engine(self) -> Optional[str]:
        """已就绪的引擎名（"cloakbrowser" / "playwright"）。
        懒加载：插件还没用过浏览器（内核未下载）时返回 None。"""
        if _cloak_kernel_ready:
            return "cloakbrowser"
        if _pw_chromium_ready:
            return "playwright"
        return None

    async def page_source(self, url: str, *, cookies: Optional[str] = None,
                          ua: Optional[str] = None, headless: bool = True,
                          timeout: int = 60, proxy: Optional[Any] = None) -> str:
        """打开 url 并返回渲染后的 HTML 源码。"""
        return await asyncio.to_thread(
            _with_page_sync, url, lambda p: p.content(),
            cookies=cookies, user_agent=ua, headless=headless, timeout=timeout, proxy=proxy,
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
        return await asyncio.to_thread(
            _with_page_sync, url, action,
            cookies=cookies, user_agent=ua, headless=headless, timeout=timeout, proxy=proxy,
        )


# 平台级单例（无状态，可安全共享）
browser = BrowserHelper()
