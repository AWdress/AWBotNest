"""
main.py
AWBotNest 平台入口。

启动顺序：
  1. 兼容性修复（Python 3.13 事件循环、配置文件自检）
  2. 启动账号（AccountManager）
  3. 初始化数据库 + DI 容器（复用旧底座）
  4. 恢复已启用插件（PluginRuntime）
  5. 启动 Web UI
  6. idle 等待
"""
# ── 前置：配置文件自检（必须在导入业务模块前）──
import sys
import asyncio
import os
import json
from pathlib import Path

_base = os.getcwd()
os.makedirs(os.path.join(_base, "data"), exist_ok=True)

# 插件运行时依赖目录（pip --target 装到这里，随 data/ 卷持久化，容器重建不丢）。
# 在导入任何业务模块前就挂到 sys.path，保证启动早期 import 也能用上已持久化的包。
_plugin_deps = os.path.join(_base, "data", "plugin_deps")
os.makedirs(_plugin_deps, exist_ok=True)
if _plugin_deps not in sys.path:
    sys.path.append(_plugin_deps)

# 配置数据源是 data/config.json（data/ 是卷映射的运行时目录；config/ 只放代码）。
# 不存在则写一份空模板，平台仍能启动，用户在前端「设置」页填 API 凭据后重启即可。
_cfg_json = os.path.join(_base, "data", "config.json")
if not os.path.exists(_cfg_json) or os.path.getsize(_cfg_json) == 0:
    _tpl = {
        "API_ID": 0, "API_HASH": "", "BOT_TOKEN": "", "BOTS": [], "ACCOUNTS": [],
        "WEB_UI_URL": "", "WEB_UI_PORT": 18001, "NGROK_ENABLE": False, "NGROK_TOKEN": "",
        "proxy_set": {"proxy_enable": False,
                       "proxy": {"scheme": "http", "hostname": "127.0.0.1", "port": 7890, "username": "", "password": ""},
                       "PROXY_URL": ""},
        "DB_INFO": {"dbset": "SQLite", "address": "127.0.0.1", "db_name": "tgbot", "port": 3306, "user": "", "password": ""},
    }
    with open(_cfg_json, "w", encoding="utf-8") as _f:
        json.dump(_tpl, _f, ensure_ascii=False, indent=2)

# 平台代理导出为环境变量：让所有插件的 httpx/requests/aiohttp 自动走系统设置的代理。
# 必须在导入业务模块 / 启动插件前执行，保证启动早期的出站请求也走代理。
try:
    from libs.proxy import export_env as _export_proxy_env
    _px = _export_proxy_env()
    if _px:
        print(f"[proxy] 平台代理已启用，插件出站请求将走 {_px}")
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
from models import create_all, async_engine
from schedulers import scheduler, start_scheduler

# 全局内核实例（供 Web UI 引用）
accounts: AccountManager = None
runtime: PluginRuntime = None


async def _init_database() -> None:
    """初始化数据库（幂等）"""
    import json
    db_flag_path = Path("db_file/dbflag/dbflag.json")
    db_flag_path.parent.mkdir(parents=True, exist_ok=True)

    db_flag_data = None
    if db_flag_path.exists():
        try:
            db_flag_data = json.loads(db_flag_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取 dbflag.json 失败，将重新初始化：%s", e)

    if not db_flag_data or db_flag_data.get("db_flag") is not True:
        logger.debug("首次运行，初始化数据库...")
        await create_all()
        db_flag_path.write_text(
            json.dumps({"db_flag": True, "alter_tables": False}, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
    # 兼容升级：幂等确保新表创建
    await create_all()


async def _init_container() -> None:
    """初始化 DI 容器（复用旧底座）"""
    from infra.container import build_container
    from infra.config import get_settings
    from libs.state import state_manager as _state_manager
    from models import async_session_maker as _session_maker
    import infra.container as _infra_container

    _container = build_container(
        user_client=accounts.primary_user_app,
        bot_client=accounts.bot_app,
        state_manager=_state_manager,
        session_maker=_session_maker,
        settings=get_settings(),
    )
    _infra_container._container_instance = _container
    logger.info("DI 容器初始化完成")


async def start_platform() -> None:
    """平台主启动流程"""
    global accounts, runtime

    accounts = AccountManager()
    runtime = PluginRuntime(accounts)

    # 注入内核单例到共享模块，供 Web UI 跨模块读取
    kernel_state.set_kernel(accounts, runtime)

    # 1) 启动账号
    await accounts.start_all()

    # 2) 数据库 + DI 容器
    await _init_database()
    await _init_container()

    # 3) 调度器
    scheduler.start()
    await start_scheduler()

    # 4) 恢复已启用插件
    await runtime.restore_enabled()

    # 5) 插件仓库轮询（强制常开）：注册定时任务并立即刷新一次市场 + 检查已装更新
    try:
        from webui import repo_sync
        repo_sync.reschedule()
        await repo_sync.sync_once()
    except Exception as e:  # noqa: BLE001 - 同步失败不影响平台启动
        logger.error("插件仓库轮询初始化失败: %r", e)

    logger.info("AWBotNest 平台启动完成")

    # 6) idle 等待
    from core import idle
    try:
        await idle()
    finally:
        logger.debug("平台关闭中...")
        await runtime.shutdown()
        await accounts.stop_all()
        await async_engine.dispose()
        logger.info("平台已关闭")


async def main() -> None:
    from webui.api import start_web_ui

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
