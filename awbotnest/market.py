from __future__ import annotations

import re
import shutil
import tempfile
import ast
import time
import logging
import json
import hashlib
import os
import stat
import uuid
import asyncio
from packaging.version import InvalidVersion, Version
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .config import PLUGINS_DIR, Settings, save_settings

MANIFEST_NAME = "manifest_v2.json"
PLUGIN_HEAT_SERVER_URL = "http://64.83.41.32:18002"
# 热度中心可以要求上报凭据；留空表示不发送，兼容未开启鉴权的部署。
PLUGIN_HEAT_REPORT_TOKEN = os.getenv("AWBOTNEST_PLUGIN_HEAT_TOKEN", "").strip()
OFFICIAL_REPO = "AWdress/AWBotNest-Plugins"
logger = logging.getLogger("awbotnest.market")
REPO_PATTERN = re.compile(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
SOURCE_MIGRATION_MAX_SECONDS = 30.0
SOURCE_MIGRATION_MAX_REQUESTS = 80
SOURCE_MIGRATION_MAX_CANDIDATES = 128
SOURCE_MIGRATION_MAX_REPOS = 16
SOURCE_MIGRATION_MAX_HISTORY = 4
SOURCE_MIGRATION_MAX_PER_PLUGIN = 8
SOURCE_MIGRATION_MAX_PLUGINS = 32
SOURCE_MIGRATION_MAX_FILES = 500
SOURCE_MIGRATION_MAX_BYTES = 16 * 1024 * 1024
SOURCE_MIGRATION_MAX_TREE_ENTRIES = 12000
SOURCE_MIGRATION_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SOURCE_MIGRATION_MAX_TOTAL_RESPONSE_BYTES = 16 * 1024 * 1024
SOURCE_MIGRATION_RETRY_SECONDS = 3600.0


class _SourceMigrationLimit(RuntimeError):
    pass


def normalize_repo(value: str) -> str:
    match = REPO_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("插件仓库必须是 GitHub owner/repo 或公开仓库地址")
    return f"{match.group(1)}/{match.group(2)}"


def _safe_path(value: str) -> PurePosixPath:
    if "\\" in value or ":" in value:
        raise ValueError(f"插件路径不安全：{value}")
    path = PurePosixPath(value.strip().lstrip("/"))
    if (not path.parts or ".." in path.parts or any(part.startswith(".") for part in path.parts)
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in path.parts)):
        raise ValueError(f"插件路径不安全：{value}")
    return path


def _is_hidden_plugin_name(name: str) -> bool:
    """模板、备份与临时文件不是插件，不能计入本地安装热度。"""
    return name.startswith(("_", "."))


def _heat_headers() -> dict[str, str]:
    return {"x-plugin-auth": PLUGIN_HEAT_REPORT_TOKEN} if PLUGIN_HEAT_REPORT_TOKEN else {}


