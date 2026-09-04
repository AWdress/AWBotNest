from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
import ast
import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .config import PLUGINS_DIR, Settings, save_settings
from .context import PluginContext
from .telegram import TelegramAccounts
from .scheduler import PluginScheduler
from .services import PlatformServices
from .deps import DependencyManager
from .routing import PluginRoutes
from .notifier import NotificationService

logger = logging.getLogger("awbotnest.plugins")
VALID_SCOPES = {"standalone", "bot", "user", "both"}
VALID_RENDER_MODES = {"schema", "vue"}


@dataclass(slots=True)
class PluginMeta:
    id: str
    name: str
    version: str
    scope: str
    description: str = ""
    author: str = ""
    icon: str = ""
    changelog: str = ""
    tags: list[str] | None = None
    render_mode: str = "schema"
    bot: str = ""
    config_schema: dict[str, object] | None = None
    requirements: list[str] | None = None
    resources: dict[str, object] | None = None
    enabled: bool = False
    loaded: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LoadedPlugin:
    meta: PluginMeta
    module: ModuleType
    context: PluginContext


class PluginRuntime:
    def __init__(self, settings: Settings, accounts: TelegramAccounts,
                 scheduler: PluginScheduler, services: PlatformServices,
                 routes: PluginRoutes, notifier: NotificationService,
                 plugins_dir: Path = PLUGINS_DIR) -> None:
        self.settings = settings
        self.accounts = accounts
        self.scheduler = scheduler
        self.services = services
        self.routes = routes
        self.notifier = notifier
        self.plugins_dir = plugins_dir
        self.loaded: dict[str, LoadedPlugin] = {}
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        self.deps = DependencyManager(settings)

    def _entries(self) -> list[Path]:
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        files = [path for path in self.plugins_dir.glob("*.py") if not path.name.startswith("_")]
        files.extend(path / "__init__.py" for path in self.plugins_dir.iterdir()
                     if path.is_dir() and not path.name.startswith("_")
                     and (path / "__init__.py").exists())
        return sorted(files)

    def entry_file(self, plugin_id: str) -> Path | None:
        return next((entry for entry in self._entries()
                     if (entry.parent.name if entry.name == "__init__.py" else entry.stem) == plugin_id), None)

    def uses_platform_ai(self, plugin_id: str) -> bool:
        """静态识别插件是否实际调用 ctx.ai，不导入或执行插件代码。"""
        entry = self.entry_file(plugin_id)
        if entry is None:
            return False
        source_files = ([entry] if entry.name != "__init__.py" else [
            path for path in entry.parent.rglob("*.py") if "__pycache__" not in path.parts
        ])
        return any(self._source_uses_platform_ai(path) for path in source_files)

    @staticmethod
    def _source_uses_platform_ai(path: Path) -> bool:
        try:
            source = path.read_text(encoding="utf-8")
            if "ctx" not in source or "ai" not in source:
                return False
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            return False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != "ai":
                continue
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id == "ctx":
                return True
            # 兼容把插件上下文保存在 self.ctx 的目录插件；不匹配平台通用的 self._ctx 适配层。
            if isinstance(owner, ast.Attribute) and owner.attr == "ctx":
                return True
        return False

    def frontend_dist_dir(self, plugin_id: str) -> Path:
        return self.plugins_dir / plugin_id / "frontend" / "dist"

    def has_frontend(self, plugin_id: str) -> bool:
        dist = self.frontend_dist_dir(plugin_id)
        return (dist / "remoteEntry.js").is_file() or (dist / "assets" / "remoteEntry.js").is_file()

    @staticmethod
    def _module_name(plugin_id: str) -> str:
        return f"awbotnest_plugins.{plugin_id}"

    def _import(self, entry: Path, plugin_id: str) -> ModuleType:
        package_name = "awbotnest_plugins"
        if package_name not in sys.modules:
            namespace = ModuleType(package_name)
            namespace.__path__ = [str(self.plugins_dir)]
            namespace.__package__ = package_name
            sys.modules[package_name] = namespace
        module_name = self._module_name(plugin_id)
        for name in list(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                sys.modules.pop(name, None)
        kwargs = {"submodule_search_locations": [str(entry.parent)]} if entry.name == "__init__.py" else {}
        spec = importlib.util.spec_from_file_location(module_name, entry, **kwargs)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件入口：{entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _metadata(entry: Path) -> dict[str, Any]:
        if entry.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("插件入口文件超过 2 MB")
        tree = ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "__plugin__"
                for target in node.targets
            ):
                value = ast.literal_eval(node.value)
                if isinstance(value, dict):
                    return value
        raise ValueError("缺少可静态读取的 __plugin__ 元数据")

    def display_name(self, plugin_id: str) -> str:
        loaded = self.loaded.get(plugin_id)
        if loaded and loaded.meta.name:
            return loaded.meta.name
        try:
            entry = self.entry_file(plugin_id)
            if entry:
                return str(self._metadata(entry).get("name") or plugin_id)
        except Exception:
            # 日志名称读取失败不应影响原本的操作或错误处理。
            pass
        return plugin_id

    def scan(self) -> list[PluginMeta]:
        result: list[PluginMeta] = []
        for entry in self._entries():
            fallback_id = entry.parent.name if entry.name == "__init__.py" else entry.stem
            try:
                raw = self._metadata(entry)
                plugin_id = str(raw.get("id") or fallback_id)
                scope = str(raw.get("scope") or "user")
                render_mode = str(raw.get("render_mode") or "schema")
                if plugin_id != fallback_id:
                    raise ValueError("__plugin__.id 必须与插件文件或目录名一致")
                if scope not in VALID_SCOPES:
                    raise ValueError(f"scope 必须是 {', '.join(sorted(VALID_SCOPES))}")
                if render_mode not in VALID_RENDER_MODES:
                    raise ValueError(f"render_mode 必须是 {', '.join(sorted(VALID_RENDER_MODES))}")
                if render_mode == "vue" and entry.name != "__init__.py":
                    raise ValueError("render_mode=vue 仅支持带 frontend/ 的目录插件")
                requirements = [str(item) for item in (raw.get("requirements") or [])]
                self.deps.validate(requirements)
                bot_id = str(raw.get("bot") or "")
                if bot_id and bot_id not in {item.id for item in self.settings.bot_specs()}:
                    raise ValueError(f"指定的 Bot 不存在：{bot_id}")
                visible = self.settings.telegram_configured or scope not in {"user", "both"}
                if not visible:
                    continue
                result.append(PluginMeta(
                    id=plugin_id,
                    name=str(raw.get("name") or plugin_id),
                    version=str(raw.get("version") or "0.0.0"),
                    scope=scope,
                    description=str(raw.get("description") or ""),
                    author=str(raw.get("author") or ""),
                    icon=str(raw.get("icon") or ""),
                    changelog=str(raw.get("changelog") or ""),
                    tags=[str(item) for item in (raw.get("tags") or [])],
                    render_mode=render_mode,
                    bot=bot_id,
                    config_schema=dict(raw.get("config_schema") or {}),
                    requirements=requirements,
                    resources=dict(raw.get("resources") or {}),
                    enabled=plugin_id in self.settings.enabled_plugins,
                    loaded=plugin_id in self.loaded,
                ))
            except Exception as exc:
                result.append(PluginMeta(
                    id=fallback_id, name=fallback_id, version="0.0.0", scope="standalone",
                    error=f"{type(exc).__name__}: {exc}",
                ))
        return result

    async def enable(self, plugin_id: str) -> PluginMeta:
        async with self._lifecycle_locks.setdefault(plugin_id, asyncio.Lock()):
            return await self._enable(plugin_id)

    async def _enable(self, plugin_id: str) -> PluginMeta:
        if plugin_id in self.loaded:
            return self.loaded[plugin_id].meta
        entry = self.entry_file(plugin_id)
        if entry is None:
            raise FileNotFoundError(f"插件不存在：{plugin_id}")
        raw = self._metadata(entry)
        scope = str(raw.get("scope") or "user")
        render_mode = str(raw.get("render_mode") or "schema")
        bot_id = str(raw.get("bot") or "")
        if scope not in VALID_SCOPES:
            raise ValueError(f"scope 必须是 {', '.join(sorted(VALID_SCOPES))}")
        if render_mode not in VALID_RENDER_MODES:
            raise ValueError(f"render_mode 必须是 {', '.join(sorted(VALID_RENDER_MODES))}")
        if render_mode == "vue" and entry.name != "__init__.py":
            raise ValueError("render_mode=vue 仅支持带 frontend/ 的目录插件")
        if bot_id and bot_id not in {item.id for item in self.settings.bot_specs()}:
            raise ValueError(f"指定的 Bot 不存在：{bot_id}")
        meta = PluginMeta(
            id=plugin_id,
            name=str(raw.get("name") or plugin_id),
            version=str(raw.get("version") or "0.0.0"),
            scope=scope,
            description=str(raw.get("description") or ""),
            author=str(raw.get("author") or ""),
            icon=str(raw.get("icon") or ""),
            changelog=str(raw.get("changelog") or ""),
            tags=[str(item) for item in (raw.get("tags") or [])],
            render_mode=render_mode,
            bot=bot_id,
            config_schema=dict(raw.get("config_schema") or {}),
            requirements=[str(item) for item in (raw.get("requirements") or [])],
            resources=dict(raw.get("resources") or {}),
            enabled=True,
        )
        if scope in {"user", "both"} and not self.settings.telegram_configured:
            meta.error = "未配置 Telegram API_ID/API_HASH"
            return meta
        try:
            await self.deps.ensure(meta.requirements or [], plugin_name=meta.name)
            module = self._import(entry, plugin_id)
        except Exception as exc:
            meta.error = f"{type(exc).__name__}: {exc}"
            return meta
        setup = getattr(module, "setup", None)
        if not callable(setup):
            meta.error = "插件缺少 setup(ctx)"
            return meta
        context = PluginContext(
            plugin_id, scope, self.accounts, self.scheduler, self.settings,
            self.services, self.routes, self.notifier, meta.bot, meta.resources or {},
            plugin_name=meta.name,
        )
        try:
            value = setup(context)
            if inspect.isawaitable(value):
                await asyncio.wait_for(value, timeout=30)
            meta.loaded = True
            self.loaded[plugin_id] = LoadedPlugin(meta, module, context)
            if plugin_id not in self.settings.enabled_plugins:
                self.settings.enabled_plugins.append(plugin_id)
                save_settings(self.settings)
            logger.info("插件已启用：%s（1 个运行实例）", meta.name)
        except asyncio.CancelledError:
            await context.close()
            raise
        except Exception as exc:
            await context.close()
            meta.error = f"{type(exc).__name__}: {exc}"
            logger.exception("插件启用失败：%s", meta.name)
        return meta

    async def disable(self, plugin_id: str, *, persist: bool = True) -> None:
        async with self._lifecycle_locks.setdefault(plugin_id, asyncio.Lock()):
            await self._disable(plugin_id, persist=persist)

    async def _disable(self, plugin_id: str, *, persist: bool = True) -> None:
        loaded = self.loaded.pop(plugin_id, None)
        if loaded is not None:
            try:
                teardown = getattr(loaded.module, "teardown", None)
                if callable(teardown):
                    value = teardown(loaded.context)
                    if inspect.isawaitable(value):
                        await asyncio.wait_for(value, timeout=15)
            except TimeoutError:
                logger.error("插件停用超时：%s", loaded.meta.name)
            except Exception:
                logger.exception("插件停用钩子失败：%s", loaded.meta.name)
            finally:
                await loaded.context.close()
                sys.modules.pop(self._module_name(plugin_id), None)
        if persist and plugin_id in self.settings.enabled_plugins:
            self.settings.enabled_plugins.remove(plugin_id)
            save_settings(self.settings)

    async def restore(self) -> None:
        available = {meta.id for meta in self.scan() if not meta.error}
        for plugin_id in list(self.settings.enabled_plugins):
            if plugin_id in available:
                try:
                    async with self._lifecycle_locks.setdefault(plugin_id, asyncio.Lock()):
                        # The web UI can disable a plugin while earlier plugins are restoring.
                        if plugin_id in self.settings.enabled_plugins:
                            await self._enable(plugin_id)
                except Exception:
                    logger.exception("恢复插件失败，平台继续启动：%s", self.display_name(plugin_id))

    async def stop(self) -> None:
        for plugin_id in reversed(list(self.loaded)):
            await self.disable(plugin_id, persist=False)

    async def refresh_telegram_plugins(self) -> None:
        candidates = [item.id for item in self.scan()
                      if item.id in self.settings.enabled_plugins and item.scope in {"user", "both"}]
        for plugin_id in candidates:
            try:
                if plugin_id in self.loaded:
                    await self.disable(plugin_id, persist=False)
                await self.enable(plugin_id)
            except Exception:
                logger.exception("刷新 Telegram 插件失败：%s", self.display_name(plugin_id))

    def self_check(self) -> dict[str, object]:
        scanned = self.scan()
        errors = [{"id": item.id, "error": item.error} for item in scanned if item.error]
        missing_dependencies = {
            item.id: self.deps.missing(item.requirements or []) for item in scanned if not item.error
        }
        missing_dependencies = {key: value for key, value in missing_dependencies.items() if value}
        missing_clients = [
            item.id for item in scanned
            if item.enabled and item.scope != "standalone" and not self.accounts.clients_for_scope(item.scope, item.bot)
        ]
        return {
            "ok": not errors and not missing_clients and not missing_dependencies,
            "scan_errors": errors,
            "enabled_without_client": missing_clients,
            "missing_dependencies": missing_dependencies,
            "loaded": sorted(self.loaded),
        }

    @staticmethod
    def validate_config(schema: dict[str, object], values: dict[str, object], *, allow_extra: bool = False) -> None:
        expected = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                    "array": list, "object": dict}
        unknown = set(values) - set(schema)
        if schema and unknown and not allow_extra:
            raise ValueError(f"包含未声明的配置项：{', '.join(sorted(unknown))}")
        for key, raw in schema.items():
            spec = raw if isinstance(raw, dict) else {}
            if spec.get("required") and (key not in values or values[key] is None or values[key] == ""):
                raise ValueError(f"配置项 {key} 不能为空")
            if key not in values or values[key] in (None, ""):
                continue
            type_name = str(spec.get("type") or "")
            target = expected.get(type_name)
            invalid_boolean_number = type_name in {"integer", "number"} and isinstance(values[key], bool)
            if target and (not isinstance(values[key], target) or invalid_boolean_number):
                raise ValueError(f"配置项 {key} 应为 {type_name}")
            choices = spec.get("enum")
            if isinstance(choices, list) and values[key] not in choices:
                raise ValueError(f"配置项 {key} 不在允许范围内")
