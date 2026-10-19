"""Confirmed management of installed plugins using the ordinary lifecycle lock."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from itertools import islice
from pathlib import Path


def _digest(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("插件配置格式不正确，无法确认管理操作") from None
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("插件配置过大，无法确认管理操作")
    return hashlib.sha256(encoded).hexdigest()


class AssistantMaintenance:
    def __init__(self, deps, *, settings, clean=None):
        self.deps, self.runtime, self.settings = deps, deps.runtime, settings
        self.clean = clean or (lambda value, limit=1000: str(value)[:limit])
        self._used = {}

    def _authorized(self):
        current = self.settings()
        if current.get("enabled") is not True or current.get("allow_actions") is not True:
            raise PermissionError("助手执行操作已关闭")
        if not callable(getattr(self.runtime, "manage_guarded", None)):
            raise PermissionError("系统暂不支持原子校验插件管理操作")

    def _source(self, plugin_id):
        entry = self.runtime.entry_file(plugin_id)
        if entry is None:
            raise ValueError("插件已移除，请重新检查插件列表")
        entry = Path(entry)
        root = Path(self.runtime.plugins_dir).resolve()
        try:
            resolved = entry.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("插件入口不在已安装插件目录内")
            if entry.is_symlink():
                raise ValueError("插件入口不能是符号链接")
            candidates = sorted(islice(entry.parent.rglob("*.py"), 257)) if entry.name == "__init__.py" else [entry]
            if len(candidates) > 256:
                raise ValueError("插件文件过多，无法完整确认管理操作")
            digest, total = hashlib.sha256(), 0
            for source in candidates:
                resolved_source = source.resolve(strict=True)
                if not resolved_source.is_relative_to(root):
                    raise ValueError("插件源码不在已安装插件目录内")
                relative = resolved_source.relative_to(root)
                if source.is_symlink():
                    raise ValueError("插件源码不能包含符号链接")
                size = source.stat().st_size
                total += size
                if total > 8 * 1024 * 1024:
                    raise ValueError("插件源码过大，无法完整确认管理操作")
                content = source.read_bytes()
                if len(content) != size:
                    raise ValueError("插件文件正在变化，请稍后重新发起")
                digest.update(str(relative).replace("\\", "/").encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(content).digest())
            return {"entry": str(resolved.relative_to(root)).replace("\\", "/"), "digest": digest.hexdigest()}
        except OSError:
            raise ValueError("插件文件不可读取或已变化，请重新发起操作") from None

    def _snapshot(self, plugin_id):
        scan = getattr(self.runtime, "scan_summary", None) or self.runtime.scan
        meta = next((item for item in scan() if item.id == plugin_id), None)
        if meta is None:
            raise ValueError("插件未安装，助手不会自动安装或下载插件")
        loaded = self.runtime.loaded.get(plugin_id)
        generation = getattr(loaded, "generation", None) if loaded is not None else None
        if loaded is not None and not isinstance(generation, str):
            raise PermissionError("插件缺少运行代次，无法安全确认，请重新启动系统")
        config = self.deps.settings
        return {"name": self.clean(meta.name, 200), "version": str(meta.version),
                "loaded_version": str(loaded.meta.version) if loaded is not None else None,
                "enabled": plugin_id in config.enabled_plugins, "loaded": loaded is not None,
                "generation": generation, "source": self._source(plugin_id),
                "config": _digest({"config": config.plugin_config.get(plugin_id, {}),
                                   "accounts": config.plugin_accounts.get(plugin_id, []),
                                   "bot_routing": getattr(config, "bot_routing", {}).get(plugin_id),
                                   "scope": getattr(meta, "scope", None), "bot": getattr(meta, "bot", None)})}

    def prepare(self, plugin_id, operation):
        self._authorized()
        if not isinstance(plugin_id, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,127}", plugin_id):
            raise ValueError("插件 ID 不正确")
        if operation not in {"enable", "disable", "reload"}:
            raise ValueError("助手仅支持启用、停用和重新加载已安装插件")
        state = self._snapshot(plugin_id)
        if operation == "enable" and state["loaded"]:
            raise ValueError("插件已运行，无需再次启用")
        if operation == "disable" and not state["loaded"] and not state["enabled"]:
            raise ValueError("插件已停用")
        if operation == "reload" and not state["loaded"]:
            raise ValueError("插件未运行，请使用启用操作")
        labels = {"enable": "启用", "disable": "停用", "reload": "重新加载"}
        summary = f"{labels[operation]}「{state['name']}」（{plugin_id}，版本 {state['version']}）"
        if operation in {"disable", "reload"}:
            summary += "。正在运行的任务可能会中断。"
        arguments = {"plugin_id": plugin_id, "operation": operation}
        return {"name": "manage_plugin", "arguments": arguments, "summary": summary,
                "read_only": False, "fingerprint": _digest({"arguments": arguments, "state": state}),
                "_state": state}

    async def execute(self, prepared):
        if not isinstance(prepared, dict) or prepared.get("name") != "manage_plugin":
            raise ValueError("插件管理请求不正确")
        expiry = prepared.get("_approval_expires")
        if type(expiry) not in {int, float} or not math.isfinite(expiry) or time.time() >= expiry:
            raise PermissionError("确认缺失或已过期，未执行插件管理操作")
        arguments = prepared.get("arguments")
        if not isinstance(arguments, dict) or set(arguments) != {"plugin_id", "operation"}:
            raise ValueError("插件管理参数不正确")
        current = self.prepare(arguments["plugin_id"], arguments["operation"])
        if any(prepared.get(key) != current[key] for key in ("fingerprint", "summary", "read_only", "_state")):
            raise ValueError("插件、配置或运行状态已变化，请重新确认")
        key = _digest({"fingerprint": current["fingerprint"], "expiry": expiry})
        self._used = {key: expires for key, expires in self._used.items() if expires > time.time()}
        if key in self._used:
            raise PermissionError("本次插件管理确认已使用，不会重复执行")
        if len(self._used) >= 1024:
            raise ValueError("待过期的管理确认较多，请稍后再试")
        # Claim before waiting on the runtime lock; cancellation/failure never retries.
        self._used[key] = expiry
        plugin_id, operation, expected = arguments["plugin_id"], arguments["operation"], current["_state"]

        def check(stage):
            self._authorized()
            if time.time() >= expiry:
                raise PermissionError("确认已过期，未继续执行插件管理操作")
            state = self._snapshot(plugin_id)
            if operation == "reload" and stage == "enable":
                # Only the disable phase approved by this request may change
                # loaded/generation; configuration/source/desired enable state stay fixed.
                if state["loaded"]:
                    raise ValueError("插件运行状态已变化，请重新确认")
                state = {**state, "loaded": expected["loaded"], "generation": expected["generation"],
                         "loaded_version": expected["loaded_version"]}
            if state != expected:
                raise ValueError("插件、配置或运行状态已变化，请重新确认")

        try:
            meta = await self.runtime.manage_guarded(plugin_id, operation, before_change=check)
        except (PermissionError, ValueError):
            raise
        except Exception:
            raise RuntimeError("插件管理未完成，未自动重试。请检查插件状态和系统日志。") from None
        if meta is not None and getattr(meta, "error", ""):
            raise RuntimeError("插件未能启动，未自动重试。请检查插件状态和系统日志。")
        loaded = self.runtime.loaded.get(plugin_id)
        return {"plugin_id": plugin_id, "operation": operation,
                "enabled": plugin_id in self.deps.settings.enabled_plugins, "loaded": loaded is not None,
                "version": str(loaded.meta.version) if loaded is not None else expected["version"], "ok": True}
