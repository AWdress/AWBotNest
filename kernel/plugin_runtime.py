"""
kernel/plugin_runtime.py
插件运行时：动态加载/卸载单文件插件，实现「真热插拔」。

加载流程：
  importlib 导入 plugins/<id>.py → 校验 setup → 构建 PlatformContext
  → await setup(ctx) → 句柄登记在 ctx 内 → 标记 loaded

卸载流程：
  ctx._unregister_all() 注销所有 handler / 清理定时任务
  → await teardown(ctx)（若有） → 从 sys.modules 移除模块

容错：单个插件加载失败只标记该插件 error，不影响内核与其它插件。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from libs.log import logger
from kernel.context import PlatformContext
from kernel.registry import registry, PluginMeta
from kernel import deps

if TYPE_CHECKING:
    from kernel.account_manager import AccountManager

PLUGINS_DIR = Path("plugins")
# 动态导入时使用的模块名前缀，避免与真实包冲突
_MODULE_PREFIX = "awbotnest_plugin_"
# group 基址分配：第一个插件从 1000 起，每个插件间隔 1000。
# group 0 留给"未分配/平台内置"，负数区间留给需要抢在所有插件之前的特殊场景。
_GROUP_BASE_START = 1000
_GROUP_BASE_STEP = 1000


class LoadedPlugin:
    """一个已加载插件的运行时状态"""

    def __init__(self, plugin_id: str, module: object, ctx: PlatformContext):
        self.id = plugin_id
        self.module = module
        self.ctx = ctx


class PluginRuntime:
    """插件运行时管理器"""

    def __init__(self, accounts: "AccountManager"):
        self._accounts = accounts
        self._loaded: dict[str, LoadedPlugin] = {}
        self._lock = asyncio.Lock()
        # 每插件分配一个唯一的 group 基址，插件内相对 group 会平移到该区间，
        # 使不同插件的 handler 落在各自独立的 group 段，互不"吃消息"。
        # 步长 1000 给插件内部留足相对偏移空间（建议相对 group 控制在 ±500 内）。
        self._group_bases: dict[str, int] = {}
        self._next_group_base = _GROUP_BASE_START

    def _group_base_for(self, plugin_id: str) -> int:
        """取得插件的 group 基址；首次访问时分配，重载/重挂保持稳定。"""
        base = self._group_bases.get(plugin_id)
        if base is None:
            base = self._next_group_base
            self._group_bases[plugin_id] = base
            self._next_group_base += _GROUP_BASE_STEP
        return base

    @property
    def loaded_ids(self) -> list[str]:
        return list(self._loaded.keys())

    def is_loaded(self, plugin_id: str) -> bool:
        return plugin_id in self._loaded

    # ──────────────────────────────────────────────
    # 加载（启用）
    # ──────────────────────────────────────────────
    async def enable(self, plugin_id: str) -> PluginMeta:
        """启用插件（对外，自带锁）。"""
        async with self._lock:
            return await self._enable_locked(plugin_id)

    async def _enable_locked(self, plugin_id: str, ensure_deps: bool = True) -> PluginMeta:
        """启用插件内部实现：调用方须已持有 self._lock。
        导入文件 → setup → 登记；幂等：已加载则直接返回。
        ensure_deps=False 时跳过依赖检查/安装（用于 resync 重挂——依赖在首次 enable 已就绪）。"""
        if True:
            meta = registry.get_meta(plugin_id)
            if meta is None:
                raise FileNotFoundError(f"插件不存在: {plugin_id}")
            if meta.error:
                # 元数据有静态错误：不加载，但不抹掉用户的启用意图
                # （保持 enabled 持久值，修好后重启/重载自动恢复，UI 显示错误徽章）
                meta.enabled = registry.is_enabled(plugin_id)
                return meta
            if plugin_id in self._loaded:
                meta.loaded = True
                meta.enabled = True
                return meta

            ctx = None
            try:
                # 启用前确保第三方依赖就绪：缺失则代装，版本冲突则拒绝启用。
                # 单进程同一个包只能有一个版本，冲突只能挡在加载前，不能强行覆盖。
                if ensure_deps and meta.requirements:
                    dep = await deps.ensure(plugin_id, meta.requirements)
                    if not dep["ok"]:
                        meta.loaded = False
                        meta.enabled = registry.is_enabled(plugin_id)
                        meta.error = dep["error"]
                        return meta

                module = self._import_module(plugin_id)
                setup = getattr(module, "setup", None)
                if setup is None or not callable(setup):
                    raise AttributeError("插件缺少 async def setup(ctx) 函数")

                ctx = PlatformContext(
                    plugin_id, self._accounts, registry,
                    group_base=self._group_base_for(plugin_id),
                )
                # setup 可以是 async 或 sync
                result = setup(ctx)
                if asyncio.iscoroutine(result):
                    await result

                self._loaded[plugin_id] = LoadedPlugin(plugin_id, module, ctx)
                registry.set_enabled(plugin_id, True)
                meta.loaded = True
                meta.enabled = True
                meta.error = None
                logger.info("插件已启用: %s", plugin_id)
                return meta
            except Exception as e:  # noqa: BLE001
                # 加载失败：先注销 setup 中途已注册的 handler/定时任务（防句柄泄漏），
                # 再清理模块，标记错误，不影响其它插件。
                logger.exception("插件启用失败: %s", plugin_id)
                if ctx is not None:
                    try:
                        ctx._unregister_all()
                    except Exception as ce:  # noqa: BLE001
                        logger.warning("清理失败插件句柄异常 [%s]: %r", plugin_id, ce)
                self._cleanup_module(plugin_id)
                # 不持久化 enabled=False：加载失败属运行态问题，不应抹掉用户"要启用"的意图。
                # 保留持久启用状态，下次重启/重载自动重试；UI 显示错误徽章。
                # 仅"显式点停用"(disable) 才会真正关闭。
                meta.loaded = False
                meta.enabled = registry.is_enabled(plugin_id)
                meta.error = f"{e.__class__.__name__}: {e}"
                return meta

    # ──────────────────────────────────────────────
    # 卸载（停用）
    # ──────────────────────────────────────────────
    async def disable(self, plugin_id: str) -> PluginMeta:
        """停用插件（对外，自带锁）。"""
        async with self._lock:
            return await self._disable_locked(plugin_id)

    async def _disable_locked(self, plugin_id: str, persist: bool = True) -> PluginMeta:
        """停用插件内部实现：调用方须已持有 self._lock。
        注销所有 handler → teardown → 卸载模块；幂等。
        persist=True 时把 enabled 持久化为 False（用户显式停用）；
        persist=False 仅运行态卸载，不动持久启用意图（进程退出/重挂场景）。"""
        if True:
            loaded = self._loaded.pop(plugin_id, None)
            if loaded is not None:
                # 1) 注销所有 handler / 定时任务
                loaded.ctx._unregister_all()
                # 2) teardown（可选）
                teardown = getattr(loaded.module, "teardown", None)
                if callable(teardown):
                    try:
                        result = teardown(loaded.ctx)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:  # noqa: BLE001
                        logger.warning("插件 teardown 异常 [%s]: %r", plugin_id, e)
                # 3) 卸载模块
                self._cleanup_module(plugin_id)
                logger.info("插件已停用: %s", plugin_id)

            if persist:
                registry.set_enabled(plugin_id, False)
            meta = registry.get_meta(plugin_id) or PluginMeta(id=plugin_id, name=plugin_id)
            meta.loaded = False
            meta.enabled = registry.is_enabled(plugin_id)
            return meta

    # ──────────────────────────────────────────────
    # 重载（改了插件文件后）
    # ──────────────────────────────────────────────
    async def reload(self, plugin_id: str) -> PluginMeta:
        """先停用再启用，用于插件文件更新后刷新。整体持锁，保证原子。"""
        async with self._lock:
            # persist=False：reload 不是用户要停用，卸载不动持久启用意图
            await self._disable_locked(plugin_id, persist=False)
            meta = await self._enable_locked(plugin_id)
            return meta

    # ──────────────────────────────────────────────
    # 启动时按持久化状态恢复
    # ──────────────────────────────────────────────
    async def restore_enabled(self) -> None:
        """根据 registry 中记录的启用状态，恢复所有应启用的插件"""
        metas = registry.scan()
        for meta in metas:
            if meta.enabled and not meta.error:
                await self.enable(meta.id)
        logger.info("插件恢复完成，已加载 %d 个", len(self._loaded))

    async def shutdown(self) -> None:
        """停用所有已加载插件（进程退出时调用）。
        仅运行态卸载，绝不持久化 enabled=False——否则重启/更新镜像后插件会全部变未启用。"""
        for plugin_id in list(self._loaded.keys()):
            async with self._lock:
                await self._disable_locked(plugin_id, persist=False)

    async def resync(self) -> None:
        """
        账号连接状态变化后（如新账号登录/上线/下线），重新挂载所有已加载插件的处理器。

        原因：插件的 handler 在 enable 时挂到「当时已连接」的 client 上。新账号登录后，
        这些 handler 不在新 client 上。最简单可靠的做法是把已加载插件全部重挂一遍：
        disable 会从所有旧 client 注销，enable 会按「当前已连接」的 client 重新注册。
        """
        async with self._lock:
            ids = list(self._loaded.keys())
            if not ids:
                return
            logger.info("账号状态变化，重新挂载 %d 个插件...", len(ids))
            for plugin_id in ids:
                # persist=False：重挂只是运行态操作，不动持久启用意图
                # ensure_deps=False：依赖在首次 enable 已就绪，重挂不必重跑 pip（否则账号每次上下线都触发）
                await self._disable_locked(plugin_id, persist=False)
                await self._enable_locked(plugin_id, ensure_deps=False)
                if plugin_id not in self._loaded:
                    logger.warning("插件 [%s] 重挂失败，启用意图保留待重试", plugin_id)

    # ──────────────────────────────────────────────
    # 内部
    # ──────────────────────────────────────────────
    def _import_module(self, plugin_id: str):
        """
        动态导入插件模块（每次新建，确保拿到最新代码）。支持两种形态：
          - 单文件：plugins/<id>.py
          - 文件夹：plugins/<id>/__init__.py（作为包导入，folder 内可相对/绝对引用）
        """
        single = PLUGINS_DIR / f"{plugin_id}.py"
        pkg_init = PLUGINS_DIR / plugin_id / "__init__.py"

        mod_name = f"{_MODULE_PREFIX}{plugin_id}"
        # 移除旧模块缓存（含子模块），保证重载拿到新代码
        for name in list(sys.modules):
            if name == mod_name or name.startswith(mod_name + "."):
                sys.modules.pop(name, None)

        if single.exists():
            spec = importlib.util.spec_from_file_location(mod_name, single)
        elif pkg_init.exists():
            # 作为包导入：submodule_search_locations 指向插件目录
            spec = importlib.util.spec_from_file_location(
                mod_name, pkg_init,
                submodule_search_locations=[str((PLUGINS_DIR / plugin_id).resolve())],
            )
        else:
            raise FileNotFoundError(f"插件不存在: {plugin_id}（既无 {plugin_id}.py 也无 {plugin_id}/__init__.py）")

        if spec is None or spec.loader is None:
            raise ImportError(f"无法为插件创建模块规格: {plugin_id}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    def _cleanup_module(self, plugin_id: str) -> None:
        """从 sys.modules 移除插件模块（含文件夹形态的子模块）"""
        mod_name = f"{_MODULE_PREFIX}{plugin_id}"
        for name in list(sys.modules):
            if name == mod_name or name.startswith(mod_name + "."):
                sys.modules.pop(name, None)
