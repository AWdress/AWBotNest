"""Schema-aware secret presentation and JSON Pointer field access."""
from __future__ import annotations

import copy
import asyncio
import inspect
import json
import logging
import re
from typing import Any

MASK = "********"


async def save_plugin_config(deps, plugin_id: str, values: dict[str, Any], *,
                             before_change=None, after_change=None, before_recover=None,
                             authorize=None, persist=None) -> dict[str, Any]:
    """Shared administrative save path, including validation and live reload.

    The assistant supplies synchronous approval checks; HTTP callers already
    authenticate before entering this function. A failed save or reload restores
    the previous configuration, rather than leaving a broken partial update.
    """
    from .config import save_settings
    settings, runtime = deps.settings, deps.runtime
    persist = persist or save_settings

    def check(callback):
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("配置保存校验不能异步执行")
        if result is False:
            raise PermissionError("配置修改授权已撤销")

    locks = getattr(runtime, "_lifecycle_locks", None)
    if locks is None:
        locks = runtime._lifecycle_locks = {}
    async with locks.setdefault(plugin_id, asyncio.Lock()):
        check(authorize)
        check(before_change)
        plugin = next((item for item in runtime.scan() if item.id == plugin_id), None)
        if plugin is None:
            raise ValueError("插件不存在")
        if not isinstance(values, dict):
            raise ValueError("插件配置必须是对象")
        if len(json.dumps(values, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 1024 * 1024:
            raise ValueError("插件配置超过 1 MB")
        previous_present = plugin_id in settings.plugin_config
        previous = copy.deepcopy(settings.plugin_config.get(plugin_id, {}))
        restored = restore_config_secrets(plugin.config_schema or {}, values, previous)
        runtime.validate_config(plugin.config_schema or {}, restored, allow_extra=plugin.render_mode == "vue")
        was_loaded = plugin_id in runtime.loaded
        previous_instance = runtime.loaded.get(plugin_id)
        was_enabled = plugin_id in settings.enabled_plugins
        # Real runtimes use these unlocked helpers under the same lifecycle lock.
        disable = getattr(runtime, "_disable", None) or getattr(runtime, "disable", None)
        enable = getattr(runtime, "_enable", None) or getattr(runtime, "enable", None)
        if was_loaded and (not callable(disable) or not callable(enable)):
            raise ValueError("插件重载服务暂不可用")
        changed = False
        try:
            settings.plugin_config[plugin_id] = restored
            persist(settings)
            changed = True
            check(after_change)
            if was_loaded:
                await disable(plugin_id, persist=False)
                check(authorize)
                check(after_change)
                if plugin_id not in settings.enabled_plugins:
                    settings.enabled_plugins.append(plugin_id)
                if getattr(runtime, "_enable", None) is not None:
                    def before_start():
                        check(authorize)
                        check(after_change)
                    result = await enable(plugin_id, before_start=before_start)
                else:
                    result = await enable(plugin_id)
                check(authorize)
                check(after_change)
                if getattr(result, "error", ""):
                    raise ValueError("插件配置未生效，已恢复原配置；请查看插件日志")
        except BaseException:
            if changed and was_loaded and plugin_id in runtime.loaded and runtime.loaded[plugin_id] is not previous_instance:
                try:
                    await disable(plugin_id, persist=False)
                except Exception:
                    logging.getLogger("awbotnest.plugins").exception("配置回滚时插件停用失败：%s", plugin_id)
            if previous_present:
                settings.plugin_config[plugin_id] = previous
            else:
                settings.plugin_config.pop(plugin_id, None)
            if was_enabled and plugin_id not in settings.enabled_plugins:
                settings.enabled_plugins.append(plugin_id)
            elif not was_enabled and plugin_id in settings.enabled_plugins:
                settings.enabled_plugins.remove(plugin_id)
            if changed:
                try:
                    persist(settings)
                    if was_loaded and plugin_id not in runtime.loaded:
                        check(before_recover)
                        if getattr(runtime, "_enable", None) is not None and before_recover is not None:
                            recovered = await enable(plugin_id, before_start=lambda: check(before_recover))
                        else:
                            recovered = await enable(plugin_id)
                        if getattr(recovered, "error", ""):
                            logging.getLogger("awbotnest.plugins").error("插件原配置恢复后启动失败：%s", plugin_id)
                except (ValueError, PermissionError):
                    logging.getLogger("awbotnest.plugins").warning("插件运行状态未恢复，请检查插件状态：%s", plugin_id)
                except Exception:
                    logging.getLogger("awbotnest.plugins").exception("插件配置恢复失败：%s", plugin_id)
            raise
        return {"ok": True, "values": mask_config(plugin.config_schema or {}, settings.plugin_config[plugin_id])}


def secret_field(spec: object) -> bool:
    return isinstance(spec, dict) and bool(spec.get("secret") or spec.get("type") == "password")


def _children(spec: dict[str, Any]) -> dict[str, Any]:
    fields = spec.get("fields", spec.get("properties", {}))
    return fields if isinstance(fields, dict) else {}


def _contains_mask(spec: object, value: Any) -> bool:
    spec = spec if isinstance(spec, dict) else {}
    if secret_field(spec):
        return value == MASK
    if isinstance(value, dict):
        return any(_contains_mask(child, value.get(key)) for key, child in _children(spec).items())
    if isinstance(value, list):
        item_spec = spec.get("items") or {"properties": _children(spec)}
        return any(_contains_mask(item_spec, item) for item in value)
    return False


def _transform(spec: object, value: Any, previous: Any, *, restore: bool) -> Any:
    spec = spec if isinstance(spec, dict) else {}
    if secret_field(spec):
        if restore:
            if value == MASK:
                if previous is None:
                    raise ValueError("敏感配置尚未保存，请填写真实值")
                return copy.deepcopy(previous)
            return copy.deepcopy(value)
        return MASK if value not in (None, "", [], {}) else copy.deepcopy(value)
    if isinstance(value, list):
        previous = previous if isinstance(previous, list) else []
        fields = _children(spec)
        item_spec = spec.get("items") or ({"properties": fields} if fields else None)
        if isinstance(item_spec, dict):
            def identity(item: Any) -> str:
                return json.dumps(_transform(item_spec, item, None, restore=False),
                                  sort_keys=True, ensure_ascii=False)
            previous_keys: dict[str, list[int]] = {}
            if restore:
                for index, item in enumerate(previous):
                    previous_keys.setdefault(identity(item), []).append(index)
            moved = restore and any(
                _contains_mask(item_spec, item) and len(previous_keys.get(identity(item), [])) == 1
                and previous_keys[identity(item)][0] != index for index, item in enumerate(value))
            result = []
            for index, item in enumerate(value):
                old = previous[index] if index < len(previous) else None
                if restore and _contains_mask(item_spec, item):
                    key = identity(item)
                    matches = previous_keys.get(key, [])
                    if len(matches) == 1:
                        old = previous[matches[0]]
                    elif len(value) != len(previous) or moved:
                        raise ValueError("列表行已变化，请先读取敏感值再删除或调整顺序")
                result.append(_transform(item_spec, item, old, restore=restore))
            return result
    if isinstance(value, dict):
        previous = previous if isinstance(previous, dict) else {}
        fields = _children(spec)
        return {key: _transform(fields[key], item, previous.get(key), restore=restore)
                if key in fields else copy.deepcopy(item) for key, item in value.items()}
    return copy.deepcopy(value)


def mask_config(schema: dict[str, object], values: dict[str, object]) -> dict[str, object]:
    return {key: _transform(schema.get(key), value, None, restore=False)
            for key, value in values.items()}


def restore_config_secrets(schema: dict[str, object], values: dict[str, object],
                           previous: dict[str, object]) -> dict[str, object]:
    return {key: _transform(schema.get(key), value, previous.get(key), restore=True)
            for key, value in values.items()}


def reveal_secret(schema: dict[str, object], values: dict[str, object], field: str) -> Any:
    if not field or len(field) > 2000:
        raise ValueError("敏感字段名称无效")
    if field.startswith("/"):
        if re.search(r"~(?:[^01]|$)", field):
            raise ValueError("敏感字段路径无效")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in field[1:].split("/")]
    else:
        parts = [field]
    key = parts.pop(0)
    if key not in schema:
        raise ValueError("该字段不是已声明的敏感配置")
    if key not in values:
        raise KeyError(field)
    spec, value = schema[key], values[key]
    inherited_secret = secret_field(spec)
    for part in parts:
        spec = spec if isinstance(spec, dict) else {}
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(value):
                raise KeyError(field)
            value = value[int(part)]
            spec = spec.get("items") or {"properties": _children(spec)}
        elif isinstance(value, dict):
            if part not in value:
                raise KeyError(field)
            value = value[part]
            spec = _children(spec).get(part, {})
        else:
            raise KeyError(field)
        inherited_secret = inherited_secret or secret_field(spec)
    if not inherited_secret:
        raise ValueError("该字段不是已声明的敏感配置")
    if value in (None, ""):
        raise KeyError(field)
    return copy.deepcopy(value)
