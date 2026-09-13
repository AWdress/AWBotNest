from __future__ import annotations

import inspect
import logging
import asyncio
import re
from pathlib import Path

from .config import PLUGINS_DIR, Settings, save_settings
from .context import PluginContext
from .telegram import TelegramAccounts
from .scheduler import PluginScheduler
from .services import PlatformServices
from .deps import DependencyManager
from .cloak_proxy import configure_cloakbrowser, requirements_use_cloakbrowser
from .routing import PluginRoutes
from .notifier import NotificationService
from .plugin_runtime import LoadedPlugin, PluginLoader, PluginMeta, PluginResolver, PluginScanner

logger = logging.getLogger("awbotnest.plugins")

_CRON_TOKEN = re.compile(r"^[0-9A-Za-z*?/,#LW-]+$")
_CRON_NAMES = re.compile(
    r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|MON|TUE|WED|THU|FRI|SAT|SUN",
    re.IGNORECASE,
)


def _valid_cron_expression(value: object) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 512:
        return False
    parts = text.split()
    if len(parts) not in {5, 6}:
        return False
    bounds = (
        ((0, 59), (0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
        if len(parts) == 6
        else ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    )
    for part, (minimum, maximum) in zip(parts, bounds, strict=True):
        if not _CRON_TOKEN.fullmatch(part):
            return False
        if not re.fullmatch(r"[0-9*?/,#LW-]+", _CRON_NAMES.sub("", part), re.IGNORECASE):
            return False
        if any(not minimum <= int(number) <= maximum for number in re.findall(r"\d+", part)):
            return False
    return True


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
        self._errors: dict[str, str] = {}
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        self.deps = DependencyManager(settings)
        self.resolver = PluginResolver()
        self.scanner = PluginScanner(settings, self.deps, plugins_dir, self.loaded, self.resolver)
        self.loader = PluginLoader(plugins_dir)

    def _entries(self) -> list[Path]:
        return self.scanner.entries()

    def entry_file(self, plugin_id: str) -> Path | None:
        return self.scanner.entry_file(plugin_id)

    def uses_platform_ai(self, plugin_id: str) -> bool:
        return self.scanner.uses_platform_ai(plugin_id)

    def cloakbrowser_channels(self) -> tuple[str, ...]:
        return self.scanner.cloakbrowser_channels()

    @staticmethod
    def _source_uses_platform_ai(path: Path) -> bool:
        return PluginScanner.source_uses_platform_ai(path)

    def frontend_dist_dir(self, plugin_id: str) -> Path:
        return self.plugins_dir / plugin_id / "frontend" / "dist"

    def has_frontend(self, plugin_id: str) -> bool:
        dist = self.frontend_dist_dir(plugin_id)
        return (dist / "remoteEntry.js").is_file() or (dist / "assets" / "remoteEntry.js").is_file()

    @staticmethod
    def _extensions(raw):
        return PluginResolver.extensions(raw)

    @staticmethod
    def _module_name(plugin_id: str) -> str:
        return PluginLoader.module_name(plugin_id)

    def _import(self, entry: Path, plugin_id: str):
        # Keep low-level compatibility for diagnostics that construct a runtime without
        # calling __init__; normal runtime instances always own a configured loader.
        loader = getattr(self, "loader", None) or PluginLoader(self.plugins_dir)
        return loader.load(entry, plugin_id)

    @staticmethod
    def _metadata(entry: Path):
        return PluginScanner.metadata(entry)

    def display_name(self, plugin_id: str) -> str:
        loaded = self.loaded.get(plugin_id)
        if loaded and loaded.meta.name:
            return loaded.meta.name
        try:
            entry = self.entry_file(plugin_id)
            if entry:
                return str(self._metadata(entry).get("name") or plugin_id)
        except Exception:
            pass
        return plugin_id

    def scan(self) -> list[PluginMeta]:
        return self.scanner.scan()

    def invalidate_scan_cache(self) -> None:
        self.scanner.invalidate_scan_cache()


    async def enable(self, plugin_id: str) -> PluginMeta:
        async with self._lifecycle_locks.setdefault(plugin_id, asyncio.Lock()):
            try:
                meta = await self._enable(plugin_id)
            except Exception as exc:
                self._errors[plugin_id] = f"{type(exc).__name__}: {exc}"
                raise
            if meta.error:
                self._errors[plugin_id] = meta.error
            else:
                self._errors.pop(plugin_id, None)
            return meta

    async def _enable(self, plugin_id: str) -> PluginMeta:
        if plugin_id in self.loaded:
            return self.loaded[plugin_id].meta
        entry = self.entry_file(plugin_id)
        if entry is None:
            raise FileNotFoundError(f"插件不存在：{plugin_id}")
        raw = self._metadata(entry)
        meta = self.resolver.descriptor(entry, raw, plugin_id, enabled=True)
        if meta.bot and meta.bot not in {item.id for item in self.settings.bot_specs()}:
            raise ValueError(f"指定的 Bot 不存在：{meta.bot}")
        meta.error = self.resolver.incompatibility(
            meta,
            telegram_configured=self.settings.telegram_configured,
            bot_ids={item.id for item in self.settings.bot_specs()},
            loaded_plugins=self.loaded,
            capabilities=self.services.governor.capabilities.names(),
            display_name=self.display_name,
        )
        if meta.error:
            return meta
        try:
            await self.deps.ensure(meta.requirements or [], plugin_name=meta.name)
            if requirements_use_cloakbrowser(meta.requirements or []):
                configure_cloakbrowser(self.settings)
            module = self._import(entry, plugin_id)
        except Exception as exc:
            meta.error = f"{type(exc).__name__}: {exc}"
            return meta
        setup = getattr(module, "setup", None)
        if not callable(setup):
            meta.error = "插件缺少 setup(ctx)"
            self.loader.unload(plugin_id)
            return meta
        contexts = []
        was_enabled = plugin_id in self.settings.enabled_plugins
        try:
            names = [None]
            if meta.instance_mode == "account" and meta.scope in {"user", "both"}:
                selected = self.settings.plugin_accounts.get(plugin_id, [])
                names = [name for name, client in self.accounts.users.items()
                         if client.is_connected() and (not selected or name in selected)]
                if not names:
                    raise RuntimeError("按账号运行的插件当前没有可用用户账号")
            for index, name in enumerate(names):
                context = PluginContext(
                    plugin_id, meta.scope, self.accounts, self.scheduler, self.settings,
                    self.services, self.routes, self.notifier, meta.bot, meta.resources or {},
                    plugin_name=meta.name, account_name=name, primary_instance=index == 0,
                    cookie_domains=meta.cookie_domains,
                )
                contexts.append(context)
                await context.execute("setup", lambda: setup(context), timeout=30)
            meta.loaded = True
            self.loaded[plugin_id] = LoadedPlugin(meta, module, contexts[0], contexts)
            if plugin_id not in self.settings.enabled_plugins:
                self.settings.enabled_plugins.append(plugin_id)
                save_settings(self.settings)
            logger.info("插件已启用：%s（%d 个运行实例）", meta.name, len(contexts))
        except asyncio.CancelledError:
            for context in reversed(contexts):
                await context.close()
            await self.services.governor.release(plugin_id)
            self.loader.unload(plugin_id)
            raise
        except Exception as exc:
            self.loaded.pop(plugin_id, None)
            meta.loaded = False
            if not was_enabled and plugin_id in self.settings.enabled_plugins:
                self.settings.enabled_plugins.remove(plugin_id)
            for context in reversed(contexts):
                await context.close()
            await self.services.governor.release(plugin_id)
            self.loader.unload(plugin_id)
            meta.error = f"{type(exc).__name__}: {exc}"
            logger.exception("插件启用失败：%s", meta.name)
        return meta

    async def disable(self, plugin_id: str, *, persist: bool = True) -> None:
        async with self._lifecycle_locks.setdefault(plugin_id, asyncio.Lock()):
            await self._disable(plugin_id, persist=persist)

    async def _disable(self, plugin_id: str, *, persist: bool = True) -> None:
        self._errors.pop(plugin_id, None)
        loaded = self.loaded.pop(plugin_id, None)
        cancellation: asyncio.CancelledError | None = None
        if loaded is not None:
            for context in reversed(loaded.contexts or [loaded.context]):
                try:
                    teardown = getattr(loaded.module, "teardown", None)
                    if cancellation is None and callable(teardown):
                        value = teardown(context)
                        if inspect.isawaitable(value):
                            await asyncio.wait_for(value, timeout=15)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                except TimeoutError:
                    logger.error("插件停用超时：%s", loaded.meta.name)
                except Exception:
                    logger.exception("插件停用钩子失败：%s", loaded.meta.name)
                finally:
                    try:
                        await context.close()
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                    except Exception:
                        logger.exception("插件上下文清理失败：%s", loaded.meta.name)
            try:
                await self.services.governor.release(plugin_id)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                logger.exception("插件治理资源清理失败：%s", loaded.meta.name)
            finally:
                self.loader.unload(plugin_id)
        if persist and plugin_id in self.settings.enabled_plugins:
            self.settings.enabled_plugins.remove(plugin_id)
            save_settings(self.settings)
        if cancellation is not None:
            raise cancellation

    async def restore(self) -> None:
        pending = {meta.id: meta for meta in self.scan()
                   if not meta.error and meta.id in self.settings.enabled_plugins}
        while pending:
            progressed = False
            for plugin_id, meta in list(pending.items()):
                if any(item in pending for item in meta.requires_plugins):
                    continue
                if any(any(capability in other.provides_capabilities for other in pending.values())
                       for capability in meta.requires_capabilities):
                    continue
                try:
                    async with self._lifecycle_locks.setdefault(plugin_id, asyncio.Lock()):
                        # The web UI can disable a plugin while earlier plugins are restoring.
                        if plugin_id in self.settings.enabled_plugins:
                            meta = await self._enable(plugin_id)
                            if meta.error:
                                self._errors[plugin_id] = meta.error
                                logger.error("恢复插件失败：%s（%s）", meta.name, meta.error)
                except Exception:
                    logger.exception("恢复插件失败，系统继续启动：%s", self.display_name(plugin_id))
                pending.pop(plugin_id)
                progressed = True
            if not progressed:
                for plugin_id, meta in pending.items():
                    self._errors[plugin_id] = "插件依赖存在循环，无法恢复"
                logger.error("插件依赖存在循环，无法恢复：%s", "、".join(item.name for item in pending.values()))
                break

    async def stop(self) -> None:
        for plugin_id in reversed(list(self.loaded)):
            try:
                await self.disable(plugin_id, persist=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("停止插件失败，继续清理其他插件：%s", self.display_name(plugin_id))

    async def refresh_telegram_plugins(self) -> None:
        candidates = [item.id for item in self.scan()
                      if item.id in self.settings.enabled_plugins and item.scope in {"user", "both"}]
        # 按加载顺序先逆序卸载，再按依赖顺序恢复。
        for plugin_id in reversed(list(self.loaded)):
            if plugin_id in candidates:
                await self.disable(plugin_id, persist=False)
        await self.restore()

    async def reload(self, plugin_id):
        async with self._lifecycle_locks.setdefault(plugin_id, asyncio.Lock()):
            await self._disable(plugin_id, persist=False)
            try:
                meta = await self._enable(plugin_id)
                if meta.error:
                    self._errors[plugin_id] = meta.error
                return meta
            except Exception as exc:
                self._errors[plugin_id] = f"{type(exc).__name__}: {exc}"
                raise

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
    def secret_field(spec: object) -> bool:
        return isinstance(spec, dict) and bool(spec.get("secret") or spec.get("type") == "password")

    @staticmethod
    def validate_config(schema: dict[str, object], values: dict[str, object], *, allow_extra: bool = False) -> None:
        expected = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                    "array": list, "object": dict, "cron": str}
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
            if (spec.get("format") == "cron" or type_name == "cron") and not _valid_cron_expression(values[key]):
                raise ValueError(f"配置项 {key} 应为 5 位或 6 位 Cron 表达式")
            choices = spec.get("enum")
            if isinstance(choices, list) and values[key] not in choices:
                raise ValueError(f"配置项 {key} 不在允许范围内")
