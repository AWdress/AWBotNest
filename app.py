"""
app.py
兼容垫片（compat shim）。

旧项目的入口模块是 app.py，提供 get_bot_app / get_user_app / get_user_apps /
scheduler / logger 等全局访问点；旧业务代码（libs/、plugins/bot/ 等）大量
`from app import ...`。

平台化后入口是 main.py，账号实例由 kernel.state 持有。本垫片把旧的访问点
重新导出，指向新内核，使旧业务代码零改动可用。
"""
from __future__ import annotations

from typing import Optional

from libs.log import logger  # noqa: F401  旧代码 from app import logger
from libs.custom_client import Client  # noqa: F401  旧代码 from app import Client
from schedulers import scheduler  # noqa: F401  旧代码 from app import scheduler


def get_bot_app():
    """返回 Bot 账号客户端（来自内核）"""
    from kernel import state as ks
    if ks.accounts is not None and ks.accounts.bot_app is not None:
        return ks.accounts.bot_app
    # 兜底：旧 manager（DI 容器/部分服务仍引用）
    from core import manager
    return manager.bot_app


def get_user_app():
    """返回主用户账号（第一个已连接）"""
    from kernel import state as ks
    if ks.accounts is not None:
        return ks.accounts.primary_user_app
    from core import manager
    return manager.user_app


def get_user_apps() -> list:
    """返回所有已连接的用户账号"""
    from kernel import state as ks
    if ks.accounts is not None:
        return ks.accounts.connected_user_apps
    from core import manager
    return [a for a in manager.user_apps if a and a.is_connected]


async def system_version_get():
    """兼容旧 login/启动逻辑对版本信息的调用（尽量复用旧实现）"""
    try:
        from libs.sys_info import system_version_get as _impl
        return await _impl()
    except Exception:  # noqa: BLE001
        return "AWBotNest", ""
