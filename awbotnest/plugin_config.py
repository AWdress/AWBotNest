"""Schema-aware secret presentation and JSON Pointer field access."""
from __future__ import annotations

import copy
import json
import re
from typing import Any

MASK = "********"


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
