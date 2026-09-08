from __future__ import annotations

import ast
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

    def entries(self) -> list[Path]:
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        files = [path for path in self.plugins_dir.glob("*.py") if not path.name.startswith("_")]
        files.extend(path / "__init__.py" for path in self.plugins_dir.iterdir()
                     if path.is_dir() and not path.name.startswith("_")
                     and (path / "__init__.py").exists())
        return sorted(files)

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
        entry = self.entry_file(plugin_id)
        if entry is None:
            return False
        sources = ([entry] if entry.name != "__init__.py" else [
            path for path in entry.parent.rglob("*.py") if "__pycache__" not in path.parts
        ])
        return any(self.source_uses_platform_ai(path) for path in sources)

    def scan(self) -> list[PluginMeta]:
        result: list[PluginMeta] = []
        bot_ids = {item.id for item in self.settings.bot_specs()}
        for entry in self.entries():
            fallback_id = entry.parent.name if entry.name == "__init__.py" else entry.stem
            try:
                raw = self.metadata(entry)
                meta = self.resolver.descriptor(
                    entry, raw, fallback_id,
                    enabled=fallback_id in self.settings.enabled_plugins,
                    loaded=fallback_id in self.loaded,
                )
                self.dependencies.validate(meta.requirements or [])
                if meta.bot and meta.bot not in bot_ids:
                    raise ValueError(f"指定的 Bot 不存在：{meta.bot}")
                if not self.settings.telegram_configured and meta.scope in {"user", "both"}:
                    continue
                result.append(meta)
            except Exception as exc:
                result.append(PluginMeta(
                    id=fallback_id, name=fallback_id, version="0.0.0", scope="standalone",
                    error=f"{type(exc).__name__}: {exc}",
                ))
        return result
