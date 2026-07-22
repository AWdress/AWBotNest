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
import copy
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
# 配置界面渲染方式合法值
VALID_RENDER_MODES = ("schema", "vue")
# 插件自带前端（vue 模式）联邦产物约定路径（相对插件目录）
FRONTEND_DIST = "frontend/dist"
FRONTEND_ENTRY = "frontend/dist/assets/remoteEntry.js"


@dataclass
class PluginMeta:
    """插件元数据（解析自 __plugin__ 字典）"""

    id: str
    name: str
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    changelog: str = ""  # 可选：面向用户的版本更新说明
    icon: str = ""       # 可选：插件图标 URL，前端卡片展示；空则回退平台 logo
    scope: str = "user"  # user | bot | both
    default_enabled: bool = False
    webhook: bool = False  # 是否提供 HTTP webhook 入站端点（配 ctx.on_webhook 使用）
    # 配置界面渲染方式：
    #   "schema" —— 默认，平台按 config_schema 自动生成表单（声明式）
    #   "vue"    —— 插件自带 Vue 联邦组件，平台运行时加载其 ./Config 组件（仅目录包）
    render_mode: str = "schema"
    config_schema: dict[str, Any] = field(default_factory=dict)
    requirements: list[str] = field(default_factory=list)  # 第三方依赖(PEP 508)，启用时由平台代装

    # 运行时字段（非元数据，由内核填充）
    file: str = ""            # 相对 plugins/ 的文件名
    enabled: bool = False     # 当前是否启用
    loaded: bool = False      # 当前是否已加载到运行时
    accounts: list[str] = field(default_factory=list)  # 应用到哪些账号(session名)；空=全部用户账号
    bot: str = ""             # 通知推送 + bot/both handler 用哪个 Bot(id)；空=默认 Bot
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
        self._account_scope: dict[str, list[str]] = {}  # 插件id -> [session名]，空/缺失=全部
        self._bot_choice: dict[str, str] = {}  # 插件id -> bot id；空/缺失=跟随默认，"default"=内置 Bot
        self._scan_cache_signature: tuple[tuple[str, int, int], ...] | None = None
        self._scan_cache: list[PluginMeta] = []
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
            self._account_scope = {}
            self._bot_choice = {}
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._enabled_state = data.get("enabled", {})
            self._config_state = data.get("config", {})
            self._account_scope = data.get("account_scope", {})
            self._bot_choice = data.get("bot_choice", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取插件状态文件失败，将重置：%s", e)
            self._enabled_state = {}
            self._config_state = {}
            self._account_scope = {}
            self._bot_choice = {}

    def _save_state(self) -> None:
        """写回磁盘"""
        try:
            payload = {"enabled": self._enabled_state, "config": self._config_state,
                       "account_scope": self._account_scope, "bot_choice": self._bot_choice}
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

    def frontend_dist_dir(self, plugin_id: str) -> Path:
        """vue 模式插件的前端构建产物目录 plugins/<id>/frontend/dist/。"""
        return self.plugins_dir / plugin_id / FRONTEND_DIST

    def has_frontend(self, plugin_id: str) -> bool:
        """vue 模式插件是否已构建出联邦入口（frontend/dist/assets/remoteEntry.js）。
        未构建时前端应提示「插件未随附前端构建产物」，而非白屏。"""
        return (self.plugins_dir / plugin_id / FRONTEND_ENTRY).exists()

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

        render_mode = str(raw.get("render_mode", "schema") or "schema")
        if render_mode not in VALID_RENDER_MODES:
            return PluginMeta(
                id=plugin_id, name=raw.get("name", plugin_id), file=rel,
                error=f"render_mode 非法: {render_mode}（应为 {'/'.join(VALID_RENDER_MODES)}）",
            )
        # vue 模式需目录包形态（自带 frontend 构建产物），单文件放不下前端工程
        if render_mode == "vue" and file_path.name != "__init__.py":
            return PluginMeta(
                id=plugin_id, name=raw.get("name", plugin_id), file=rel,
                error="render_mode=vue 仅支持目录包插件（需自带 frontend/ 前端工程）",
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
            changelog=str(raw.get("changelog", "") or ""),
            icon=raw.get("icon", "") or "",
            scope=scope,
            default_enabled=bool(raw.get("default_enabled", False)),
            webhook=bool(raw.get("webhook", False)),
            render_mode=render_mode,
            config_schema=raw.get("config_schema", {}) or {},
            requirements=self._coerce_requirements(raw.get("requirements")),
            file=rel,
        )
        # 填充启用状态：已有记录优先，否则用 default_enabled
        meta.enabled = self._enabled_state.get(plugin_id, meta.default_enabled)
        meta.accounts = list(self._account_scope.get(plugin_id, []))
        meta.bot = self._bot_choice.get(plugin_id, "") or ""
        return meta

    @staticmethod
    def _coerce_requirements(raw: Any) -> list[str]:
        """把 __plugin__['requirements'] 规整成字符串列表。
        接受 list[str]；其它形态（含字符串、None）一律返回空列表，避免畸形声明崩 scan。
        具体的 PEP 508 合法性校验留给启用时的 deps.ensure。"""
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if isinstance(x, str) and x.strip()]

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
    def _entry_signature(self) -> tuple[tuple[str, int, int], ...]:
        """用入口文件路径、修改时间和大小判断插件元数据是否变化。"""
        entries: list[Path] = [
            path for path in self.plugins_dir.glob("*.py")
            if not path.name.startswith("_")
        ]
        entries.extend(
            init for directory in self.plugins_dir.iterdir()
            if directory.is_dir() and not directory.name.startswith("_")
            and directory.name != "__pycache__"
            and (init := directory / "__init__.py").exists()
        )
        signature = []
        for entry in sorted(entries):
            try:
                stat = entry.stat()
                signature.append((str(entry), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(signature)

    def _cached_metas(self) -> list[PluginMeta]:
        """返回可安全修改的副本，并填入最新启用、账号和路由状态。"""
        metas = copy.deepcopy(self._scan_cache)
        for meta in metas:
            meta.enabled = self._enabled_state.get(meta.id, meta.default_enabled)
            meta.accounts = list(self._account_scope.get(meta.id, []))
            meta.bot = self._bot_choice.get(meta.id, "") or ""
        return metas

    def scan(self) -> list[PluginMeta]:
        """
        扫描 plugins/ 下的插件，支持两种形态：
          - 单文件：plugins/<id>.py
          - 文件夹：plugins/<id>/__init__.py
        忽略 _ 开头的文件/目录（如 _TEMPLATE.py、_helpers）。
        """
        with self._lock:
            signature = self._entry_signature()
            if signature == self._scan_cache_signature:
                return self._cached_metas()

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

            self._scan_cache = sorted(metas, key=lambda m: m.id)
            self._scan_cache_signature = signature
            return self._cached_metas()

    def get_meta(self, plugin_id: str) -> Optional[PluginMeta]:
        """读取单个插件元数据（单文件或文件夹形态）"""
        entry = self.entry_file(plugin_id)
        if entry is None:
            return None
        return self.parse_meta(entry, plugin_id)

    def display_name(self, plugin_id: str) -> str:
        """插件展示名：优先中文元数据 name，取不到（文件已删/解析失败）回退 id。
        用于日志、通知等给人看的场景。"""
        try:
            meta = self.get_meta(plugin_id)
            if meta and meta.name:
                return meta.name
        except Exception:
            pass
        return plugin_id

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

    def get_account_scope(self, plugin_id: str) -> list[str]:
        """返回插件应用到的账号 session 列表；空列表表示全部用户账号。"""
        with self._lock:
            return list(self._account_scope.get(plugin_id, []))

    def set_account_scope(self, plugin_id: str, sessions: list[str]) -> None:
        """设置插件应用到的账号；空列表=全部用户账号（删除该记录）。"""
        with self._lock:
            if sessions:
                self._account_scope[plugin_id] = list(sessions)
            else:
                self._account_scope.pop(plugin_id, None)
            self._save_state()

    def purge_account(self, session_name: str) -> list[str]:
        """从所有插件的账号范围里移除某个 session（账号被删除时调用）。
        返回受影响（范围发生变化）的插件 id 列表，供调用方决定是否 resync。"""
        affected: list[str] = []
        with self._lock:
            for pid, sessions in list(self._account_scope.items()):
                if session_name in sessions:
                    rest = [x for x in sessions if x != session_name]
                    if rest:
                        self._account_scope[pid] = rest
                    else:
                        # 范围清空=回退到「全部账号」，删除该记录
                        self._account_scope.pop(pid, None)
                    affected.append(pid)
            if affected:
                self._save_state()
        return affected

    # ──────────────────────────────────────────────
    # 通知推送 Bot 选择（单选；空/缺失=跟随默认，"default"=内置 Bot）
    # ──────────────────────────────────────────────
    def get_bot_choice(self, plugin_id: str) -> str:
        """返回插件选定的 Bot id；空字符串表示跟随当前默认 Bot。"""
        with self._lock:
            return self._bot_choice.get(plugin_id, "") or ""

    def set_bot_choice(self, plugin_id: str, bot_id: str) -> None:
        """设置插件推送/handler 用的 Bot；空=跟随默认，"default"=内置 Bot。"""
        with self._lock:
            bot_id = (bot_id or "").strip()
            if bot_id:
                self._bot_choice[plugin_id] = bot_id
            else:
                self._bot_choice.pop(plugin_id, None)
            self._save_state()

    def purge_bot(self, bot_id: str) -> list[str]:
        """从所有插件路由中移除指定渠道，保留同一路由中的其他渠道。"""
        affected: list[str] = []
        with self._lock:
            for pid, choice in list(self._bot_choice.items()):
                channel_ids = [item.strip() for item in choice.split(",") if item.strip()]
                remaining = [item for item in channel_ids if item != bot_id]
                if len(remaining) == len(channel_ids):
                    continue
                if remaining:
                    self._bot_choice[pid] = ",".join(remaining)
                else:
                    self._bot_choice.pop(pid, None)
                affected.append(pid)
            if affected:
                self._save_state()
        return affected

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
            self._account_scope.pop(plugin_id, None)
            self._bot_choice.pop(plugin_id, None)
            self._save_state()


# 全局单例
registry = PluginRegistry()
