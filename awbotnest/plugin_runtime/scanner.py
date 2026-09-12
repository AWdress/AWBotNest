from __future__ import annotations

import ast
import copy
import threading
from pathlib import Path
from typing import Any, Mapping

from ..config import Settings
from ..deps import DependencyManager
from .models import LoadedPlugin, PluginMeta
from .resolver import PluginResolver


class PluginScanner:
    """Discovers plugin entries and reads metadata without importing plugin code."""

    def __init__(self, settings: Settings, dependencies: DependencyManager, plugins_dir: Path,
                 loaded: Mapping[str, LoadedPlugin], resolver: PluginResolver) -> None:
        self.settings = settings
        self.dependencies = dependencies
        self.plugins_dir = plugins_dir
        self.loaded = loaded
        self.resolver = resolver
        self._lock = threading.RLock()
        self._scan_cache_signature: tuple[tuple[str, int, int], ...] | None = None
        self._scan_cache: list[PluginMeta] = []
        self._ai_usage_cache: dict[str, tuple[tuple[tuple[str, int, int], ...], bool]] = {}

    def entries(self) -> list[Path]:
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        files = [path for path in self.plugins_dir.glob("*.py") if not path.name.startswith("_")]
        files.extend(path / "__init__.py" for path in self.plugins_dir.iterdir()
                     if path.is_dir() and not path.name.startswith("_")
                     and (path / "__init__.py").exists())
        return sorted(files)

    def _entry_signature(self) -> tuple[tuple[str, int, int], ...]:
        signature: list[tuple[str, int, int]] = []
        for entry in self.entries():
            try:
                stat = entry.stat()
                signature.append((str(entry), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(signature)

    def _cached_metas(self) -> list[PluginMeta]:
        metas = copy.deepcopy(self._scan_cache)
        for meta in metas:
            meta.enabled = meta.id in self.settings.enabled_plugins
            meta.loaded = meta.id in self.loaded
        bot_ids = {item.id for item in self.settings.bot_specs()}
        visible: list[PluginMeta] = []
        for meta in metas:
            if meta.bot and meta.bot not in bot_ids and not meta.error:
                meta.error = f"ValueError: 指定的 Bot 不存在：{meta.bot}"
            if self.settings.telegram_configured or meta.scope not in {"user", "both"}:
                visible.append(meta)
        return visible

    def invalidate_scan_cache(self) -> None:
        with self._lock:
            self._scan_cache_signature = None
            self._scan_cache = []
            self._ai_usage_cache = {}

    def entry_file(self, plugin_id: str) -> Path | None:
        return next((entry for entry in self.entries()
                     if (entry.parent.name if entry.name == "__init__.py" else entry.stem) == plugin_id), None)

    @staticmethod
    def metadata(entry: Path) -> dict[str, Any]:
        if entry.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("插件入口文件超过 2 MB")
        tree = ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "__plugin__" for target in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, dict):
                    return value
        raise ValueError("缺少可静态读取的 __plugin__ 元数据")

    @staticmethod
    def source_uses_platform_ai(path: Path) -> bool:
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
            if isinstance(owner, ast.Attribute) and owner.attr == "ctx":
                return True
        return False

    def uses_platform_ai(self, plugin_id: str) -> bool:
        with self._lock:
            entry = self.entry_file(plugin_id)
            if entry is None:
                self._ai_usage_cache.pop(plugin_id, None)
                return False
            sources = ([entry] if entry.name != "__init__.py" else [
                path for path in entry.parent.rglob("*.py") if "__pycache__" not in path.parts
            ])
            signature: list[tuple[str, int, int]] = []
            for path in sources:
                try:
                    stat = path.stat()
                    signature.append((str(path), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    continue
            cache_key = tuple(signature)
            cached = self._ai_usage_cache.get(plugin_id)
            if cached is not None and cached[0] == cache_key:
                return cached[1]
            result = any(self.source_uses_platform_ai(path) for path in sources)
            self._ai_usage_cache[plugin_id] = (cache_key, result)
            return result

    @staticmethod
    def source_cloakbrowser_channels(path: Path) -> set[str]:
        """Find unpinned CloakBrowser channels or platform-browser use in source."""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            return set()

        launch_names = {
            "launch", "launch_async", "launch_context", "launch_context_async",
            "launch_persistent_context", "launch_persistent_context_async",
        }
        direct_launches: set[str] = set()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"cloakbrowser", "cloakbrowser.browser"}:
                        module_aliases.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and str(node.module or "").startswith("cloakbrowser"):
                for alias in node.names:
                    if alias.name in launch_names:
                        direct_launches.add(alias.asname or alias.name)
                    elif alias.name == "browser":
                        module_aliases.add(alias.asname or alias.name)

        channels: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Platform ctx.browser always uses CloakBrowser's default Stable channel.
            if isinstance(node.func, ast.Attribute) and node.func.attr == "run":
                owner = node.func.value
                if (isinstance(owner, ast.Attribute) and owner.attr == "browser"
                        and (isinstance(owner.value, ast.Name) and owner.value.id == "ctx"
                             or isinstance(owner.value, ast.Attribute)
                             and owner.value.attr == "ctx")):
                    channels.add("stable")
                    continue

            is_launch = isinstance(node.func, ast.Name) and node.func.id in direct_launches
            if isinstance(node.func, ast.Attribute):
                is_launch = (
                    node.func.attr in launch_names
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in module_aliases
                )
            if not is_launch:
                continue
            keywords = {item.arg: item.value for item in node.keywords if item.arg}
            if any(item.arg is None for item in node.keywords):
                # **kwargs may contain a version pin or channel; let the real call
                # download exactly what it resolves instead of guessing here.
                continue
            version_node = keywords.get("browser_version")
            if version_node is not None:
                if not (isinstance(version_node, ast.Constant) and version_node.value in {None, ""}):
                    continue
            channel_node = keywords.get("release_channel")
            if channel_node is None:
                channels.add("stable")
            elif isinstance(channel_node, ast.Constant):
                channels.add("preview" if str(channel_node.value).lower() == "preview" else "stable")
            # Dynamic channel selection is deliberately deferred to the actual call.
        return channels

    def cloakbrowser_channels(self) -> tuple[str, ...]:
        """Return channels required by the currently enabled plugin sources."""
        channels: set[str] = set()
        for meta in self.scan():
            if not meta.enabled or meta.error:
                continue
            entry = self.entry_file(meta.id)
            if entry is None:
                continue
            sources = ([entry] if entry.name != "__init__.py" else [
                path for path in entry.parent.rglob("*.py") if "__pycache__" not in path.parts
            ])
            for source in sources:
                channels.update(self.source_cloakbrowser_channels(source))
        return tuple(channel for channel in ("stable", "preview") if channel in channels)

    def scan(self) -> list[PluginMeta]:
        with self._lock:
            signature = self._entry_signature()
            if signature == self._scan_cache_signature:
                return self._cached_metas()

            result: list[PluginMeta] = []
            for entry in self.entries():
                fallback_id = entry.parent.name if entry.name == "__init__.py" else entry.stem
                try:
                    raw = self.metadata(entry)
                    meta = self.resolver.descriptor(entry, raw, fallback_id)
                    self.dependencies.validate(meta.requirements or [])
                    result.append(meta)
                except Exception as exc:
                    result.append(PluginMeta(
                        id=fallback_id, name=fallback_id, version="0.0.0", scope="standalone",
                        error=f"{type(exc).__name__}: {exc}",
                    ))
            self._scan_cache = sorted(result, key=lambda item: item.id)
            self._scan_cache_signature = signature
            return self._cached_metas()
