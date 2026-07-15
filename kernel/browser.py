"""
kernel/browser.py
平台级浏览器自动化能力，供插件通过 ctx.browser 使用（无需自己装浏览器）。

引擎选择（参考 MoviePilot 的做法）：
- 优先 CloakBrowser（停用 Chromium，过 Cloudflare/指纹检测），是 Playwright 的 drop-in 替代。
- CloakBrowser 不可用（未装成/内核未下载）时自动回退平台内置的 Playwright Chromium。

安装策略：
- Playwright 及其 chromium 在镜像构建期装好（见 Dockerfile），永远可用。
- CloakBrowser 体积大且可能需联网拉专有内核，故在**平台启动时后台**按需 pip 安装 +
  `python -m cloakbrowser install` 下载内核到 data/browser_cache（HOME 指过去，随卷持久化），
  失败只回退 Playwright，不阻断启动。

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
from pathlib import Path
from typing import Any, Callable, Optional

from libs.log import logger

# 浏览器内核缓存目录（随 data/ 卷持久化；main.py 已把 HOME 指到这里，
# 故 cloakbrowser 的 ~/.cloakbrowser 实际落在此目录下，容器重建不必重下）。
BROWSER_CACHE_DIR = Path(os.getcwd()) / "data" / "browser_cache"

_cloak_kernel_ready = False   # cloakbrowser 内核是否已下载就绪


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

    # 2) 下载 CloakBrowser 内核（等价 MoviePilot 的 `python -m cloakbrowser install`）。
    #    HOME 已在 main.py 指向 data/browser_cache，内核落在卷内、可持久化。
    try:
        env = dict(os.environ)
        from libs.proxy import proxy_url
        px = proxy_url()
        if px:
            env.setdefault("HTTPS_PROXY", px)
            env.setdefault("https_proxy", px)
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


async def ensure_cloakbrowser() -> None:
    """平台启动时后台调用：在线程里预热 CloakBrowser，不阻塞主启动流程。"""
    await asyncio.to_thread(_ensure_cloakbrowser_sync)


# ──────────────────────────────────────────────
# 上下文启动（同步）：优先 cloakbrowser，回退 playwright
# ──────────────────────────────────────────────
def _open_context_sync(headless: bool, user_agent: Optional[str], proxy: Optional[Any]):
    """返回 (engine, context, closers)。closers 逆序调用以彻底释放资源。"""
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

    # 回退 Playwright（镜像内置）
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    launch_kw: dict[str, Any] = {"headless": headless}
    if proxy:
        launch_kw["proxy"] = {"server": proxy} if isinstance(proxy, str) else proxy
    browser = pw.chromium.launch(**launch_kw)
    ctx_kw: dict[str, Any] = {}
    if user_agent:
        ctx_kw["user_agent"] = user_agent
    context = browser.new_context(**ctx_kw)
    return "playwright", context, [context.close, browser.close, pw.stop]


def _with_page_sync(url: str, action: Callable[[Any], Any], *,
                    cookies: Optional[str], user_agent: Optional[str],
                    headless: bool, timeout: int, proxy: Optional[Any]) -> Any:
    """启动上下文 → 打开页面 → 导航 → 执行 action(page) → 关闭。全在调用线程里同步跑。"""
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
        """当前可用引擎名，供插件判断/记录。"""
        if _cloak_importable():
            return "cloakbrowser"
        if _playwright_importable():
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
