"""Safe assistant access to existing plugin actions and declared configuration.

Plugins do not need an AI-specific adapter. Only the already registered action
table and configuration schema are used, never arbitrary APIs or Python methods.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .plugin_config import save_plugin_config, secret_field


_PRIVATE_HINTS = re.compile(
    r"password|passwd|secret|token|api[ _-]?key|license[ _-]?key|cookies?|session|api[ _-]?hash|"
    r"credential|authorization|aes[ _-]?key|private[ _-]?key|access[ _-]?key|bearer|"
    r"密码|密碼|口令|密钥|秘钥|凭据|凭证|验证码|授权码|访问令牌", re.I,
)
_DESTINATION = re.compile(
    r"(?:^|[^a-z])(?:urls?|uris?|hosts?|hostname|proxy|proxies|webhooks?|callbacks?|endpoints?|"
    r"servers?|domains?|headers?|commands?|cmd|scripts?|code|exec|execute|shell|python|"
    r"environment|environ|env|paths?|executable|binary|entrypoint|working_dir|cwd|module|address|ports?)(?:$|[^a-z])|"
    r"地址|域名|代理|回调|服务端|端点|请求头|脚本|命令|执行入口|目录|路径|环境变量", re.I,
)


def _described_secret(spec, name="") -> bool:
    if not isinstance(spec, dict):
        return True
    text = " ".join(str(spec.get(key) or "") for key in
                    ("type", "format", "inputType", "input_type", "widget", "label", "title", "description", "help"))
    return secret_field(spec) or bool(_PRIVATE_HINTS.search(name) or _PRIVATE_HINTS.search(text))


def _has_sensitive_fields(spec, name="", depth=0) -> bool:
    if depth > 8 or _described_secret(spec, name):
        return True
    children = spec.get("fields", spec.get("properties", {}))
    if isinstance(children, dict) and any(_has_sensitive_fields(child, str(key), depth + 1)
                                          for key, child in children.items()):
        return True
    return "items" in spec and _has_sensitive_fields(spec["items"], "", depth + 1)


def _has_stored_sensitive_fields(value, depth=0) -> bool:
    if depth > 12:
        return True
    if isinstance(value, dict):
        return any(_PRIVATE_HINTS.search(str(key)) or _has_stored_sensitive_fields(child, depth + 1)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_has_stored_sensitive_fields(child, depth + 1) for child in value)
    return False


def _credential_destination(spec, name="") -> bool:
    if not isinstance(spec, dict):
        return True
    text = " ".join([name, *(str(spec.get(key) or "") for key in
                    ("type", "format", "widget", "label", "title", "description", "help"))])
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return bool(_DESTINATION.search(text))


def _has_restricted_fields(spec, name="", depth=0) -> bool:
    if depth > 8 or _described_secret(spec, name) or _credential_destination(spec, name):
        return True
    children = spec.get("fields", spec.get("properties", {}))
    if isinstance(children, dict) and any(_has_restricted_fields(child, str(key), depth + 1)
                                          for key, child in children.items()):
        return True
    return "items" in spec and _has_restricted_fields(spec["items"], "", depth + 1)


def _field_spec(schema, parts):
    """Resolve schema paths, including list row indices, without reading secrets."""
    spec = {"type": "object", "properties": schema}
    for part in parts:
        kind = spec.get("type")
        if kind in {"array", "list", "multiselect"}:
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise ValueError("配置列表路径无效")
            children = spec.get("fields", spec.get("properties", {}))
            spec = spec.get("items") or {"type": "object", "properties": children}
        else:
            children = spec.get("fields", spec.get("properties", {}))
            spec = children.get(part) if isinstance(children, dict) else None
        if not isinstance(spec, dict):
            raise ValueError("配置字段未声明")
    return spec


def _parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path or len(path) > 1000:
        raise ValueError("配置字段路径无效")
    if not path.startswith("/"):
        return [path]
    if re.search(r"~(?:[^01]|$)", path):
        raise ValueError("配置字段路径无效")
    result = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    if not result or any(not part or part.startswith("__") for part in result):
        raise ValueError("配置字段路径无效")
    return result


def _pointer(parts: list[str]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _read(values, parts):
    value = values
    for part in parts:
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(value):
                return False, None
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return False, None
    return True, value


def _write(values, parts, value):
    parent = values
    for part in parts[:-1]:
        if isinstance(parent, list):
            parent = parent[int(part)]
        else:
            if part not in parent:
                parent[part] = {}
            parent = parent[part]
        if not isinstance(parent, (dict, list)):
            raise ValueError("配置字段的上级不是对象或列表")
    key = parts[-1]
    if isinstance(parent, list):
        parent[int(key)] = copy.deepcopy(value)
    else:
        parent[key] = copy.deepcopy(value)


class AssistantPlugins:
    def __init__(self, deps):
        self.deps = deps

    def actions(self, meta) -> list[dict[str, Any]]:
        from .assistant_tools import _SENSITIVE
        describe = getattr(self.deps.routes, "describe_actions", None)
        if not callable(describe):
            return []
        labels = {}
        restricted_actions = set()
        def buttons(fields, depth=0):
            if not isinstance(fields, dict) or depth > 8:
                return
            for key, spec in fields.items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("type") == "action":
                    name = str(spec.get("action") or key)
                    labels.setdefault(name, str(spec.get("label") or spec.get("title") or key))
                    if _described_secret(spec, str(key)):
                        restricted_actions.add(name)
                buttons(spec.get("fields", spec.get("properties")), depth + 1)
        buttons(getattr(meta, "config_schema", None) or {})
        result = []
        for original in describe(meta.id):
            item = copy.deepcopy(original)
            if (not isinstance(item.get("name"), str) or _SENSITIVE.search(item["name"])
                    or item["name"] in restricted_actions
                    or _PRIVATE_HINTS.search(str(item.get("description") or ""))
                    or _has_sensitive_fields(item.get("parameters"))):
                continue
            item["description"] = item.get("description") or labels.get(item["name"]) or item["name"]
            # Existing plugins need no opt-in. Undeclared legacy actions remain
            # writes even if their historical read_only flag was optimistic.
            item["read_only"] = item.get("read_only") is True and item.get("agent_exposed") is True
            item["parameter_status"] = ("declared" if item.get("parameters_declared", item.get("agent_exposed"))
                                        else "legacy_empty_payload")
            result.append(item)
        return result

    def validate_action(self, meta, name: str, payload: Any) -> dict[str, Any]:
        from .assistant_tools import validate_tool_arguments
        item = next((item for item in self.actions(meta) if item["name"] == name), None)
        if item is None:
            raise ValueError("插件未注册此动作，或动作需要敏感参数")
        if item["parameter_status"] == "legacy_empty_payload" and payload:
            raise ValueError("该按钮未声明额外参数；可按网页按钮传空对象执行，不要猜测参数")
        return validate_tool_arguments(item["parameters"], payload)

    def config_catalog(self, meta) -> dict[str, Any]:
        from .assistant_tools import AssistantTools, _object, validate_tool_arguments, validate_tool_schema
        schema = getattr(meta, "config_schema", None) or {}
        values = self.deps.settings.plugin_config.get(meta.id, {})
        credentialed = (_has_sensitive_fields({"type": "object", "properties": schema})
                        or _has_stored_sensitive_fields(values))
        fields = []
        omitted = False
        def visit(spec, parts, label, depth=0):
            nonlocal omitted
            if not isinstance(spec, dict) or depth > 8 or len(fields) >= 300:
                omitted = True
                return
            if _described_secret(spec, parts[-1]) or credentialed and _credential_destination(spec, parts[-1]):
                omitted = True
                return
            if spec.get("type") in {"info", "action"} or any(spec.get(key) for key in ("readonly", "read_only", "disabled")):
                return
            children = spec.get("fields", spec.get("properties", {}))
            kind = spec.get("type") or ("object" if children else "string")
            present, current = _read(values, parts)
            if kind == "object" and isinstance(children, dict) and children:
                for key, child in children.items():
                    child_label = str(child.get("label") or child.get("title") or key) if isinstance(child, dict) else str(key)
                    visit(child, parts + [str(key)], f"{label} / {child_label}", depth + 1)
                return
            if kind in {"array", "list"} and (
                    _has_sensitive_fields(spec) or credentialed and _has_restricted_fields(spec)):
                omitted = True
                item_spec = spec.get("items") or {"type": "object", "properties": children}
                if isinstance(current, list):
                    for index, _ in enumerate(current):
                        visit(item_spec, parts + [str(index)], f"{label} [{index + 1}]", depth + 1)
                return
            if _has_sensitive_fields(spec) or credentialed and _has_restricted_fields(spec):
                omitted = True
                return
            try:
                normalized = validate_tool_schema(_object({"value": AssistantTools._config_spec(spec)}))["properties"]["value"]
            except ValueError:
                omitted = True
                return
            if present and isinstance(current, (dict, list)):
                try:
                    validate_tool_arguments(_object({"value": normalized}), {"value": current})
                except ValueError:
                    # Vue/legacy storage may carry undeclared fields, including
                    # credentials. Never expose or replace that whole container.
                    omitted = True
                    if kind in {"array", "list"} and isinstance(current, list):
                        item_spec = spec.get("items") or {"type": "object", "properties": children}
                        for index, _ in enumerate(current):
                            visit(item_spec, parts + [str(index)], f"{label} [{index + 1}]", depth + 1)
                    return
            field = {"path": _pointer(parts), "label": label, "schema": normalized, "value_set": present}
            if present:
                try:
                    validate_tool_arguments(_object({"value": normalized}), {"value": current})
                    field["value"] = copy.deepcopy(current)
                except ValueError:
                    field["unavailable"] = True
            elif "default" in spec:
                try:
                    validate_tool_arguments(_object({"value": normalized}), {"value": spec["default"]})
                    field["value"] = copy.deepcopy(spec["default"])
                except ValueError:
                    field["unavailable"] = True
            fields.append(field)
        if isinstance(schema, dict):
            for key, spec in schema.items():
                label = str(spec.get("label") or spec.get("title") or key) if isinstance(spec, dict) else str(key)
                visit(spec, [str(key)], label)
        return {"fields": fields, "omitted_sensitive": omitted}

    def prepare_config_patch(self, meta, changes: dict[str, Any]) -> dict[str, Any]:
        from .assistant_tools import _json_copy, validate_tool_arguments
        changes = _json_copy(changes)
        if not isinstance(changes, dict) or not changes or len(changes) > 50:
            raise ValueError("配置修改必须包含 1 至 50 个字段")
        catalog = {field["path"]: field for field in self.config_catalog(meta)["fields"]}
        values = copy.deepcopy(self.deps.settings.plugin_config.get(meta.id, {}))
        diff, paths = [], []
        for key, value in changes.items():
            parts = _parts(key)
            pointer = _pointer(parts)
            if any(parts[:len(previous)] == previous or previous[:len(parts)] == parts for previous in paths):
                raise ValueError("配置修改不能重复或包含相互覆盖的路径")
            field = catalog.get(pointer)
            if field is None:
                raise ValueError("配置字段未声明、不可修改或包含敏感值，请在设置页面处理")
            validated = validate_tool_arguments({"type": "object", "properties": {"value": field["schema"]},
                                                  "required": ["value"], "additionalProperties": False}, {"value": value})["value"]
            self._validate_original(_field_spec(meta.config_schema or {}, parts), validated, pointer)
            present, before = _read(values, parts)
            if present and type(before) is type(validated) and before == validated:
                continue
            _write(values, parts, validated)
            paths.append(parts)
            change = {"field": pointer,
                      "before": "（当前值格式不正确）" if field.get("unavailable") else copy.deepcopy(
                          before if present else field.get("value")),
                      "after": copy.deepcopy(validated)}
            if not present:
                change["before_source"] = "default" if "value" in field else "unset"
            diff.append(change)
        if not diff:
            raise ValueError("配置没有变化")
        self.deps.runtime.validate_config(meta.config_schema or {}, values, allow_extra=meta.render_mode == "vue")
        return {"values": values, "changes": diff}

    def _validate_original(self, spec, value, path, depth=0):
        if depth > 8:
            raise ValueError("配置定义层级过深")
        self.deps.runtime.validate_config({path: spec}, {path: value})
        children = spec.get("fields", spec.get("properties", {}))
        if isinstance(value, dict) and isinstance(children, dict):
            self.deps.runtime.validate_config(children, value)
            for key, child in children.items():
                if key in value and isinstance(child, dict):
                    self._validate_original(child, value[key], f"{path}/{key}", depth + 1)
        elif isinstance(value, list):
            item_spec = spec.get("items") or ({"type": "object", "properties": children} if children else None)
            if isinstance(item_spec, dict):
                for index, item in enumerate(value):
                    self._validate_original(item_spec, item, f"{path}/{index}", depth + 1)

    def _reload_state(self, meta):
        runtime, settings = self.deps.runtime, self.deps.settings
        current = next((item for item in runtime.scan() if item.id == meta.id), None)
        if current is None:
            raise ValueError("插件已移除，请重新确认")
        result = {"schema": copy.deepcopy(current.config_schema or {}),
                  "version": str(getattr(current, "version", "")),
                  "config": copy.deepcopy(settings.plugin_config.get(meta.id, {})),
                  "accounts": copy.deepcopy(getattr(settings, "plugin_accounts", {}).get(meta.id, [])),
                  "bot_routing": copy.deepcopy(getattr(settings, "bot_routing", {}).get(meta.id)),
                  "enabled": meta.id in settings.enabled_plugins}
        if callable(getattr(runtime, "entry_file", None)) and hasattr(runtime, "plugins_dir"):
            from .assistant_maintenance import AssistantMaintenance
            result["source"] = AssistantMaintenance(self.deps, settings=lambda: {})._source(meta.id)
        return result

    async def save_config_patch(self, meta, changes, *, before_change=None, authorize=None):
        prepared = self.prepare_config_patch(meta, changes)
        before = self._reload_state(meta)
        expected = {**before, "config": copy.deepcopy(prepared["values"])}
        def recheck_modified():
            if self._reload_state(meta) != expected:
                raise ValueError("插件源码、配置或绑定已变化，修改已中止，请重新确认")
        def recheck_recovery():
            if self._reload_state(meta) != before:
                raise ValueError("插件源码或绑定已变化，原配置已恢复，插件保持停用")
        # The prepared full values never go to the model or approval message.
        await save_plugin_config(self.deps, meta.id, prepared["values"], before_change=before_change,
                                 after_change=recheck_modified, before_recover=recheck_recovery, authorize=authorize)
        return {"ok": True, "plugin_id": meta.id, "changes": prepared["changes"], "reloaded": meta.id in self.deps.runtime.loaded}
