"""
kernel/__init__.py
AWBotNest 平台内核统一出口。

内核只提供能力，不含业务逻辑。业务一律以插件形式存在于 plugins/ 目录。
对外导出：
    from kernel import AccountManager, PluginRuntime, PlatformContext, registry
"""
from __future__ import annotations

from kernel.registry import PluginRegistry, PluginMeta, registry
from kernel.context import PlatformContext
from kernel.account_manager import AccountManager
from kernel.plugin_runtime import PluginRuntime

__all__ = [
    "AccountManager",
    "PluginRuntime",
    "PlatformContext",
    "PluginRegistry",
    "PluginMeta",
    "registry",
]
