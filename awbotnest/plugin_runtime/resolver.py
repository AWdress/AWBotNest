from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Collection

from packaging.version import InvalidVersion, Version

from .. import __version__
from .models import PluginMeta


VALID_SCOPES = {"standalone", "bot", "user", "both"}
VALID_RENDER_MODES = {"schema", "vue"}


class PluginResolver:
    """Builds validated descriptors and resolves platform/plugin requirements."""

    @staticmethod
    def extensions(raw: dict[str, Any]) -> dict[str, Any]:
        mode = str(raw.get("instance_mode") or "shared")
        if mode not in {"shared", "account"}:
            raise ValueError("instance_mode 必须为 shared 或 account")
        result: dict[str, Any] = {
            "instance_mode": mode,
            "plugin_api_version": int(raw.get("plugin_api_version") or 2),
            "min_platform_version": str(raw.get("min_platform_version") or ""),
            "max_platform_version": str(raw.get("max_platform_version") or ""),
        }
        for name in ("requires_plugins", "requires_capabilities", "provides_capabilities", "cookie_domains"):
            value = raw.get(name) or []
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"{name} 必须为非空字符串列表")
            result[name] = list(dict.fromkeys(value))
        return result

    def descriptor(self, entry: Path, raw: dict[str, Any], fallback_id: str, *,
                   enabled: bool = False, loaded: bool = False) -> PluginMeta:
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
        return PluginMeta(
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
            bot=str(raw.get("bot") or ""),
            config_schema=dict(raw.get("config_schema") or {}),
            requirements=[str(item) for item in (raw.get("requirements") or [])],
            resources=dict(raw.get("resources") or {}),
            **self.extensions(raw),
            enabled=enabled,
            loaded=loaded,
        )

    @staticmethod
    def incompatibility(meta: PluginMeta, *, telegram_configured: bool,
                        bot_ids: Collection[str], loaded_plugins: Collection[str],
                        capabilities: Collection[str], display_name: Callable[[str], str]) -> str:
        if meta.bot and meta.bot not in bot_ids:
            return f"指定的 Bot 不存在：{meta.bot}"
        if meta.scope in {"user", "both"} and not telegram_configured:
            return "未配置 Telegram API_ID/API_HASH"
        if meta.plugin_api_version > 2:
            return f"插件需要接口版本 {meta.plugin_api_version}，当前平台支持 2"
        try:
            if meta.min_platform_version and Version(__version__) < Version(meta.min_platform_version.lstrip("vV")):
                return f"插件要求平台不低于 {meta.min_platform_version}"
            if meta.max_platform_version and Version(__version__) > Version(meta.max_platform_version.lstrip("vV")):
                return f"插件只兼容到平台 {meta.max_platform_version}"
        except InvalidVersion:
            return "插件声明的平台兼容版本格式不正确"
        missing_plugins = [display_name(item) for item in meta.requires_plugins if item not in loaded_plugins]
        if missing_plugins:
            return "请先启用依赖插件：" + "、".join(missing_plugins)
        missing_capabilities = [item for item in meta.requires_capabilities if item not in capabilities]
        if missing_capabilities:
            return "缺少平台能力：" + "、".join(missing_capabilities)
        return ""