class PluginMarket:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.install_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._pending_installs: dict[str, list[tuple[Path, Path]]] = {}
        self._cache: dict[str, Any] | None = None
        self._cache_until = 0.0
        self._state_path = PLUGINS_DIR.parent / "data" / "repo_sync.json"
        self._last_sync: str | None = None
        self._skipped_manifest_logged: set[str] = set()
        self._sources_path = PLUGINS_DIR.parent / "data" / "plugin_sources.json"
        self._sources: dict[str, dict[str, str]] = {}
        self._local_only: set[str] = set()
        self._source_generation: dict[str, int] = {}
        self._source_candidates: list[dict[str, Any]] = []
        self._source_candidates_complete = False
        self._source_completed_repos: set[str] = set()
        self._migration_lock = asyncio.Lock()
        self._migration_failures: dict[str, tuple[str, float]] = {}
        self._migration_cursor = 0
        try:
            source_state = json.loads(self._sources_path.read_text(encoding="utf-8"))
            entries = source_state.get("plugins", {}) if isinstance(source_state, dict) else {}
            if isinstance(entries, dict):
                self._sources = {str(key): dict(item) for key, item in entries.items()
                                 if isinstance(item, dict)}
            local_only = source_state.get("local_only", []) if isinstance(source_state, dict) else []
            if isinstance(local_only, list):
                self._local_only = {item for item in local_only if isinstance(item, str)
                                    and re.fullmatch(r"[A-Za-z0-9_-]+", item)}
                for plugin_id in self._local_only:
                    self._sources.pop(plugin_id, None)
        except (OSError, json.JSONDecodeError):
            pass
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8")) if self._state_path.exists() else {}
            if isinstance(state, dict) and isinstance(state.get("store"), dict):
                self._cache = state["store"]
                self._cache_until = time.monotonic() + 300
                self._last_sync = state.get("last_sync")
        except (OSError, json.JSONDecodeError):
            pass

    def clear_cache(self) -> None:
        self._cache_until = 0.0

    def cached(self) -> dict[str, Any]:
        """页面仅读取最近成功缓存，不因过期或缺失而访问远程。"""
        snapshot = self._cache if self._cache is not None else {
            "plugins": [], "errors": [], "install_counts": {},
            "official_ids": [], "last_sync": None, "manifest": MANIFEST_NAME,
        }
        result = {**snapshot, "plugins": []}
        for source in snapshot.get("plugins", []):
            plugin = dict(source)
            plugin_id = str(plugin.get("id") or "")
            if getattr(self, "_sources", {}).get(plugin_id) and not self.source_matches(plugin):
                continue
            source_path = plugin.get("path")
            installed = (
                self._installed_version_for_source(plugin_id, str(source_path))
                if source_path else self._installed_version(plugin_id)
            )
            plugin.update(installed=installed is not None, installed_version=installed,
                          local_version=installed,
                          update_available=self._newer(str(plugin.get("version") or "0"), installed))
            plugin["source_confirmed"] = self.source_matches(plugin)
            plugin["auto_update_allowed"] = installed is not None and plugin["source_confirmed"]
            result["plugins"].append(plugin)
        return result

    async def refresh(self) -> dict[str, Any]:
        """强制刷新插件市场缓存，供启动流程和定时任务调用。"""
        self.clear_cache()
        return await self.list_all()

    def _heat_state(self) -> dict[str, Any]:
        state_path = PLUGINS_DIR.parent / "data" / "plugin_heat_state.json"
        try:
            value = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if not isinstance(value, dict): value = {}
        except (OSError, json.JSONDecodeError):
            value = {}
        value.setdefault("installation_id", str(uuid.uuid4()))
        value.setdefault("installs", {})
        # 每个插件都有独立的安装周期：更新沿用周期，卸载后重新安装会生成新的
        # 周期。这样热度中心可以忽略轮询重报，同时仍统计真正的卸载后重装。
        value.setdefault("plugin_installations", {})
        value.setdefault("reported", {})
        return value

    def local_install_counts(self) -> dict[str, int]:
        """Return persisted local heat immediately, without contacting the center."""
        state = self._heat_state()
        counts = {str(key): max(0, int(value or 0))
                for key, value in (state.get("installs") or {}).items()}
        snapshot = self.cached()
        for plugin in snapshot.get("plugins", []):
            value = plugin.get("install_count")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[str(plugin.get("id"))] = value
        for key, value in (snapshot.get("install_counts") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[str(key)] = value
        return counts

    def _save_heat_state(self, state: dict[str, Any]) -> None:
        path = PLUGINS_DIR.parent / "data" / "plugin_heat_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def source_matches(self, plugin: dict[str, Any]) -> bool:
        """An explicit install or an exact legacy-source proof establishes provenance."""
        if str(plugin.get("id") or "") in getattr(self, "_local_only", set()):
            return False
        source = getattr(self, "_sources", {}).get(str(plugin.get("id") or ""), {})
        if not source:
            return False
        try:
            repo = normalize_repo(str(plugin.get("repo") or "")).casefold()
            path = _safe_path(str(plugin.get("path") or f"{plugin['id']}.py")).as_posix()
        except (ValueError, KeyError):
            return False
        return repo == str(source.get("repo") or "").casefold() and path == source.get("path")

    def _save_sources(self) -> None:
        self._sources_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._sources_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 2, "plugins": self._sources,
                                         "local_only": sorted(getattr(self, "_local_only", set()))},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._sources_path)

    def confirm_source(self, plugin: dict[str, Any]) -> None:
        plugin_id = str(plugin.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", plugin_id):
            raise ValueError("插件 ID 不合法")
        source = {
            "repo": normalize_repo(str(plugin.get("repo") or "")),
            "path": _safe_path(str(plugin.get("path") or f"{plugin_id}.py")).as_posix(),
        }
        if self.source_matches(plugin):
            return
        self._set_source_state(plugin_id, source, local_only=False)

    def _set_source_state(self, plugin_id: str, source: dict[str, str] | None, *, local_only: bool) -> None:
        previous = self._sources.get(plugin_id)
        was_local = plugin_id in self._local_only
        had_generation = plugin_id in self._source_generation
        generation = self._source_generation.get(plugin_id, 0)
        if source is None:
            self._sources.pop(plugin_id, None)
        else:
            self._sources[plugin_id] = source
        if local_only:
            self._local_only.add(plugin_id)
        else:
            self._local_only.discard(plugin_id)
        self._source_generation[plugin_id] = generation + 1
        try:
            self._save_sources()
        except BaseException:
            if previous is None:
                self._sources.pop(plugin_id, None)
            else:
                self._sources[plugin_id] = previous
            if was_local:
                self._local_only.add(plugin_id)
            else:
                self._local_only.discard(plugin_id)
            if had_generation:
                self._source_generation[plugin_id] = generation
            else:
                self._source_generation.pop(plugin_id, None)
            raise

    def forget_source(self, plugin_id: str, *, local_only: bool = True) -> None:
        # A successful manual upload explicitly opts out of automatic updates,
        # even if its bytes happen to equal an old repository snapshot.
        self._set_source_state(plugin_id, None, local_only=local_only)

    async def record_install(self, plugin: dict[str, Any], event_type: str = "install") -> None:
        """记录本地安装热度并尽力上报中心；网络失败不影响安装。"""
        state_path = PLUGINS_DIR.parent / "data" / "plugin_heat_state.json"
        state = self._heat_state()
        plugin_id = str(plugin.get("id") or "").strip()
        if not plugin_id:
            return
        if plugin.get("repo"):
            self.confirm_source(plugin)
        event_type = event_type if event_type in {"install", "update"} else "install"
        cycles = state.setdefault("plugin_installations", {})
        cycle = str(cycles.get(plugin_id) or "")
        if event_type == "install" or not cycle:
            cycle = str(uuid.uuid4())
            cycles[plugin_id] = cycle
        version = str(plugin.get("version") or "unknown")[:64]
        reported = state.setdefault("reported", {})
        plugin_reports = reported.setdefault(plugin_id, {})
        report_key = f"{cycle}:{version}"
        installs = state.setdefault("installs", {})
        if report_key not in plugin_reports:
            installs[plugin_id] = max(0, int(installs.get(plugin_id, 0) or 0)) + 1
            plugin_reports[report_key] = True
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_heat_state(state)
        event = {
            "event_id": str(uuid.uuid4()), "installation_id": cycle,
            "plugin_id": plugin_id,
            "event_type": event_type, "version": version, "app_version": "2",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4, connect=2)) as client:
                await client.post(
                    f"{PLUGIN_HEAT_SERVER_URL}/api/plugin-heat/events",
                    json={"events": [event]}, headers=_heat_headers(),
                )
        except Exception:
            logger.debug("插件安装热度上报暂时失败：%s", plugin_id)

    def forget_install(self, plugin_id: str) -> None:
        """结束一个插件安装周期；下次安装同一版本也应重新计入热度。"""
        plugin_id = str(plugin_id or "").strip()
        if not plugin_id:
            return
        state = self._heat_state()
        state.setdefault("plugin_installations", {}).pop(plugin_id, None)
        self._save_heat_state(state)
        self.forget_source(plugin_id, local_only=False)

    async def poll_updates(self, runtime: Any) -> dict[str, Any]:
        await self.refresh()
        await self.migrate_legacy_sources()
        async with self.install_lock:
            # An administrator may have updated a plugin while source proof was
            # waiting on GitHub. Re-read local versions only after taking the
            # installation lock; never install from that stale version snapshot.
            return await self._poll_updates(runtime, listing=self.cached())

    @staticmethod
    def _source_ignored(path: PurePosixPath) -> bool:
        return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}

    @staticmethod
    def _source_reparse(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )

    @staticmethod
    def _source_tree_path(value: Any) -> str:
        # Unlike a manifest's install path, a Git tree can legitimately contain
        # dotfiles, spaces and Unicode. Do not normalize away unsafe components.
        if (not isinstance(value, str) or not value or value.startswith("/")
                or "\\" in value or ":" in value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise ValueError("来源快照包含不安全路径")
        return value

    @staticmethod
    def _source_metadata(content: bytes, plugin_id: str) -> dict[str, Any]:
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("插件入口文件超过 2 MB")
        tree = ast.parse(content.decode("utf-8"))
        assignments = [node for node in tree.body if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__plugin__" for target in node.targets
        )]
        if len(assignments) != 1:
            raise ValueError("插件缺少唯一的静态元数据")
        metadata = ast.literal_eval(assignments[0].value)
        if (not isinstance(metadata, dict) or metadata.get("id") != plugin_id
                or not isinstance(metadata.get("version"), str)
                or not metadata["version"].strip() or len(metadata["version"]) > 64):
            raise ValueError("插件 ID 或版本元数据不合法")
        return metadata

    @classmethod
    def _local_source_snapshot(cls, plugin_id: str) -> dict[str, Any] | None:
        """Read the scanner's actual entry and hash every installed source byte."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", plugin_id) or _is_hidden_plugin_name(plugin_id):
            return None
        try:
            if cls._source_reparse(PLUGINS_DIR.lstat()):
                return None
            package = PLUGINS_DIR / plugin_id
            package_entry = package / "__init__.py"
            # Packages override a residual V1 single file, just as the scanner
            # does. A linked package must not silently fall back to that file.
            try:
                package_info = package.lstat()
            except FileNotFoundError:
                package_info = None
            if package_info is not None and cls._source_reparse(package_info):
                return None
            if package_info is not None and stat.S_ISDIR(package_info.st_mode) and package_entry.exists():
                root, entry, kind = package, package_entry, "directory"
            else:
                root = PLUGINS_DIR
                entry = PLUGINS_DIR / f"{plugin_id}.py"
                kind = "file"
            resolved_root = root.resolve(strict=True)
            if not resolved_root.is_relative_to(PLUGINS_DIR.resolve(strict=True)):
                return None
            files: dict[str, str] = {}
            entry_content: bytes | None = None
            total_bytes = 0
            visited = 0
            pending = [root] if kind == "directory" else []
            paths = [entry] if kind == "file" else []
            while pending or paths:
                if not paths:
                    directory = pending.pop()
                    directory_info = directory.lstat()
                    if (cls._source_reparse(directory_info)
                            or not directory.resolve(strict=True).is_relative_to(resolved_root)
                            or not stat.S_ISDIR(directory_info.st_mode)):
                        return None
                    with os.scandir(directory) as children:
                        for child in children:
                            visited += 1
                            if visited > SOURCE_MIGRATION_MAX_TREE_ENTRIES:
                                raise _SourceMigrationLimit("本地文件数超过来源核对预算")
                            paths.append(directory / child.name)
                    if not paths:
                        continue
                path = paths.pop()
                info = path.lstat()
                if (cls._source_reparse(info)
                        or not path.resolve(strict=True).is_relative_to(resolved_root)):
                    return None
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    return None
                relative = PurePosixPath(path.relative_to(root).as_posix())
                cls._source_tree_path(relative.as_posix())
                if cls._source_ignored(relative):
                    continue
                if len(files) >= SOURCE_MIGRATION_MAX_FILES or info.st_size > SOURCE_MIGRATION_MAX_BYTES - total_bytes:
                    raise _SourceMigrationLimit("本地插件超过来源核对预算")
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns) != (
                        info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns
                    ):
                        return None
                    content = handle.read(SOURCE_MIGRATION_MAX_BYTES - total_bytes + 1)
                    after = os.fstat(handle.fileno())
                if (len(content) != info.st_size or
                        (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                    return None
                total_bytes += len(content)
                files[relative.as_posix()] = hashlib.sha1(
                    b"blob " + str(len(content)).encode("ascii") + b"\0" + content
                ).hexdigest()
                if path == entry:
                    entry_content = content
            if entry_content is None:
                return None
            metadata = cls._source_metadata(entry_content, plugin_id)
            fingerprint = hashlib.sha256(json.dumps(
                {"kind": kind, "files": files}, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            return {"kind": kind, "metadata": metadata, "files": files, "fingerprint": fingerprint}
        except (OSError, ValueError, SyntaxError, TypeError, UnicodeError, _SourceMigrationLimit):
            return None

    @staticmethod
    def _source_repository_hint(metadata: dict[str, Any]) -> str | None:
        repositories: set[str] = set()
        for key in ("repo", "repository", "source_repo", "repo_url", "homepage"):
            value = metadata.get(key)
            if not value:
                continue
            if not isinstance(value, str):
                return None
            if key == "homepage" and not value.strip().startswith(("https://github.com/", "http://github.com/")):
                continue
            try:
                repositories.add(normalize_repo(value).casefold())
            except ValueError:
                if key != "homepage":
                    return None
        return next(iter(repositories)) if len(repositories) == 1 else None

    def _source_inventory_state(self) -> str:
        values = [(str(item.get("id") or ""), str(item.get("repo") or ""),
                   str(item.get("path") or ""), str(item.get("branch") or "main"),
                   str(item.get("version") or "")) for item in self._source_candidates]
        return hashlib.sha256(json.dumps({
            "repos": list(self.settings.plugin_repos), "candidates": values,
            "complete": self._source_candidates_complete,
            "completed_repos": sorted(self._source_completed_repos),
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def _source_json(self, url: str, budget: dict[str, Any]) -> Any:
        if url in budget["json"]:
            return budget["json"][url]
        remaining = budget["deadline"] - time.monotonic()
        if remaining <= 0 or budget["requests"] >= SOURCE_MIGRATION_MAX_REQUESTS:
            raise _SourceMigrationLimit("来源核对已达到网络预算")
        budget["requests"] += 1
        response = await asyncio.wait_for(self._github(url), timeout=min(5.0, remaining))
        response.raise_for_status()
        response_size = len(response.content)
        if (response_size > SOURCE_MIGRATION_MAX_RESPONSE_BYTES
                or budget["bytes"] + response_size > SOURCE_MIGRATION_MAX_TOTAL_RESPONSE_BYTES):
            raise _SourceMigrationLimit("来源快照响应超过预算")
        budget["bytes"] += response_size
        value = response.json()
        budget["json"][url] = value
        return value

    async def _source_tree(self, repo: str, ref: str, budget: dict[str, Any]) -> dict[str, dict[str, Any]]:
        key = (repo.casefold(), ref)
        if key in budget["trees"]:
            return budget["trees"][key]
        if repo.casefold() not in budget["repos"]:
            if len(budget["repos"]) >= SOURCE_MIGRATION_MAX_REPOS:
                raise _SourceMigrationLimit("来源仓库数超过核对预算")
            budget["repos"].add(repo.casefold())
        payload = await self._source_json(
            f"https://api.github.com/repos/{repo}/git/trees/{quote(ref, safe='')}?recursive=1", budget
        )
        if not isinstance(payload, dict) or payload.get("truncated") is not False or not isinstance(payload.get("tree"), list):
            raise ValueError("来源仓库未提供完整 Git 快照")
        if len(payload["tree"]) > SOURCE_MIGRATION_MAX_TREE_ENTRIES:
            raise _SourceMigrationLimit("来源快照文件数超过预算")
        result = {}
        for item in payload["tree"]:
            if not isinstance(item, dict):
                raise ValueError("来源 Git 快照格式不合法")
            path = self._source_tree_path(item.get("path"))
            if path in result or item.get("type") not in {"blob", "tree", "commit"}:
                raise ValueError("来源 Git 快照存在重复或不合法项")
            if not isinstance(item.get("sha"), str) or not re.fullmatch(r"[a-fA-F0-9]{40}", item["sha"]):
                raise ValueError("来源 Git 快照哈希不合法")
            result[path] = item
        budget["trees"][key] = result
        return result

    def _source_snapshot_matches(self, snapshot: dict[str, Any], source: str,
                                 tree: dict[str, dict[str, Any]]) -> bool:
        file_source = PurePosixPath(source).suffix == ".py"
        if file_source != (snapshot["kind"] == "file"):
            return False
        selected = [(source, tree[source])] if file_source and source in tree else []
        if not file_source:
            prefix = source.rstrip("/") + "/"
            selected = [(path[len(prefix):], item) for path, item in tree.items() if path.startswith(prefix)]
        remote: dict[str, str] = {}
        total_bytes = 0
        for path, item in selected:
            if item.get("type") == "tree" and item.get("mode") == "040000":
                continue
            if item.get("type") != "blob" or item.get("mode") not in {"100644", "100755"}:
                raise ValueError("来源插件包含符号链接或特殊 Git 文件")
            relative = PurePosixPath(path)
            if self._source_ignored(relative):
                continue
            size = item.get("size")
            if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
                raise ValueError("来源插件文件大小不合法")
            total_bytes += size or 0
            if len(remote) >= SOURCE_MIGRATION_MAX_FILES or total_bytes > SOURCE_MIGRATION_MAX_BYTES:
                raise _SourceMigrationLimit("来源插件超过核对预算")
            relative_path = f"{snapshot['metadata']['id']}.py" if file_source else relative.as_posix()
            remote[relative_path] = item["sha"].lower()
        return bool(remote) and remote == snapshot["files"]

    async def _source_candidate_matches(self, candidate: dict[str, Any], snapshot: dict[str, Any],
                                        budget: dict[str, Any]) -> bool:
        repo = normalize_repo(str(candidate.get("repo") or ""))
        source = _safe_path(str(candidate.get("path") or f"{candidate['id']}.py")).as_posix()
        branch = str(candidate.get("branch") or "main")
        if len(branch) > 128 or ".." in branch or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch):
            raise ValueError("来源仓库分支名称不合法")
        tree = await self._source_tree(repo, branch, budget)
        if self._source_snapshot_matches(snapshot, source, tree):
            return True
        history = await self._source_json(f"https://api.github.com/repos/{repo}/commits?" + urlencode({
            "path": source, "sha": branch, "per_page": SOURCE_MIGRATION_MAX_HISTORY,
        }), budget)
        if not isinstance(history, list):
            raise ValueError("来源仓库历史格式不合法")
        if len(history) > SOURCE_MIGRATION_MAX_HISTORY:
            raise _SourceMigrationLimit("来源仓库历史超过核对预算")
        refs: set[str] = set()
        for commit in history:
            ref = commit.get("sha") if isinstance(commit, dict) else None
            if not isinstance(ref, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", ref):
                raise ValueError("来源仓库历史提交不合法")
            if ref in refs:
                continue
            refs.add(ref)
            historical = await self._source_tree(repo, ref, budget)
            if self._source_snapshot_matches(snapshot, source, historical):
                return True
        return False

    def _reselect_source_cache(self) -> None:
        if self._cache is None:
            return
        counts = self._cache.get("install_counts") or {}
        selected = []
        seen = set()
        for old in self._cache.get("plugins", []):
            plugin_id = str(old.get("id") or "")
            if plugin_id in seen:
                continue
            plugin = old
            if self._sources.get(plugin_id) and not self.source_matches(old):
                plugin = next((item for item in self._source_candidates if item.get("id") == plugin_id
                               and self.source_matches(item)), None)
                if plugin is None:
                    continue
            plugin = {**plugin, "install_count": counts.get(plugin_id, old.get("install_count", 0))}
            selected.append(plugin)
            seen.add(plugin_id)
        for candidate in self._source_candidates:
            plugin_id = str(candidate.get("id") or "")
            if (plugin_id not in seen and self.source_matches(candidate)
                    and (self.settings.telegram_configured or candidate.get("scope") not in {"user", "both"})):
                selected.append({**candidate, "install_count": counts.get(plugin_id, 0)})
                seen.add(plugin_id)
        self._cache = {**self._cache, "plugins": selected,
                       "official_ids": [item["id"] for item in selected if item.get("official")]}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"store": self._cache, "last_sync": self._last_sync},
                                            ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            logger.debug("插件来源已保存，市场缓存暂未写入磁盘", exc_info=True)

    async def migrate_legacy_sources(self) -> dict[str, Any]:
        """Prove old repository installs in the background, without reinstalling."""
        result: dict[str, Any] = {"bound": [], "checked": 0}
        if self._migration_lock.locked() or not self._source_candidates:
            return result
        async with self._migration_lock:
            budget = {"deadline": time.monotonic() + SOURCE_MIGRATION_MAX_SECONDS,
                      "requests": 0, "bytes": 0, "repos": set(), "json": {}, "trees": {}, "candidates": 0}
            inventory = self._source_inventory_state()
            by_id: dict[str, list[dict[str, Any]]] = {}
            for candidate in self._source_candidates:
                plugin_id = str(candidate.get("id") or "")
                if re.fullmatch(r"[A-Za-z0-9_-]+", plugin_id) and not _is_hidden_plugin_name(plugin_id):
                    by_id.setdefault(plugin_id, []).append(dict(candidate))
            plugin_ids = sorted(by_id)
            if not plugin_ids:
                return result
            offset = self._migration_cursor % len(plugin_ids)
            ordered = plugin_ids[offset:] + plugin_ids[:offset]
            examined = 0
            try:
                async with asyncio.timeout(SOURCE_MIGRATION_MAX_SECONDS):
                    for plugin_id in ordered:
                        if examined >= SOURCE_MIGRATION_MAX_PLUGINS:
                            break
                        self._migration_cursor = (plugin_ids.index(plugin_id) + 1) % len(plugin_ids)
                        if self._sources.get(plugin_id) or plugin_id in self._local_only:
                            continue
                        snapshot = await asyncio.to_thread(self._local_source_snapshot, plugin_id)
                        if snapshot is None:
                            continue
                        examined += 1
                        hint = self._source_repository_hint(snapshot["metadata"])
                        eligible: dict[tuple[str, str, str], dict[str, Any]] = {}
                        for candidate in by_id[plugin_id]:
                            try:
                                repo = normalize_repo(str(candidate.get("repo") or "")).casefold()
                                path = _safe_path(str(candidate.get("path") or f"{plugin_id}.py")).as_posix()
                            except ValueError:
                                continue
                            if repo != OFFICIAL_REPO.casefold() and repo != hint:
                                continue
                            if not self._source_candidates_complete and repo not in self._source_completed_repos:
                                continue
                            if (PurePosixPath(path).suffix == ".py") != (snapshot["kind"] == "file"):
                                continue
                            eligible[(repo, path, str(candidate.get("branch") or "main"))] = candidate
                        if not eligible or len(eligible) > SOURCE_MIGRATION_MAX_PER_PLUGIN:
                            continue
                        if budget["candidates"] + len(eligible) > SOURCE_MIGRATION_MAX_CANDIDATES:
                            break
                        cache_key = snapshot["fingerprint"] + ":" + inventory
                        previous = self._migration_failures.get(plugin_id)
                        if previous and previous[0] == cache_key and previous[1] > time.monotonic():
                            continue
                        budget["candidates"] += len(eligible)
                        result["checked"] += 1
                        generation = self._source_generation.get(plugin_id, 0)
                        matched = None
                        for repo in dict.fromkeys((OFFICIAL_REPO.casefold(), hint)):
                            if not repo:
                                continue
                            candidates = [item for key, item in eligible.items() if key[0] == repo]
                            matches = []
                            all_verified = True
                            for candidate in candidates:
                                try:
                                    if await self._source_candidate_matches(candidate, snapshot, budget):
                                        matches.append(candidate)
                                except _SourceMigrationLimit:
                                    raise
                                except Exception:
                                    all_verified = False
                                    logger.debug("旧插件来源暂未核对成功：%s", plugin_id, exc_info=True)
                            if all_verified and len(matches) == 1:
                                matched = matches[0]
                                break
                            if matches:
                                break  # An ambiguous official source must not fall through to a fork.
                        if matched is not None:
                            async with self.install_lock:
                                current = await asyncio.to_thread(self._local_source_snapshot, plugin_id)
                                if (not self._sources.get(plugin_id) and plugin_id not in self._local_only
                                        and generation == self._source_generation.get(plugin_id, 0)
                                        and inventory == self._source_inventory_state()
                                        and current is not None and current["fingerprint"] == snapshot["fingerprint"]):
                                    try:
                                        self.confirm_source(matched)
                                    except OSError:
                                        logger.debug("旧插件来源暂未写入磁盘：%s", plugin_id, exc_info=True)
                                    else:
                                        result["bound"].append(plugin_id)
                                        self._migration_failures.pop(plugin_id, None)
                                        self._reselect_source_cache()
                        if plugin_id not in result["bound"]:
                            self._migration_failures[plugin_id] = (cache_key, time.monotonic() + SOURCE_MIGRATION_RETRY_SECONDS)
                            if len(self._migration_failures) > SOURCE_MIGRATION_MAX_PLUGINS * 8:
                                self._migration_failures.pop(next(iter(self._migration_failures)))
            except (TimeoutError, _SourceMigrationLimit):
                logger.debug("本轮旧插件来源核对已达到预算，稍后继续")
            return result

    async def _poll_updates(self, runtime: Any, *, listing: dict[str, Any] | None = None) -> dict[str, Any]:
        """刷新市场并自动更新已安装且有新版本的插件。"""
        listing = await self.refresh() if listing is None else listing
        updated: list[str] = []
        errors: list[str] = []
        for plugin in listing.get("plugins", []):
            if not plugin.get("installed") or not plugin.get("update_available"):
                continue
            if not self.source_matches(plugin):
                continue
            plugin_id = str(plugin.get("id") or "")
            plugin_name = str(plugin.get("name") or runtime.display_name(plugin_id))
            was_loaded = plugin_id in runtime.loaded
            try:
                if was_loaded: await runtime.disable(plugin_id, persist=False)
                await self.install(plugin)
                runtime.invalidate_scan_cache()
                if was_loaded:
                    result = await runtime.enable(plugin_id)
                    if result.error: raise RuntimeError(result.error)
                self.confirm_source(plugin)
                self.finish(plugin_id, True)
                try:
                    await self.record_install(plugin, "update")
                except Exception:
                    # The plugin and its source are committed. Optional heat
                    # accounting must not report this successful update as a
                    # failed transaction or attempt to restore removed backups.
                    logger.debug("插件已更新，安装热度暂未保存：%s", plugin_id, exc_info=True)
                updated.append(plugin_name)
            except asyncio.CancelledError:
                self.finish(plugin_id, False)
                runtime.invalidate_scan_cache()
                raise
            except Exception as exc:
                errors.append(f"{plugin_name}：{exc}（已跳过，继续更新其他插件）")
                if was_loaded and plugin_id in runtime.loaded:
                    try:
                        await runtime.disable(plugin_id, persist=False)
                    except Exception as stop_exc:
                        errors.append(f"{plugin_name} 停止新实例失败：{stop_exc}")
                try:
                    self.finish(plugin_id, False)
                    runtime.invalidate_scan_cache()
                except Exception as rollback_exc:
                    errors.append(f"{plugin_name} 回滚失败：{rollback_exc}")
                if was_loaded:
                    try:
                        restored = await runtime.enable(plugin_id)
                        if restored.error:
                            errors.append(f"{plugin_name} 恢复失败：{restored.error}")
                    except Exception as restore_exc:
                        errors.append(f"{plugin_name} 恢复失败：{restore_exc}")
        if updated: self.clear_cache()
        return {"ok": not errors, "updated": updated, "errors": errors}

    async def discover_repositories(self) -> dict[str, Any]:
        """发现官方仓库的 fork 及同名仓库，并验证 V2 清单后加入配置。"""
        candidates: set[str] = set()
        errors: list[str] = []
        for url, params, label in (
            (f"https://api.github.com/repos/{OFFICIAL_REPO}/forks",
             {"per_page": 100, "sort": "newest"}, "获取 fork 列表"),
            ("https://api.github.com/search/repositories",
             {"q": "AWBotNest-Plugins in:name", "per_page": 100}, "搜索仓库"),
        ):
            try:
                async with httpx.AsyncClient(
                    timeout=30, follow_redirects=True, proxy=self.settings.proxy_url or None,
                    headers=self._github_headers(url),
                ) as client:
                    response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                items = payload if isinstance(payload, list) else payload.get("items", [])
                candidates.update(
                    str(item["full_name"]) for item in items
                    if isinstance(item, dict) and item.get("full_name")
                )
            except Exception as exc:
                errors.append(f"{label}失败：{exc}")

        existing = {repo.casefold() for repo in self.settings.plugin_repos}
        added: list[str] = []
        skipped_existing = 0
        for candidate in sorted(candidates):
            if candidate.casefold() in existing or candidate.casefold() == OFFICIAL_REPO.casefold():
                skipped_existing += 1
                continue
            try:
                await self.list_repo(candidate)
            except Exception:
                continue
            self.settings.plugin_repos.append(candidate)
            existing.add(candidate.casefold())
            added.append(candidate)
        if added:
            save_settings(self.settings)
            self.clear_cache()
            logger.info("插件仓库自动发现：新增 %d 个仓库 %s", len(added), added)
        return {"ok": not errors, "found": len(candidates), "added": added,
                "skipped_existing": skipped_existing, "errors": errors}

    def _github_headers(self, url: str) -> dict[str, str]:
        from urllib.parse import urlsplit
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "AWBotNest/2.0"}
        target = urlsplit(url)
        if target.scheme == "https" and target.netloc.lower() == "api.github.com" and self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        return headers

    async def _github(self, url: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     proxy=self.settings.proxy_url or None) as client:
            response = await client.get(url, headers=self._github_headers(url))
        return response

    async def list_repo(self, repo_value: str) -> dict[str, Any]:
        repo = normalize_repo(repo_value)
        repo_response = await self._github(f"https://api.github.com/repos/{repo}")
        if repo_response.status_code == 404:
            raise ValueError("插件仓库不存在或不是公开仓库")
        repo_response.raise_for_status()
        branch = str(repo_response.json().get("default_branch") or "main")
        manifest_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{MANIFEST_NAME}"
        manifest_response = await self._github(manifest_url)
        if manifest_response.status_code == 404:
            raise ValueError(f"仓库根目录缺少 {MANIFEST_NAME}")
        manifest_response.raise_for_status()
        payload = manifest_response.json()
        entries = payload.get("plugins") if isinstance(payload, dict) and "plugins" in payload else payload
        if not isinstance(entries, dict):
            raise ValueError(f"{MANIFEST_NAME} 必须是插件 ID 到信息的对象")
        plugins = []
        source_candidates = []
        for plugin_id, raw in entries.items():
            if not isinstance(raw, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(plugin_id)):
                continue
            scope = str(raw.get("scope") or "user")
            source_path = _safe_path(str(raw.get("path") or f"{plugin_id}.py"))
            installed_version = self._installed_version_for_source(str(plugin_id), source_path)
            remote_version = str(raw.get("version") or "0.0.0")
            is_official = repo.casefold() == OFFICIAL_REPO.casefold()
            plugin = {
                "id": str(plugin_id),
                "name": str(raw.get("name") or plugin_id),
                "version": remote_version,
                "author": str(raw.get("author") or ""),
                "description": str(raw.get("description") or ""),
                "changelog": str(raw.get("changelog") or ""),
                "icon": str(raw.get("icon") or ""),
                "tags": [str(item).strip()[:24] for item in (raw.get("tags") or [])
                         if str(item).strip()][:8],
                "scope": scope,
                "path": source_path.as_posix(),
                "repo": repo,
                "repo_url": repo,
                "official": is_official,
                "branch": branch,
                "installed": installed_version is not None,
                "installed_version": installed_version,
                "local_version": installed_version,
                "from_manifest": True,
                "update_available": self._newer(remote_version, installed_version),
            }
            source_candidates.append(plugin)
            if self.settings.telegram_configured or scope not in {"user", "both"}:
                plugins.append(plugin)
        return {"repo": repo, "branch": branch, "manifest": MANIFEST_NAME,
                "plugins": plugins, "source_candidates": source_candidates}

    async def _install_counts(self) -> dict[str, int]:
        """读取全局插件热度；中心不可用时不影响插件商店。"""
        state = self._heat_state()
        local = state.get("installs") or {}
        for entry in PLUGINS_DIR.iterdir() if PLUGINS_DIR.exists() else ():
            if _is_hidden_plugin_name(entry.name):
                continue
            plugin_id = entry.stem if entry.is_file() and entry.suffix == ".py" else (entry.name if entry.is_dir() and (entry / "__init__.py").exists() else "")
            if plugin_id and plugin_id not in local:
                local[plugin_id] = 1
        state["installs"] = local
        self._save_heat_state(state)
        try:
            # 热度是增强信息，不应阻塞市场加载；中心不可达时立即回退本地缓存。
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(4, connect=2), follow_redirects=True,
                proxy=self.settings.proxy_url or None,
            ) as client:
                response = await client.get(f"{PLUGIN_HEAT_SERVER_URL}/api/plugin-heat/counts")
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0) > 1024 * 1024:
                return self.local_install_counts()
            raw = (response.json() or {}).get("counts")
            if not isinstance(raw, dict) or len(raw) > 10_000:
                return self.local_install_counts()
            counts = {
                str(plugin_id): count for plugin_id, count in raw.items()
                if isinstance(count, int) and not isinstance(count, bool)
                and 0 <= count <= 9_007_199_254_740_991
            }
            # 把当前已安装插件纳入本地热度缓存。
            for entry in PLUGINS_DIR.iterdir() if PLUGINS_DIR.exists() else ():
                if _is_hidden_plugin_name(entry.name):
                    continue
                plugin_id = entry.stem if entry.is_file() and entry.suffix == ".py" else (entry.name if entry.is_dir() and (entry / "__init__.py").exists() else "")
                if plugin_id and plugin_id not in local:
                    local[plugin_id] = 1
            for plugin_id, count in local.items():
                counts.setdefault(str(plugin_id), int(count or 0))
            return counts
        except Exception:
            return self.local_install_counts()

    @staticmethod
    def _entry_version(entry: Path) -> str | None:
        try:
            tree = ast.parse(entry.read_text(encoding="utf-8"))
        except Exception:
            return None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__plugin__" for target in node.targets
            ):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return None
                return str(value.get("version") or "0.0.0") if isinstance(value, dict) else None
        return None

    @staticmethod
    def _installed_version(plugin_id: str) -> str | None:
        # 单文件与目录两种形态并存时，运行时实际加载的是目录形态，
        # 读取版本必须用同一个入口；否则会读到旧的单文件而永远判定「有新版本」。
        entry = PLUGINS_DIR / plugin_id / "__init__.py"
        if not entry.exists():
            entry = PLUGINS_DIR / f"{plugin_id}.py"
        if not entry.exists():
            return None
        return PluginMarket._entry_version(entry)

    @staticmethod
    def _installed_version_for_source(plugin_id: str, source_path: PurePosixPath | str) -> str | None:
        """按清单形态读取本地版本，避免把 V1 单文件误当成 V2 目录插件。"""
        source = PurePosixPath(str(source_path))
        entry = (PLUGINS_DIR / plugin_id / "__init__.py"
                 if source.suffix != ".py" else PLUGINS_DIR / f"{plugin_id}.py")
        return PluginMarket._entry_version(entry) if entry.exists() else None

    @staticmethod
    def installed_for(plugin: dict[str, Any]) -> bool:
        plugin_id = str(plugin.get("id") or "")
        source_path = PurePosixPath(str(plugin.get("path") or f"{plugin_id}.py"))
        if not plugin_id:
            return False
        entry = (PLUGINS_DIR / plugin_id / "__init__.py"
                 if source_path.suffix != ".py" else PLUGINS_DIR / f"{plugin_id}.py")
        return entry.exists()

    @staticmethod
    def _newer(remote: str, installed: str | None) -> bool:
        if installed is None:
            return False
        try:
            return Version(remote) > Version(installed)
        except InvalidVersion:
            return remote != installed

    async def list_all(self) -> dict[str, Any]:
        async with self._refresh_lock:
            return await self._list_all()

    async def _list_all(self) -> dict[str, Any]:
        if self._cache is not None and time.monotonic() < self._cache_until:
            return self.cached()
        stale_cache = self._cache
        plugins: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        source_candidates: list[dict[str, Any]] = []
        source_candidates_complete = True
        completed_repos: set[str] = set()
        # 官方仓库是内置来源，不依赖旧配置是否曾经保存过它；否则从
        # 只配置第三方仓库时也应显示官方插件。
        repos = [OFFICIAL_REPO, *self.settings.plugin_repos]
        seen_repos: set[str] = set()
        for repo in repos:
            try:
                repo = normalize_repo(repo)
            except Exception as exc:
                source_candidates_complete = False
                errors.append(f"{repo}: {exc}")
                continue
            if repo.casefold() in seen_repos:
                continue
            seen_repos.add(repo.casefold())
            try:
                listing = await self.list_repo(repo)
            except Exception as exc:
                # 仓库可能同时包含 V1 内容或只是普通插件仓库；缺少 V2
                # 清单不应阻断市场，也不应把整条官方/自定义仓库列表标红。
                # 真实网络错误仍保留，方便用户排查仓库不可达问题。
                if MANIFEST_NAME in str(exc) and "缺少" in str(exc):
                    if repo.casefold() not in self._skipped_manifest_logged:
                        logger.debug("已跳过不含 V2 清单的插件仓库：%s", repo)
                        self._skipped_manifest_logged.add(repo.casefold())
                    continue
                source_candidates_complete = False
                errors.append(f"{repo}: {exc}")
                # 单仓库暂时失败也保留它的缓存，而不只在全部仓库失败时保留。
                for source in (stale_cache or {}).get("plugins", []):
                    if str(source.get("repo") or "").casefold() == repo.casefold() and source["id"] not in seen:
                        seen.add(source["id"])
                        plugins.append(dict(source))
                continue
            source_candidates.extend(dict(item) for item in listing.get("source_candidates", listing["plugins"]))
            completed_repos.add(repo.casefold())
            for plugin in listing["plugins"]:
                bound = getattr(self, "_sources", {}).get(plugin["id"])
                if bound and not self.source_matches(plugin):
                    continue
                if plugin["id"] not in seen:
                    seen.add(plugin["id"])
                    plugins.append(plugin)
        # Keep all valid candidates before same-ID display deduplication. A
        # copied plugin in an earlier repository is not proof of its origin.
        self._source_candidates = source_candidates
        self._source_candidates_complete = source_candidates_complete
        self._source_completed_repos = completed_repos
        install_counts = await self._install_counts()
        # 与 V1 一致：刷新失败时继续使用上次成功缓存，避免 GitHub 限流
        # 或单个仓库异常把整个插件市场显示成空白。
        if not plugins and errors and stale_cache and stale_cache.get("plugins"):
            plugins = list(stale_cache["plugins"])
            official_ids = list(stale_cache.get("official_ids") or [])
            for plugin in plugins:
                plugin["install_count"] = install_counts.get(plugin.get("id"), 0)
            logger.warning("插件市场刷新失败，已回退到上次缓存（%d 个插件）", len(plugins))
        official_ids = [p["id"] for p in plugins if p.get("official")]
        for plugin in plugins:
            plugin["install_count"] = install_counts.get(plugin["id"], 0)
        self._cache = {
            "plugins": plugins, "errors": errors, "manifest": MANIFEST_NAME,
            "install_counts": install_counts,
            "official_ids": official_ids,
            "last_sync": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._last_sync = self._cache["last_sync"]
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"store": self._cache, "last_sync": self._last_sync}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._state_path)
        self._cache_until = time.monotonic() + 300
        return self.cached()

    async def _download_file(self, repo: str, branch: str, path: PurePosixPath) -> bytes:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path.as_posix()}"
        response = await self._github(url)
        response.raise_for_status()
        if len(response.content) > 20 * 1024 * 1024:
            raise ValueError(f"插件文件超过 20 MB：{path}")
        return response.content

    @staticmethod
    def _validate_entry(content: bytes, plugin_id: str) -> None:
        tree = ast.parse(content.decode("utf-8"), filename=f"{plugin_id}.py")
        metadata = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__plugin__"
                for target in node.targets
            ):
                try:
                    metadata = ast.literal_eval(node.value)
                except (ValueError, TypeError) as exc:
                    raise ValueError("插件 __plugin__ 元数据必须为静态字典，不能包含函数调用或动态表达式") from exc
                break
        if not isinstance(metadata, dict) or str(metadata.get("id") or "") != plugin_id:
            raise ValueError("插件 __plugin__.id 与清单 ID 不一致")

    async def install(self, plugin: dict[str, Any]) -> Path:
        plugin_id = str(plugin.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", plugin_id):
            raise ValueError("插件 ID 不合法")
        repo = normalize_repo(str(plugin.get("repo") or ""))
        branch = str(plugin.get("branch") or "main")
        if ".." in branch or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch):
            raise ValueError("仓库分支名称不合法")
        source_path = _safe_path(str(plugin.get("path") or f"{plugin_id}.py"))
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        if source_path.suffix == ".py":
            content = await self._download_file(repo, branch, source_path)
            self._validate_entry(content, plugin_id)
            destination = PLUGINS_DIR / f"{plugin_id}.py"
            backup = PLUGINS_DIR / f".{plugin_id}.py.backup"
            if plugin_id in self._pending_installs:
                raise RuntimeError("该插件已有安装任务")
            # 保留单文件的原子替换：新内容写临时文件后一步换入，过程中不出现「插件不存在」。
            backup.unlink(missing_ok=True)
            if destination.exists():
                shutil.copy2(destination, backup)
            temp = PLUGINS_DIR / f".{plugin_id}.py.tmp"
            temp.write_bytes(content)
            try:
                # 先登记事务，失败时由 finish() 完整回滚。
                self._pending_installs[plugin_id] = [(destination, backup)]
                temp.replace(destination)
            except Exception:
                temp.unlink(missing_ok=True)
                self.finish(plugin_id, False)
                raise
            return destination

        tree_response = await self._github(
            f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
        )
        tree_response.raise_for_status()
        prefix = source_path.as_posix().rstrip("/") + "/"
        files = [
            _safe_path(str(item.get("path") or ""))
            for item in tree_response.json().get("tree", [])
            if item.get("type") == "blob" and str(item.get("path") or "").startswith(prefix)
        ]
        if not files or not any(path.as_posix() == prefix + "__init__.py" for path in files):
            raise ValueError("目录插件缺少 __init__.py")
        if len(files) > 500:
            raise ValueError("目录插件文件数超过 500 个")
        with tempfile.TemporaryDirectory(prefix="awbotnest-plugin-") as temporary:
            staged = Path(temporary) / plugin_id
            total_size = 0
            for remote_path in files:
                relative = PurePosixPath(remote_path.as_posix()[len(prefix):])
                local = staged.joinpath(*relative.parts)
                local.parent.mkdir(parents=True, exist_ok=True)
                content = await self._download_file(repo, branch, remote_path)
                total_size += len(content)
                if total_size > 100 * 1024 * 1024:
                    raise ValueError("目录插件总大小超过 100 MB")
                local.write_bytes(content)
            self._validate_entry((staged / "__init__.py").read_bytes(), plugin_id)
            destination = PLUGINS_DIR / plugin_id
            backup = PLUGINS_DIR / f".{plugin_id}.backup"
            if plugin_id in self._pending_installs:
                raise RuntimeError("该插件已有安装任务")
            if backup.exists():
                shutil.rmtree(backup)
            if destination.exists():
                destination.replace(backup)
            try:
                # 先登记事务，失败时由 finish() 完整回滚。
                self._pending_installs[plugin_id] = [(destination, backup)]
                shutil.copytree(staged, destination)
            except Exception:
                self.finish(plugin_id, False)
                raise
            return destination

    def finish(self, plugin_id: str, success: bool) -> None:
        transaction = self._pending_installs.get(plugin_id)
        if transaction is None:
            return  # 下载/校验失败尚未修改插件，不能删除原安装。
        if success:
            # New code and provenance are already committed. Backup removal is
            # maintenance, not a reason to undo a successful install.
            for _, backup in transaction:
                try:
                    if backup.is_dir():
                        shutil.rmtree(backup)
                    else:
                        backup.unlink(missing_ok=True)
                except OSError:
                    logger.debug("插件已安装，旧文件备份暂未清理：%s", plugin_id, exc_info=True)
            self._pending_installs.pop(plugin_id, None)
            return
        for original, backup in transaction:
            if original.is_dir():
                shutil.rmtree(original)
            else:
                original.unlink(missing_ok=True)
            if backup.exists():
                backup.replace(original)
        self._pending_installs.pop(plugin_id, None)
