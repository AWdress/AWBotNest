from __future__ import annotations

from dataclasses import asdict, dataclass, field
from types import ModuleType
from typing import Any

from ..context import PluginContext


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
    instance_mode: str = "shared"
    requires_plugins: list[str] = field(default_factory=list)
    requires_capabilities: list[str] = field(default_factory=list)
    provides_capabilities: list[str] = field(default_factory=list)
    cookie_domains: list[str] = field(default_factory=list)
    plugin_api_version: int = 2
    min_platform_version: str = ""
    max_platform_version: str = ""
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
    contexts: list[PluginContext] = field(default_factory=list)
