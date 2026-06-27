"""
kernel/registry.py
插件注册表：负责插件元数据解析、启用状态的持久化。

设计要点：
- 元数据来自插件文件顶层的 __plugin__ 字典，无需导入/执行插件即可读取（静态 AST 解析），
  避免「为了在前端列出插件而执行任意代码」的安全风险。
- 启用状态持久化到 data/plugins_state.json，重启后内核据此自动恢复。
"""
from __future__ import annotations

import ast
import json
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from libs.log import logger

# 插件目录与状态文件
PLUGINS_DIR = Path("plugins")
STATE_FILE = Path("data/plugins_state.json")

# __plugin__ 必填字段
REQUIRED_FIELDS = ("name", "id", "version", "scope")
# scope 合法值
VALID_SCOPES = ("user", "bot", "both")


@dataclass
class PluginMeta:
    """插件元数据（解析自 __plugin__ 字典）"""

    id: str
    name: str
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    scope: str = "user"  # user | bot | both
    default_enabled: bool = False
    config_schema: dict[str, Any] = field(default_factory=dict)

    # 运行时字段（非元数据，由内核填充）
    file: str = ""            # 相对 plugins/ 的文件名
    enabled: bool = False     # 当前是否启用
    loaded: bool = False      # 当前是否已加载到运行时
    error: Optional[str] = None  # 加载/解析错误信息（前端标红用）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PluginRegistry:
    """插件注册表（线程安全）"""

    def __init__(self, plugins_dir: Path = PLUGINS_DIR, state_file: Path = STATE_FILE):
        self.plugins_dir = plugins_dir
        self.state_file = state_file
        self._lock = threading.RLock()
        self._enabled_state: dict[str, bool] = {}
        self._config_state: dict[str, dict[str, Any]] = {}
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_state()

    # ──────────────────────────────────────────────
    # 状态持久化
    # ──────────────────────────────────────────────
    def _load_state(self) -> None:
        """从磁盘读取启用状态与插件配置"""
        if not self.state_file.exists():
            self._enabled_state = {}
            self._config_state = {}
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._enabled_state = data.get("enabled", {})
            self._config_state = data.get("config", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取插件状态文件失败，将重置：%s", e)
            self._enabled_state = {}
            self._config_state = {}

    def _save_state(self) -> None:
        """写回磁盘"""
        try:
            payload = {"enabled": self._enabled_state, "config": self._config_state}
            self.state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error("写入插件状态文件失败：%s", e)

    # ──────────────────────────────────────────────
    # 元数据解析（静态 AST，不执行插件代码）
    # ──────────────────────────────────────────────
    def entry_file(self, plugin_id: str) -> Optional[Path]:
        """
        解析插件入口文件，支持两种形态：
          - 单文件：plugins/<id>.py
          - 文件夹：plugins/<id>/__init__.py
        返回入口 .py 路径；都不存在返回 None。
        """
        single = self.plugins_dir / f"{plugin_id}.py"
        if single.exists():
            return single
        pkg_init = self.plugins_dir / plugin_id / "__init__.py"
        if pkg_init.exists():
            return pkg_init
        return None

    def is_package_plugin(self, plugin_id: str) -> bool:
        """该插件是否为文件夹形态"""
        return (self.plugins_dir / plugin_id / "__init__.py").exists()

    def parse_meta(self, file_path: Path, plugin_id: Optional[str] = None) -> PluginMeta:
        """
        静态解析插件入口文件的 __plugin__ 元数据。
        plugin_id 未指定时：文件夹形态用父目录名，单文件用文件名。
        解析失败或字段缺失时，返回带 error 的 PluginMeta（不抛异常）。
        """
        if plugin_id is None:
            plugin_id = file_path.parent.name if file_path.name == "__init__.py" else file_path.stem
        # file 字段：文件夹形态显示 <id>/，单文件显示文件名
        rel = f"{plugin_id}/" if file_path.name == "__init__.py" else file_path.name
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
        except (OSError, SyntaxError) as e:
            return PluginMeta(
                id=plugin_id, name=plugin_id, file=rel,
                error=f"解析失败: {e.__class__.__name__}: {e}",
            )

        raw = self._extract_plugin_dict(tree)
        if raw is None:
            return PluginMeta(
                id=plugin_id, name=plugin_id, file=rel,
                error="缺少 __plugin__ 元数据字典",
            )

        # 校验必填字段
        missing = [f for f in REQUIRED_FIELDS if f not in raw]
        if missing:
            return PluginMeta(
                id=plugin_id, name=raw.get("name", plugin_id), file=rel,
                error=f"__plugin__ 缺少必填字段: {', '.join(missing)}",
            )

        scope = raw.get("scope", "user")
        if scope not in VALID_SCOPES:
            return PluginMeta(
                id=plugin_id, name=raw.get("name", plugin_id), file=rel,
                error=f"scope 非法: {scope}（应为 {'/'.join(VALID_SCOPES)}）",
            )

        # ID 必须与文件名/目录名一致（单文件单插件约定）
        if raw.get("id") != plugin_id:
            return PluginMeta(
                id=plugin_id, name=raw.get("name", plugin_id), file=rel,
                error=f"__plugin__['id']({raw.get('id')}) 必须等于文件名/目录名({plugin_id})",
            )


        meta = PluginMeta(
            id=plugin_id,
            name=raw.get("name", plugin_id),
            version=str(raw.get("version", "0.0.0")),
            author=raw.get("author", ""),
            description=raw.get("description", ""),
            scope=scope,
            default_enabled=bool(raw.get("default_enabled", False)),
            config_schema=raw.get("config_schema", {}) or {},
            file=rel,
        )
        # 填充启用状态：已有记录优先，否则用 default_enabled
        meta.enabled = self._enabled_state.get(plugin_id, meta.default_enabled)
        return meta

    @staticmethod
    def _extract_plugin_dict(tree: ast.Module) -> Optional[dict[str, Any]]:
        """从 AST 中找到 __plugin__ = {...} 并安全求值（仅字面量）"""
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__plugin__":
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:  # noqa: BLE001 - 畸形字面量(含 TypeError/RecursionError 等)不应崩 scan
                        return None
        return None

    @staticmethod
    def _has_setup(tree: ast.Module) -> bool:
        """文件顶层是否定义了 setup 函数（async 或普通）"""
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "setup":
                return True
        return False

    def _is_plugin_candidate(self, file_path: Path) -> bool:
        """
        判断一个 .py 是否「打算成为插件」：有 __plugin__ 或 setup 之一即算。
        二者皆无的视为普通辅助模块（如 base.py），不纳入插件列表。
        """
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        except (OSError, SyntaxError):
            return True  # 解析失败仍当候选，让 parse_meta 报错给用户看
        return self._extract_plugin_dict(tree) is not None or self._has_setup(tree)

    # ──────────────────────────────────────────────
    # 扫描
    # ──────────────────────────────────────────────
    def scan(self) -> list[PluginMeta]:
        """
        扫描 plugins/ 下的插件，支持两种形态：
          - 单文件：plugins/<id>.py
          - 文件夹：plugins/<id>/__init__.py
        忽略 _ 开头的文件/目录（如 _TEMPLATE.py、_helpers）。
        """
        with self._lock:
            metas: list[PluginMeta] = []
            seen_ids: set[str] = set()

            # 单文件
            for f in sorted(self.plugins_dir.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                if not self._is_plugin_candidate(f):
                    continue
                pid = f.stem
                seen_ids.add(pid)
                metas.append(self.parse_meta(f, pid))

            # 文件夹形态：plugins/<id>/__init__.py
            for d in sorted(self.plugins_dir.iterdir()):
                if not d.is_dir() or d.name.startswith("_") or d.name == "__pycache__":
                    continue
                init = d / "__init__.py"
                if not init.exists():
                    continue
                if d.name in seen_ids:
                    continue  # 同名单文件优先
                if not self._is_plugin_candidate(init):
                    continue
                metas.append(self.parse_meta(init, d.name))

            return sorted(metas, key=lambda m: m.id)

    def get_meta(self, plugin_id: str) -> Optional[PluginMeta]:
        """读取单个插件元数据（单文件或文件夹形态）"""
        entry = self.entry_file(plugin_id)
        if entry is None:
            return None
        return self.parse_meta(entry, plugin_id)

    # ──────────────────────────────────────────────
    # 启用状态读写
    # ──────────────────────────────────────────────
    def is_enabled(self, plugin_id: str) -> bool:
        with self._lock:
            return self._enabled_state.get(plugin_id, False)

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        with self._lock:
            self._enabled_state[plugin_id] = enabled
            self._save_state()

    def enabled_ids(self) -> list[str]:
        """返回当前标记为启用的插件 ID 列表"""
        with self._lock:
            return [pid for pid, on in self._enabled_state.items() if on]

    # ──────────────────────────────────────────────
    # 插件配置读写（对应 config_schema）
    # ──────────────────────────────────────────────
    def get_config(self, plugin_id: str) -> dict[str, Any]:
        """
        返回插件当前配置：以 config_schema 的 default 为底，叠加用户已保存的值。
        """
        with self._lock:
            meta = self.get_meta(plugin_id)
            defaults: dict[str, Any] = {}
            if meta and meta.config_schema:
                for key, spec in meta.config_schema.items():
                    if isinstance(spec, dict) and "default" in spec:
                        defaults[key] = spec["default"]
            saved = self._config_state.get(plugin_id, {})
            return {**defaults, **saved}

    def set_config(self, plugin_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            self._config_state[plugin_id] = values
            self._save_state()

    def remove(self, plugin_id: str) -> None:
        """从状态中移除插件记录（删除插件时调用）"""
        with self._lock:
            self._enabled_state.pop(plugin_id, None)
            self._config_state.pop(plugin_id, None)
            self._save_state()


# 全局单例
registry = PluginRegistry()
