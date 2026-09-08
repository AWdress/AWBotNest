from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


class PluginLoader:
    """Owns Python module cache invalidation, import, and unload mechanics."""

    def __init__(self, plugins_dir: Path) -> None:
        self.plugins_dir = plugins_dir

    @staticmethod
    def module_name(plugin_id: str) -> str:
        return f"awbotnest_plugins.{plugin_id}"

    def load(self, entry: Path, plugin_id: str) -> ModuleType:
        # Same-second updates can retain size and timestamp, so remove bytecode first.
        sources = entry.parent.rglob("*.py") if entry.name == "__init__.py" else [entry]
        for source in sources:
            cache = Path(importlib.util.cache_from_source(str(source)))
            if cache.is_file() and cache.resolve().is_relative_to(self.plugins_dir.resolve()):
                cache.unlink()
        importlib.invalidate_caches()
        package_name = "awbotnest_plugins"
        if package_name not in sys.modules:
            namespace = ModuleType(package_name)
            namespace.__path__ = [str(self.plugins_dir)]
            namespace.__package__ = package_name
            sys.modules[package_name] = namespace
        module_name = self.module_name(plugin_id)
        self.unload(plugin_id)
        kwargs = {"submodule_search_locations": [str(entry.parent)]} if entry.name == "__init__.py" else {}
        spec = importlib.util.spec_from_file_location(module_name, entry, **kwargs)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件入口：{entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # A partially imported plugin must never survive a failed/cancelled load.
            self.unload(plugin_id)
            raise
        return module

    def unload(self, plugin_id: str) -> None:
        prefix = self.module_name(plugin_id)
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name, None)
