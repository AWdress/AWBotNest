"""
main.py
AWBotNest 平台入口。

启动顺序：
  1. 兼容性修复（Python 3.13 事件循环、配置文件自检）
  2. 启动账号（AccountManager）
  3. 恢复已启用插件（PluginRuntime）
  4. 启动 Web UI
  5. idle 等待
"""
# ── 前置：配置文件自检（必须在导入业务模块前）──
import sys
import asyncio
import os
import json
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(_PROJECT_ROOT)
_instance_lock_file = None


def _acquire_instance_lock() -> None:
    """同一份 data 目录只允许一个平台进程运行。"""
    global _instance_lock_file
    lock_path = _PROJECT_ROOT / "data" / "awbotnest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        owner = handle.read().strip()
    except OSError:
        # Windows 对已锁定的首字节连读取也会拒绝；不影响后续非阻塞加锁判断。
        owner = ""

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if lock_path.stat().st_size == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        detail = f"（{owner}）" if owner else ""
        raise RuntimeError(
            f"检测到另一套 AWBotNest 正在运行{detail}，本次启动已停止。"
            "请不要同时使用 uv、python、systemd 或多个容器重复启动。"
        ) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(f"PID {os.getpid()}，启动命令：{' '.join(sys.argv)}")
    handle.flush()
    _instance_lock_file = handle


def _web_port_available(host: str, port: int) -> bool:
    """检查 Web 端口当前是否可以监听。"""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False


async def _wait_for_web_port(
    host: str,
    port: int,
    timeout: float = 120,
    interval: float = 2,
) -> None:
    """等待旧进程释放 Web 端口，防止短暂占用导致重启失败。"""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + max(0, timeout)
    next_notice = started

    while not _web_port_available(host, port):
        now = loop.time()
        remaining = deadline - now
        if remaining <= 0:
            raise RuntimeError(
                f"Web 端口 {port} 等待 {int(timeout)} 秒后仍被占用，AWBotNest 已停止启动。"
                "请确认旧的 AWBotNest 进程已经退出，或在系统设置中修改端口。"
            )
        if now >= next_notice:
            logger.warning(
                "Web 端口 %s 暂时被占用，等待旧进程退出后自动重试（最多再等 %s 秒）",
                port,
                max(1, int(remaining)),
            )
            next_notice = now + 10
        await asyncio.sleep(min(max(0.1, interval), remaining))

    waited = loop.time() - started
    if waited >= 0.1:
        logger.info("Web 端口 %s 已释放，AWBotNest 继续启动", port)


if __name__ == "__main__":
    _acquire_instance_lock()

# A restore is applied before config, databases, Telegram sessions, or plugins are opened.
try:
    from webui.backup import apply_pending_restore as _apply_pending_restore
    _restored_files = _apply_pending_restore()
    if _restored_files:
        print(f"[restore] 备份恢复完成，共恢复 {_restored_files} 个文件")
except Exception as _restore_error:  # noqa: BLE001 - keep the rolled-back platform bootable
    print(f"[restore] 待恢复备份应用失败，已保留原数据: {_restore_error}")

_base = str(_PROJECT_ROOT)
os.makedirs(os.path.join(_base, "data"), exist_ok=True)

# 插件运行时依赖目录（pip --target 装到这里，随 data/ 卷持久化，容器重建不丢）。
# 在导入任何业务模块前就挂到 sys.path，保证启动早期 import 也能用上已持久化的包。
_plugin_deps = os.path.join(_base, "data", "plugin_deps")
os.makedirs(_plugin_deps, exist_ok=True)
if _plugin_deps not in sys.path:
    sys.path.append(_plugin_deps)

# 浏览器内核缓存（供插件 ctx.browser 用）。镜像不烤浏览器二进制，内核由平台启动时
# 下载到此目录（随 data/ 卷持久化，容器重建不必重下）。
# - Playwright chromium：装到 PLAYWRIGHT_BROWSERS_PATH（指到卷内 ms-playwright 子目录）。
# - CloakBrowser 内核：放 ~/.cloakbrowser，故把 HOME 指到 data/browser_cache（仅容器内）。
_browser_cache = os.path.join(_base, "data", "browser_cache")
os.makedirs(_browser_cache, exist_ok=True)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(_browser_cache, "ms-playwright"))
if sys.platform != "win32":
    os.environ["HOME"] = _browser_cache

