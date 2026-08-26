"""
kernel/plugin_runtime.py
插件运行时：动态加载/卸载单文件插件，实现「真热插拔」。

加载流程：
  importlib 导入 plugins/<id>.py → 校验 setup → 构建 PlatformContext
  → await setup(ctx) → 句柄登记在 ctx 内 → 标记 loaded

卸载流程：
  await ctx.aclose() 注销 handler / 清理定时任务并等待后台任务退出
  → await teardown(ctx)（若有） → 从 sys.modules 移除模块

容错：单个插件加载失败只标记该插件 error，不影响内核与其它插件。
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from packaging.version import InvalidVersion, Version

from libs.log import logger
from kernel.context import PlatformContext
from kernel.registry import registry, PluginMeta
from kernel import deps
from kernel.plugin_governance import governor

if TYPE_CHECKING:
    from kernel.account_manager import AccountManager

PLUGINS_DIR = Path("plugins")
# 动态导入时使用的模块名前缀，避免与真实包冲突
_MODULE_PREFIX = "awbotnest_plugin_"
# group 基址分配：第一个插件从 1000 起，每个插件间隔 1000。
# group 0 留给"未分配/平台内置"，负数区间留给需要抢在所有插件之前的特殊场景。
_GROUP_BASE_START = 1000
_GROUP_BASE_STEP = 1000
PLUGIN_API_VERSION = 1
_VERSION_FILE = Path("VERSION")


class LoadedPlugin:
    """一个已加载插件的运行时状态"""

    def __init__(self, plugin_id: str, module: object, contexts: list[PlatformContext]):
        self.id = plugin_id
        self.module = module
        self.contexts = contexts

    @property
    def ctx(self) -> PlatformContext:
        return self.contexts[0]


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

    def get_webhook_handler(self, plugin_id: str) -> Optional[object]:
        """取已加载插件注册的 webhook 处理器（ctx.on_webhook）。
        插件未加载或未注册处理器时返回 None。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            return None
        return next((handler for ctx in loaded.contexts
                     if (handler := getattr(ctx, "_webhook_handler", None)) is not None), None)

    def get_action_handler(self, plugin_id: str, action: str) -> Optional[object]:
        """取已加载插件注册的动作处理器（ctx.action(name)）。
        插件未加载或未注册该动作时返回 None。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            return None
        return next((handler for ctx in loaded.contexts
                     if (handler := getattr(ctx, "_action_handlers", {}).get(action)) is not None), None)

    def get_api_handler(self, plugin_id: str, method: str, path: str) -> Optional[object]:
        """取已加载插件注册的 API 处理器（ctx.on_api）。
        插件未加载或未注册该 (方法, 路径) 时返回 None。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            return None
        norm = "/" + str(path or "").strip().strip("/")
        return next((handler for ctx in loaded.contexts
                     if (handler := getattr(ctx, "_api_handlers", {}).get((str(method).upper(), norm))) is not None), None)

    @staticmethod
    def _platform_version() -> Version | None:
        try:
            return Version(_VERSION_FILE.read_text(encoding="utf-8").strip().lstrip("vV"))
        except (OSError, InvalidVersion):
            return None

    def _compatibility_error(self, meta: PluginMeta) -> str | None:
        if meta.plugin_api_version > PLUGIN_API_VERSION:
            return f"插件需要接口版本 {meta.plugin_api_version}，当前平台只支持 {PLUGIN_API_VERSION}"
        current = self._platform_version()
        try:
            if current and meta.min_platform_version and current < Version(meta.min_platform_version.lstrip("vV")):
                return f"插件要求平台不低于 {meta.min_platform_version}"
            if current and meta.max_platform_version and current > Version(meta.max_platform_version.lstrip("vV")):
                return f"插件只兼容到平台 {meta.max_platform_version}"
        except InvalidVersion:
            return "插件声明的平台兼容版本格式不正确"
        missing_plugins = [pid for pid in meta.requires_plugins if not self.is_loaded(pid)]
        if missing_plugins:
            return f"请先启用依赖插件：{', '.join(missing_plugins)}"
        available = set(governor.capabilities.names())
        missing_capabilities = [name for name in meta.requires_capabilities if name not in available]
        if missing_capabilities:
            return f"缺少平台能力：{', '.join(missing_capabilities)}"
        return None

    def dependency_graph(self) -> dict[str, object]:
        metas = registry.scan()
        nodes = []
        edges = []
        known = {meta.id for meta in metas}
        for meta in metas:
            nodes.append({
                "id": meta.id, "name": meta.name, "loaded": self.is_loaded(meta.id),
                "version": meta.version, "instance_mode": meta.instance_mode,
            })
            for dependency in meta.requires_plugins:
                edges.append({"from": meta.id, "to": dependency, "type": "plugin", "missing": dependency not in known})
            for requirement in meta.requirements:
                edges.append({
                    "from": meta.id, "to": requirement, "type": "python",
                    # Python 包的安装与版本校验由依赖管理器在启用插件时完成；这里仅展示声明。
                    "missing": False,
                })
            for capability in meta.requires_capabilities:
                edges.append({
                    "from": meta.id, "to": f"capability:{capability}", "type": "capability",
                    "missing": capability not in governor.capabilities.names(),
                })
            for capability in meta.provides_capabilities:
                edges.append({"from": meta.id, "to": f"capability:{capability}", "type": "provides", "missing": False})
        return {"nodes": nodes, "edges": edges, "capabilities": governor.capabilities.names()}

    def runtime_status(self, plugin_id: str) -> dict[str, object]:
        loaded = self._loaded.get(plugin_id)
        return {
            "loaded": loaded is not None,
            "instances": [
                {"id": ctx.instance_id, "account": ctx.account_name or "", "active": ctx._active}
                for ctx in (loaded.contexts if loaded else [])
            ],
            **governor.status(plugin_id),
        }

    async def self_check(self, plugin_id: str) -> dict[str, object]:
        async with self._lock:
            return await self._self_check_locked(plugin_id)

    async def _self_check_locked(self, plugin_id: str) -> dict[str, object]:
        """执行平台基础检查，并按需调用插件提供的 self_check(ctx)。"""
        meta = registry.get_meta(plugin_id)
        if meta is None:
            raise FileNotFoundError(f"插件不存在: {plugin_id}")
        loaded = self._loaded.get(plugin_id)
        enabled = registry.is_enabled(plugin_id)
        checks: list[dict[str, object]] = [
            {
                "id": "metadata", "name": "插件文件", "ok": not bool(meta.error),
                "detail": meta.error or "元数据和入口文件正常",
            },
            {
                "id": "runtime", "name": "运行状态", "ok": bool(loaded) if enabled else True,
                "detail": "已加载" if loaded else ("已停用" if not enabled else "启用后未成功加载"),
            },
        ]
        compatibility_error = self._compatibility_error(meta)
        checks.append({
            "id": "compatibility", "name": "版本与依赖",
            "ok": compatibility_error is None,
            "detail": compatibility_error or "平台版本、插件依赖和能力声明均满足",
        })
        config = registry.get_config(plugin_id)
        def missing_value(value: object) -> bool:
            return value in (None, [], {}) or (isinstance(value, str) and not value.strip())

        missing_config = [
            key for key, spec in (meta.config_schema or {}).items()
            if isinstance(spec, dict) and spec.get("required")
            and spec.get("type") not in {"info", "action"}
            and (
                not isinstance(spec.get("show_if"), dict)
                or all(config.get(cond_key) == cond_value
                       for cond_key, cond_value in spec["show_if"].items())
            )
            and missing_value(config.get(key))
        ]
        checks.append({
            "id": "config", "name": "插件配置", "ok": not missing_config,
            "detail": (
                f"缺少必填项：{', '.join(missing_config)}"
                if missing_config else "配置格式正常"
            ),
        })

        if loaded:
            ctx = loaded.ctx
            scope = str(meta.scope or "user")
            if scope in {"user", "both"}:
                user_names = {
                    str(getattr(app, "name", ""))
                    for current in loaded.contexts
                    for app in current._scoped_user_apps()
                    if getattr(app, "name", None)
                }
                user_count = len(user_names)
                checks.append({
                    "id": "accounts", "name": "用户账号", "ok": user_count > 0,
                    "detail": f"{user_count} 个可用账号" if user_count else "没有可用账号",
                })
            if scope in {"bot", "both"}:
                bot = ctx._chosen_bot()
                bot_ok = self._accounts.connection_ready(bot)
                checks.append({
                    "id": "bot", "name": "Bot 账号", "ok": bot_ok,
                    "detail": "已连接" if bot_ok else "没有可用 Bot",
                })
            from schedulers import scheduler

            job_count = sum(
                1 for job in scheduler.get_jobs()
                if str(job.id).startswith(f"{plugin_id}::")
            )
            checks.append({
                "id": "scheduler", "name": "定时任务", "ok": True,
                "detail": f"已注册 {job_count} 个任务" if job_count else "没有注册定时任务",
            })
            runtime_status = self.runtime_status(plugin_id)
            open_circuits = [item for item in runtime_status["circuits"] if item["open"]]
            checks.append({
                "id": "governance", "name": "运行保护", "ok": not open_circuits,
                "detail": (
                    f"{len(runtime_status['instances'])} 个实例，{runtime_status['background_tasks']} 个后台任务"
                    if not open_circuits else f"{len(open_circuits)} 项功能已熔断，正在自动恢复"
                ),
            })

            custom_check = getattr(loaded.module, "self_check", None)
            if callable(custom_check):
                for current in loaded.contexts:
                    suffix = f"（{current.account_name}）" if current.account_name else ""
                    try:
                        async def run_custom_check(check_ctx=current):
                            if inspect.iscoroutinefunction(custom_check):
                                return await custom_check(check_ctx)
                            value = await asyncio.to_thread(custom_check, check_ctx)
                            return await value if inspect.isawaitable(value) else value

                        result = await governor.execute(
                            plugin_id, f"self_check:{current.instance_id}", run_custom_check, timeout=15,
                        )
                        instance_checks = self._normalise_self_checks(result)
                        for item in instance_checks:
                            item["id"] = f"{item['id']}:{current.instance_id}"[:80]
                            item["name"] = f"{item['name']}{suffix}"[:80]
                        checks.extend(instance_checks)
                    except TimeoutError:
                        checks.append({
                            "id": f"plugin-check:{current.instance_id}"[:80],
                            "name": f"插件检查{suffix}", "ok": False,
                            "detail": "检查超过 15 秒，已停止等待",
                        })
                    except Exception as exc:  # noqa: BLE001
                        checks.append({
                            "id": f"plugin-check:{current.instance_id}"[:80],
                            "name": f"插件检查{suffix}", "ok": False,
                            "detail": f"{exc.__class__.__name__}: {exc}"[:300],
                        })

        return {
            "plugin_id": plugin_id,
            "plugin_name": meta.name,
            "ok": all(bool(item["ok"]) for item in checks),
            "checks": checks,
        }

    @staticmethod
    def _normalise_self_checks(result: object) -> list[dict[str, object]]:
        if result is None:
            return []
        values = result if isinstance(result, list) else [result]
        checks: list[dict[str, object]] = []
        for index, item in enumerate(values):
            if isinstance(item, bool):
                item = {"ok": item, "name": "插件检查"}
            if not isinstance(item, dict) or "ok" not in item:
                raise ValueError("self_check 必须返回含 ok 的字典或字典列表")
            checks.append({
                "id": str(item.get("id") or f"plugin-check-{index + 1}")[:80],
                "name": str(item.get("name") or "插件检查")[:80],
                "ok": bool(item["ok"]),
                "detail": str(item.get("detail") or ("正常" if item["ok"] else "检查未通过"))[:300],
            })
        return checks

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

            contexts: list[PlatformContext] = []
            try:
                compatibility_error = self._compatibility_error(meta)
                if compatibility_error:
                    meta.loaded = False
                    meta.enabled = registry.is_enabled(plugin_id)
                    meta.error = compatibility_error
                    return meta
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

                governor.configure(plugin_id, meta.resources)
                account_names: list[str | None] = [None]
                if meta.instance_mode == "account" and meta.scope in {"user", "both"}:
                    selected = set(registry.get_account_scope(plugin_id))
                    account_names = [
                        str(getattr(app, "name")) for app in self._accounts.connected_user_apps
                        if getattr(app, "name", None) and (not selected or getattr(app, "name", None) in selected)
                    ]
                    if not account_names:
                        raise RuntimeError("按账号运行的插件当前没有可用用户账号")

                for index, account_name in enumerate(account_names):
                    ctx = PlatformContext(
                        plugin_id, self._accounts, registry,
                        group_base=self._group_base_for(plugin_id) + index,
                        account_name=account_name,
                        primary_instance=index == 0,
                    )
                    contexts.append(ctx)
                    # setup 可以是 async 或 sync。account 模式下每个账号调用一次。
                    await governor.execute(
                        plugin_id, f"setup:{ctx.instance_id}", lambda current=ctx: setup(current),
                    )

                self._loaded[plugin_id] = LoadedPlugin(plugin_id, module, contexts)
                registry.set_enabled(plugin_id, True)
                meta.loaded = True
                meta.enabled = True
                meta.error = None
                governor.events.append(
                    plugin_id, "plugin_enabled", instances=[ctx.instance_id for ctx in contexts],
                    version=meta.version,
                )
                logger.info("插件已启用: %s（%d 个运行实例）", meta.name, len(contexts))
                return meta
            except Exception as e:  # noqa: BLE001
                # 加载失败：先注销 setup 中途已注册的 handler/定时任务（防句柄泄漏），
                # 再清理模块，标记错误，不影响其它插件。
                logger.exception("插件启用失败: %s", meta.name)
                for ctx in contexts:
                    ctx._active = False
                await governor.cancel_all(plugin_id)
                for ctx in reversed(contexts):
                    try:
                        await ctx.aclose(cancel_governor_tasks=False)
                    except Exception as ce:  # noqa: BLE001
                        logger.warning("清理失败插件句柄异常 [%s]: %r", plugin_id, ce)
                await governor.release(plugin_id)
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
        if persist:
            dependents = [
                meta.name for meta in registry.scan()
                if plugin_id in meta.requires_plugins and self.is_loaded(meta.id)
            ]
            capability_dependents: set[str] = set()
            for meta in registry.scan():
                if not self.is_loaded(meta.id) or meta.id == plugin_id:
                    continue
                for capability in meta.requires_capabilities:
                    owners = [owner for _, owner, _ in governor.capabilities.providers(capability)]
                    if plugin_id in owners and not any(owner != plugin_id for owner in owners):
                        capability_dependents.add(meta.name)
            if dependents:
                raise RuntimeError(f"请先停用依赖它的插件：{', '.join(dependents)}")
            if capability_dependents:
                names = ", ".join(sorted(capability_dependents))
                raise RuntimeError(f"请先停用依赖其能力的插件：{names}")
        loaded = self._loaded.pop(plugin_id, None)
        if loaded is not None:
            # 所有实例先同时停止接收新事件，再统一取消平台托管任务。
            for ctx in loaded.contexts:
                ctx._active = False
            await governor.cancel_all(plugin_id)
            for ctx in reversed(loaded.contexts):
                await ctx.aclose(cancel_governor_tasks=False)
            # 保持旧版语义：平台登记的资源先清理，再调用插件 teardown。
            teardown = getattr(loaded.module, "teardown", None)
            if callable(teardown):
                for ctx in reversed(loaded.contexts):
                    try:
                        await governor.execute(
                            plugin_id, f"teardown:{ctx.instance_id}",
                            lambda current=ctx: teardown(current), timeout=10,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("插件 teardown 异常 [%s]: %r", plugin_id, e)
            await governor.release(plugin_id)
            governor.events.append(plugin_id, "plugin_disabled", instances=len(loaded.contexts))
            self._cleanup_module(plugin_id)
            logger.info("插件已停用: %s", registry.display_name(plugin_id))

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
        pending = {meta.id: meta for meta in registry.scan() if meta.enabled and not meta.error}
        capability_providers: dict[str, set[str]] = {}
        for meta in pending.values():
            for capability in meta.provides_capabilities:
                capability_providers.setdefault(capability, set()).add(meta.id)
        while pending:
            progressed = False
            for plugin_id, meta in list(pending.items()):
                if any(dependency in pending for dependency in meta.requires_plugins):
                    continue
                if any(
                    capability_providers.get(capability, set()) & pending.keys()
                    for capability in meta.requires_capabilities
                ):
                    continue
                await self.enable(plugin_id)
                pending.pop(plugin_id, None)
                progressed = True
            if not progressed:
                cycle = ", ".join(sorted(pending))
                logger.error("插件依赖存在循环，无法恢复：%s", cycle)
                break
        logger.info("插件恢复完成，已加载 %d 个", len(self._loaded))

    async def shutdown(self) -> None:
        """停用所有已加载插件（进程退出时调用）。
        仅运行态卸载，绝不持久化 enabled=False——否则重启/更新镜像后插件会全部变未启用。"""
        for plugin_id in reversed(list(self._loaded.keys())):
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
            # 先按加载顺序逆序卸载使用者，再按原顺序恢复提供者，避免依赖和能力链短暂倒置。
            for plugin_id in reversed(ids):
                await self._disable_locked(plugin_id, persist=False)
            for plugin_id in ids:
                # ensure_deps=False：依赖在首次 enable 已就绪，重挂不必重跑 pip。
                await self._enable_locked(plugin_id, ensure_deps=False)
                if plugin_id not in self._loaded:
                    logger.warning("插件 [%s] 重挂失败，启用意图保留待重试", registry.display_name(plugin_id))

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
        self._cleanup_module(plugin_id)
        self._purge_bytecode(plugin_id, single, pkg_init)
        importlib.invalidate_caches()

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
        prefixes = (f"{_MODULE_PREFIX}{plugin_id}", f"plugins.{plugin_id}")
        for name in list(sys.modules):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
                sys.modules.pop(name, None)

    @staticmethod
    def _purge_bytecode(plugin_id: str, single: Path, pkg_init: Path) -> None:
        """删除当前插件的字节码缓存，避免快速修改后重载仍执行旧代码。

        Python 的时间戳型 pyc 只记录秒级修改时间和源码大小；同一秒内把源码
        改成相同大小时，即使模块已从 sys.modules 移除，也可能命中旧 pyc。
        文件夹插件还需要一并清理辅助模块缓存。
        """
        candidates: list[Path] = []
        if single.exists():
            cache_dir = single.parent / "__pycache__"
            if cache_dir.is_dir():
                candidates.extend(cache_dir.glob(f"{plugin_id}.*.pyc"))
            candidates.append(single.with_suffix(".pyc"))
        elif pkg_init.exists():
            candidates.extend(pkg_init.parent.rglob("*.pyc"))

        for cache_file in candidates:
            try:
                cache_file.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("清理插件字节码缓存失败 [%s]: %s", plugin_id, exc)
