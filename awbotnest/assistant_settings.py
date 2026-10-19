"""Confirmed, partial access to non-secret administrator settings.

The assistant never receives the complete settings document.  Changes are merged
on the host and passed to the same validators/persistence path as the settings UI.
Network destinations which would forward saved credentials are intentionally not
editable here.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from dataclasses import asdict

from .api.settings import current_ai_settings, save_ai_settings_value, save_system_settings
from .services.ai_protocol import API_FORMATS


_CAPABILITIES = ("text", "vision", "image", "audio")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TITLES = {"system": "系统设置", "ai": "AI 服务设置"}
_SYSTEM_LABELS = {
    "bot_name": "主要 Bot 名称", "default_bot_id": "默认 Bot",
    "web_host": "网页监听地址", "web_port": "网页端口",
    "system_update_notify_enabled": "系统更新通知", "browser_engine": "浏览器引擎",
    "log_cleaner.enabled": "自动清理日志", "log_cleaner.keep_lines": "日志保留条数",
    "log_cleaner.hour": "日志清理小时", "log_cleaner.minute": "日志清理分钟",
}


def _object(properties, required=(), description=""):
    value = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        value["required"] = list(required)
    if description:
        value["description"] = description
    return value


def _string(description="", maximum=128):
    return {"type": "string", "maxLength": maximum, "description": description}


def _array(items, description="", maximum=100):
    return {"type": "array", "items": items, "maxItems": maximum, "description": description}


def _capability_list():
    return _array({"type": "string", "enum": list(_CAPABILITIES)}, maximum=4)


def _changed(before, after, path=""):
    if type(before) is dict and type(after) is dict:
        result = []
        for key in sorted(before.keys() | after.keys()):
            result.extend(_changed(before.get(key), after.get(key), f"{path}.{key}".lstrip(".")))
        return result
    if before == after and type(before) is type(after):
        return []
    return [{"field": path, "label": _SYSTEM_LABELS.get(path, path),
             "before": copy.deepcopy(before), "after": copy.deepcopy(after)}]


class AssistantSettings:
    def __init__(self, deps):
        self.deps = deps
        self.settings = deps.settings

    def sections(self):
        return [{"section": section, "title": title} for section, title in _TITLES.items()]

    def _section(self, section):
        if section not in _TITLES:
            raise ValueError("设置分组不存在，请选择 system 或 ai")

    def state(self, section):
        """A private revision digest; secret changes also invalidate approvals."""
        self._section(section)
        encoded = json.dumps(asdict(self.settings), sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _schema(self, section):
        if section == "system":
            bot_ids = [item.id for item in self.settings.bot_specs()]
            return _object({
                "bot_name": _string("主要 Bot 显示名称，不修改 Token", 64),
                "default_bot_id": {"type": "string", "enum": list(dict.fromkeys(bot_ids)),
                                   "description": "仅可选择系统已有 Bot，不修改通知收件人"},
                "web_host": _string("监听 IP 地址或 localhost；保存后需重启，地址错误可能无法访问", 255),
                "web_port": {"type": "integer", "minimum": 1, "maximum": 65535,
                             "description": "保存后需重启；不会自动修改 Docker 端口映射"},
                "system_update_notify_enabled": {"type": "boolean", "description": "系统更新通知"},
                "browser_engine": {"type": "string", "enum": ["chromium", "cloakbrowser"],
                                   "description": "选择 CloakBrowser 时由系统安装受控依赖；不修改免费 Key"},
                "log_cleaner": _object({
                    "enabled": {"type": "boolean"},
                    "keep_lines": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                    "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                }, description="局部修改日志保留和清理时间；定时任务变更重启后生效"),
            })
        assignment = _object({"default_model": _string("模型 ID 或别名；空字符串表示取消"),
                              "fallback_model": _string("备用模型 ID 或别名；空字符串表示取消")})
        return _object({
            "providers": _array(_object({
                "id": _string("已保存的服务商 ID"), "name": _string("显示名称"),
                "enabled": {"type": "boolean"},
                "api_format": {"type": "string", "enum": sorted(API_FORMATS)},
            }, ("id",)), "按 ID 局部修改已有服务商，不替换列表；不允许修改地址或密钥"),
            "models": _array(_object({
                "id": _string("现有或新模型的唯一 ID"), "name": _string("显示名称"),
                "alias": _string("模型别名"), "enabled": {"type": "boolean"},
                "provider_id": _string("已保存的服务商 ID"),
                "model": _string("服务商要求的模型名称", 256),
                "capabilities": _capability_list(), "remove": {"type": "boolean"},
            }, ("id",)), "按 ID 增加或局部修改模型；remove=true 删除，未提及模型保持不变"),
            "capabilities": _object({key: copy.deepcopy(assignment) for key in _CAPABILITIES},
                                    description="按能力局部修改默认/备用模型"),
            "plugin_permissions": _array(_object({
                "plugin_id": _string("插件 ID"), "enabled": {"type": "boolean"},
                "capabilities": _capability_list(),
                "models": _object({key: _string("指定模型 ID 或别名；空字符串跟随系统")
                                    for key in _CAPABILITIES}),
            }, ("plugin_id",)), "按插件 ID 局部修改 AI 能力权限和模型；未提及插件保持不变"),
            "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 300},
            "image_timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 600},
            "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 20},
        })

    def _values(self, section, value=None):
        if section == "system":
            return {key: copy.deepcopy(getattr(self.settings, key))
                    for key in self._schema(section)["properties"]}
        value = current_ai_settings(self.settings) if value is None else value
        provider_keys = ("id", "name", "enabled", "api_format")
        model_keys = ("id", "name", "alias", "enabled", "provider_id", "model", "capabilities")
        providers = [{key: copy.deepcopy(item[key]) for key in provider_keys if key in item}
                     for item in value.get("providers", []) if isinstance(item, dict)]
        models = [{key: copy.deepcopy(item[key]) for key in model_keys if key in item}
                  for item in value.get("models", []) if isinstance(item, dict)]
        capabilities = {cap: {key: str(config.get(key) or "") for key in ("default_model", "fallback_model")}
                        for cap, config in value.get("capabilities", {}).items()
                        if cap in _CAPABILITIES and isinstance(config, dict)}
        permissions = []
        for plugin_id, config in value.get("plugin_permissions", {}).items():
            if not isinstance(config, dict):
                continue
            permissions.append({"plugin_id": str(plugin_id), "enabled": config.get("enabled", True),
                                "capabilities": [cap for cap in config.get("capabilities", []) if cap in _CAPABILITIES],
                                "models": {cap: str(model or "") for cap, model in (config.get("models") or {}).items()
                                           if cap in _CAPABILITIES}})
        return {"providers": providers, "models": models, "capabilities": capabilities,
                "plugin_permissions": permissions,
                **{key: value.get(key, default) for key, default in (("timeout_seconds", 60),
                    ("image_timeout_seconds", 300), ("max_concurrency", 3))}}

    def describe(self, section):
        self._section(section)
        return {"section": section, "title": _TITLES[section],
                "description": ("只修改指定字段；密钥、账号凭据、Cookie、Session、环境变量、代理、仓库和服务地址不对助手开放。"
                                "网页监听和日志定时设置保存后需重启，不会自动重启。"),
                "properties": self._schema(section)["properties"], "values": self._values(section)}

    @staticmethod
    def _unique(items, field, label):
        identifiers = [item[field] for item in items]
        if any(not _ID.fullmatch(value) for value in identifiers):
            raise ValueError(f"{label} ID 格式不正确")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"{label} ID 不能重复")

    def _merge_ai(self, changes):
        value = current_ai_settings(self.settings)
        if changes.get("plugin_permissions"):
            scan = getattr(getattr(self.deps, "runtime", None), "scan_summary", None)
            if callable(scan):
                installed = {str(item.id) for item in scan()}
                if any(item["plugin_id"] not in installed for item in changes["plugin_permissions"]):
                    raise ValueError("AI 权限对应的插件未安装")
        providers = value.setdefault("providers", [])
        for item in changes.get("providers", []):
            provider = next((source for source in providers if str(source.get("id")) == item["id"]), None)
            if provider is None:
                raise ValueError("只能修改系统已有的 AI 服务商，请先由管理员保存服务地址和密钥")
            provider.update(item)
        self._unique(changes.get("providers", []), "id", "服务商")
        self._unique(changes.get("models", []), "id", "模型")
        self._unique(changes.get("plugin_permissions", []), "plugin_id", "插件")
        models = value.setdefault("models", [])
        for item in changes.get("models", []):
            model = next((source for source in models if str(source.get("id")) == item["id"]), None)
            if item.get("remove"):
                if set(item) - {"id", "remove"}:
                    raise ValueError("删除模型不能同时修改其他字段")
                if model is None:
                    raise ValueError("待删除的模型不存在")
                models.remove(model)
                continue
            if model is None:
                if any(not item.get(key) for key in ("provider_id", "model", "capabilities")):
                    raise ValueError("新增模型必须填写 provider_id、model 和 capabilities")
                model = {"id": item["id"], "name": item.get("name") or item["id"],
                         "alias": item.get("alias") or item["id"], "enabled": True}
                models.append(model)
            model.update({key: child for key, child in item.items() if key != "remove"})
        for capability, assignment in changes.get("capabilities", {}).items():
            value.setdefault("capabilities", {}).setdefault(capability, {}).update(assignment)
        for item in changes.get("plugin_permissions", []):
            permissions = value.setdefault("plugin_permissions", {})
            permission = permissions.setdefault(item["plugin_id"], {"enabled": True,
                                                "capabilities": list(_CAPABILITIES), "models": {}})
            for key, child in item.items():
                if key == "plugin_id":
                    continue
                if key == "models":
                    permission.setdefault("models", {}).update(child)
                else:
                    permission[key] = copy.deepcopy(child)
        for key in ("timeout_seconds", "image_timeout_seconds", "max_concurrency"):
            if key in changes:
                value[key] = changes[key]
        self._validate_ai_references(value, changes)
        return value

    @staticmethod
    def _validate_ai_references(value, changes):
        providers = {str(item.get("id")): item for item in value.get("providers", [])}
        models = {str(item.get("id")): item for item in value.get("models", [])}
        aliases = {}
        for item in models.values():
            alias = str(item.get("alias") or "")
            if alias and alias in aliases and aliases[alias] is not item:
                raise ValueError("模型别名不能重复")
            if alias and alias in models and models[alias] is not item:
                raise ValueError("模型别名不能与其他模型 ID 相同")
            if alias:
                aliases[alias] = item
            if str(item.get("provider_id") or "") not in providers:
                raise ValueError("模型对应的 AI 服务商不存在")
        def assignment(model_id, capability):
            if not model_id:
                return
            model = models.get(model_id) or aliases.get(model_id)
            if not model or not model.get("enabled", True):
                raise ValueError(f"{capability} 使用的模型不存在或未启用，请同时调整能力分配")
            if capability not in model.get("capabilities", []):
                raise ValueError(f"分配的模型不支持 {capability} 能力")
            if not providers[str(model.get("provider_id"))].get("enabled", True):
                raise ValueError(f"{capability} 使用的 AI 服务商未启用，请同时调整能力分配")
        for capability, config in value.get("capabilities", {}).items():
            if capability in _CAPABILITIES and isinstance(config, dict):
                for key in ("default_model", "fallback_model"):
                    assignment(str(config.get(key) or ""), capability)
        for config in value.get("plugin_permissions", {}).values():
            if isinstance(config, dict) and config.get("enabled", True):
                for capability, model_id in (config.get("models") or {}).items():
                    if capability in config.get("capabilities", []) and capability in _CAPABILITIES:
                        assignment(str(model_id or ""), capability)

    def prepare(self, section, changes):
        self._section(section)
        from .assistant_tools import validate_tool_arguments
        changes = validate_tool_arguments(self._schema(section), changes)
        if not changes:
            raise ValueError("请提供需要修改的设置字段")
        before = self._values(section)
        if section == "system":
            after = copy.deepcopy(before)
            for key, value in changes.items():
                if key == "log_cleaner":
                    after[key] = {**after[key], **value}
                else:
                    after[key] = value
            if "web_host" in changes:
                host = changes["web_host"].strip()
                if host != "localhost":
                    try:
                        ipaddress.ip_address(host)
                    except ValueError as exc:
                        raise ValueError("监听地址必须是 IP 地址或 localhost") from exc
                changes["web_host"] = after["web_host"] = host
            if "bot_name" in changes:
                changes["bot_name"] = after["bot_name"] = changes["bot_name"].strip() or "主要 Bot"
        else:
            after = self._values(section, self._merge_ai(changes))
        diff = _changed(before, after)
        if not diff:
            raise ValueError("设置没有变化，无需保存")
        summary = f"修改{_TITLES[section]}：" + "；".join(
            f"{item['label']}：{json.dumps(item['before'], ensure_ascii=False)} → "
            f"{json.dumps(item['after'], ensure_ascii=False)}" for item in diff)
        return {"section": section, "changes": changes, "diff": diff, "summary": summary}

    async def save(self, section, changes, *, authorize=None, expected_state=None):
        if not callable(authorize):
            raise PermissionError("设置修改必须由管理员确认")
        expected_state = expected_state or self.state(section)
        def guard():
            if authorize() is not True:
                raise PermissionError("设置修改权限已撤回，请重新确认")
            if self.state(section) != expected_state:
                raise ValueError("设置在确认后发生变化，请重新确认")
            return True
        guard()
        prepared = self.prepare(section, changes)
        if section == "system":
            patch = copy.deepcopy(prepared["changes"])
            if "log_cleaner" in patch:
                patch["log_cleaner"] = {**self.settings.log_cleaner, **patch["log_cleaner"]}
            # The full UI save also accepts credentials.  Keep them on the host
            # and use its existing preserve-value mechanism, so even unusual
            # whitespace in a previously saved secret cannot be changed here.
            for key in ("api_hash", "bot_token", "ai_api_key", "webhook_secret", "api_key",
                        "github_token", "cloakbrowser_license_key", "proxy_url"):
                patch[key] = "********"
            patch["bots"] = [{"id": bot.id, "name": bot.name, "token": "********"}
                             for bot in self.settings.bots]
            result = await save_system_settings(
                self.deps, patch, before_commit=guard,
                update_fields=prepared["changes"].keys(),
            )
        else:
            value = self._merge_ai(prepared["changes"])
            result = await save_ai_settings_value(self.deps, value, before_commit=guard)
        return {**result, "section": section, "changes": prepared["diff"],
                "message": ("设置已保存；网页监听和日志定时设置需重启后生效，本次未自动重启。"
                            if result.get("restart_required") else "设置已保存。")}