# 配置数据源是 data/config.json（data/ 是卷映射的运行时目录；config/ 只放代码）。
# 不存在则写一份空模板，平台仍能启动，用户在前端「设置」页填 API 凭据后重启即可。
_cfg_json = os.path.join(_base, "data", "config.json")
if not os.path.exists(_cfg_json) or os.path.getsize(_cfg_json) == 0:
    _tpl = {
        "API_ID": 0, "API_HASH": "", "BOT_TOKEN": "", "BOT_NAME": "主要通知渠道",
        "DEFAULT_BOT_ID": "default", "BOTS": [], "ACCOUNTS": [],
        "WEB_UI_URL": "", "WEB_UI_PORT": 18001,
        "proxy_set": {"proxy_enable": False,
                       "proxy": {"scheme": "http", "hostname": "127.0.0.1", "port": 7890, "username": "", "password": ""},
                       "PROXY_URL": ""},
        "DB_INFO": {"dbset": "SQLite", "address": "127.0.0.1", "db_name": "tgbot", "port": 3306, "user": "", "password": ""},
    }
    with open(_cfg_json, "w", encoding="utf-8") as _f:
        json.dump(_tpl, _f, ensure_ascii=False, indent=2)

# 平台代理导出为环境变量：让 httpx/requests 请求自动走系统设置的代理。
# aiohttp 默认不读取环境代理；平台内置网络链路会另外显式传递代理。
# 必须在导入业务模块 / 启动插件前执行，保证启动早期的出站请求也走代理。
try:
    from libs.proxy import display_proxy_url as _display_proxy_url
    from libs.proxy import export_env as _export_proxy_env
    _px = _export_proxy_env()
    if _px:
        print(f"[proxy] 代理已启用，出站请求将走 {_display_proxy_url(_px)}")
except Exception as _e:  # noqa: BLE001 - 代理导出失败不应阻断启动
    print(f"[proxy] 导出代理环境变量失败: {_e!r}")

# Python 3.13+ 事件循环策略
if sys.version_info >= (3, 13):
    try:
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    except Exception:
        pass
elif sys.platform != "win32":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass

# ── 业务导入 ──
from core import logger, config, manager
from kernel import AccountManager, PluginRuntime
from kernel import state as kernel_state
from schedulers import scheduler, start_scheduler

# 全局内核实例（供 Web UI 引用）
# 启动时赋值，启动前为 None
accounts: AccountManager | None = None
runtime: PluginRuntime | None = None


async def start_platform() -> None:
    """平台主启动流程"""
    global accounts, runtime

    accounts = AccountManager()
    runtime = PluginRuntime(accounts)

    # 注入内核单例到共享模块，供 Web UI 跨模块读取
    kernel_state.set_kernel(accounts, runtime)

    # 1) 启动账号
    await accounts.start_all()

    # 2) 调度器
    scheduler.start()
    await start_scheduler()

    # 3) 恢复已启用插件
    await runtime.restore_enabled()

    # 4) 插件仓库轮询（强制常开）：注册定时任务并立即刷新一次市场 + 检查已装更新
    try:
        from webui import repo_sync
        repo_sync.reschedule()
        await repo_sync.sync_once()
    except Exception as e:  # noqa: BLE001 - 同步失败不影响平台启动
        logger.error("插件仓库轮询初始化失败: %r", e)

    # 浏览器内核不在启动时预热：改为懒加载——插件首次真正调用 ctx.browser 时才
    # 下载内核（见 kernel/browser.py）。不用浏览器的部署零开销、零额外磁盘占用。

    logger.info("AWBotNest 启动完成")

    # 5) idle 等待
    from core import idle
    try:
        await idle()
    finally:
        logger.debug("AWBotNest 关闭中...")
        await runtime.shutdown()
        await accounts.stop_all()
        logger.info("AWBotNest 已关闭")


async def main() -> None:
    from webui.api import start_web_ui

    await _wait_for_web_port("0.0.0.0", config.telegram.web_ui_port)

    # 用 task + FIRST_EXCEPTION：start_platform 末尾 idle() 永不返回，
    # 若 web_ui 崩溃，必须立即感知并退出，而非被 gather 卡死等不到的 idle。
    platform_task = asyncio.create_task(start_platform())
    web_task = asyncio.create_task(start_web_ui(port=config.telegram.web_ui_port))
    tasks = [platform_task, web_task]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        # 取消尚未结束的任务（如崩溃时仍在 idle 的 platform）
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # 任一任务抛异常则上报
        for t in done:
            exc = t.exception()
            if exc is not None:
                logger.error("后台任务异常: %r", exc)
                raise exc
    except KeyboardInterrupt:
        logger.warning("程序被用户中断")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
