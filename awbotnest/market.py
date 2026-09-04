from __future__ import annotations

import re
import shutil
import tempfile
import ast
import time
import logging
import json
import uuid
import asyncio
from packaging.version import InvalidVersion, Version
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from .config import PLUGINS_DIR, Settings, save_settings

MANIFEST_NAME = "manifest_v2.json"
PLUGIN_HEAT_SERVER_URL = "http://115.231.35.106:18002"
OFFICIAL_REPO = "AWdress/AWBotNest-Plugins"
logger = logging.getLogger("awbotnest.market")
REPO_PATTERN = re.compile(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


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


class PluginMarket:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.install_lock = asyncio.Lock()
        self._pending_installs: dict[str, tuple[Path, Path]] = {}
        self._cache: dict[str, Any] | None = None
        self._cache_until = 0.0
        self._state_path = PLUGINS_DIR.parent / "data" / "repo_sync.json"
        self._last_sync: str | None = None
        self._skipped_manifest_logged: set[str] = set()
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
        return self._cache if self._cache is not None else {
            "plugins": [], "errors": [], "install_counts": {},
            "official_ids": [], "last_sync": None, "manifest": MANIFEST_NAME,
        }

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

    async def record_install(self, plugin: dict[str, Any], event_type: str = "install") -> None:
        """记录本地安装热度并尽力上报中心；网络失败不影响安装。"""
        state_path = PLUGINS_DIR.parent / "data" / "plugin_heat_state.json"
        state = self._heat_state()
        plugin_id = str(plugin.get("id") or "").strip()
        if not plugin_id:
            return
        installs = state.setdefault("installs", {})
        installs[plugin_id] = max(0, int(installs.get(plugin_id, 0) or 0)) + 1
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        event = {
            "event_id": str(uuid.uuid4()), "installation_id": str(state["installation_id"]),
            "plugin_id": plugin_id,
            "event_type": event_type if event_type in {"install", "update"} else "install",
            "version": str(plugin.get("version") or "")[:64], "app_version": "2",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4, connect=2)) as client:
                await client.post(f"{PLUGIN_HEAT_SERVER_URL}/api/plugin-heat/events", json={"events": [event]})
        except Exception:
            logger.debug("插件安装热度上报暂时失败：%s", plugin_id)

    async def poll_updates(self, runtime: Any) -> dict[str, Any]:
        async with self.install_lock:
            return await self._poll_updates(runtime)

    async def _poll_updates(self, runtime: Any) -> dict[str, Any]:
        """刷新市场并自动更新已安装且有新版本的插件。"""
        listing = await self.refresh()
        updated: list[str] = []
        errors: list[str] = []
        for plugin in listing.get("plugins", []):
            if not plugin.get("installed") or not plugin.get("update_available"):
                continue
            plugin_id = str(plugin.get("id") or "")
            plugin_name = str(plugin.get("name") or runtime.display_name(plugin_id))
            was_loaded = plugin_id in runtime.loaded
            try:
                if was_loaded: await runtime.disable(plugin_id, persist=False)
                await self.install(plugin)
                if was_loaded:
                    result = await runtime.enable(plugin_id)
                    if result.error: raise RuntimeError(result.error)
                self.finish(plugin_id, True)
                await self.record_install(plugin, "update")
                updated.append(plugin_name)
            except Exception as exc:
                errors.append(f"{plugin_name}：{exc}（已跳过，继续更新其他插件）")
                try:
                    self.finish(plugin_id, False)
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
        for plugin_id, raw in entries.items():
            if not isinstance(raw, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(plugin_id)):
                continue
            scope = str(raw.get("scope") or "user")
            if not self.settings.telegram_configured and scope in {"user", "both"}:
                continue
            source_path = _safe_path(str(raw.get("path") or f"{plugin_id}.py"))
            installed_version = self._installed_version(str(plugin_id))
            remote_version = str(raw.get("version") or "0.0.0")
            is_official = repo.casefold() == OFFICIAL_REPO.casefold()
            plugins.append({
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
            })
        return {"repo": repo, "branch": branch, "manifest": MANIFEST_NAME, "plugins": plugins}

    async def _install_counts(self) -> dict[str, int]:
        """读取全局插件热度；中心不可用时不影响插件商店。"""
        state = self._heat_state()
        local = state.get("installs") or {}
        for entry in PLUGINS_DIR.iterdir() if PLUGINS_DIR.exists() else ():
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
                plugin_id = entry.stem if entry.is_file() and entry.suffix == ".py" else (entry.name if entry.is_dir() and (entry / "__init__.py").exists() else "")
                if plugin_id and plugin_id not in local:
                    local[plugin_id] = 1
            for plugin_id, count in local.items():
                counts.setdefault(str(plugin_id), int(count or 0))
            return counts
        except Exception:
            return self.local_install_counts()

    @staticmethod
    def _installed_version(plugin_id: str) -> str | None:
        entry = PLUGINS_DIR / f"{plugin_id}.py"
        if not entry.exists():
            entry = PLUGINS_DIR / plugin_id / "__init__.py"
        if not entry.exists():
            return None
        try:
            tree = ast.parse(entry.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "__plugin__" for target in node.targets
                ):
                    value = ast.literal_eval(node.value)
                    return str(value.get("version") or "0.0.0") if isinstance(value, dict) else None
        except Exception:
            return None
        return None

    @staticmethod
    def _newer(remote: str, installed: str | None) -> bool:
        if installed is None:
            return False
        try:
            return Version(remote) > Version(installed)
        except InvalidVersion:
            return remote != installed

    async def list_all(self) -> dict[str, Any]:
        if self._cache is not None and time.monotonic() < self._cache_until:
            return self._cache
        stale_cache = self._cache
        plugins: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        # 官方仓库是内置来源，不依赖旧配置是否曾经保存过它；否则从
        # 只配置第三方仓库时也应显示官方插件。
        repos = [OFFICIAL_REPO, *self.settings.plugin_repos]
        seen_repos: set[str] = set()
        for repo in repos:
            try:
                repo = normalize_repo(repo)
            except Exception as exc:
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
                        logger.info("插件仓库提示：已跳过不含 V2 清单的仓库 %s", repo)
                        self._skipped_manifest_logged.add(repo.casefold())
                    continue
                errors.append(f"{repo}: {exc}")
                continue
            for plugin in listing["plugins"]:
                if plugin["id"] not in seen:
                    seen.add(plugin["id"])
                    plugins.append(plugin)
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
        self._state_path.write_text(json.dumps({"store": self._cache, "last_sync": self._last_sync}, ensure_ascii=False), encoding="utf-8")
        self._cache_until = time.monotonic() + 300
        return self._cache

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
            backup = PLUGINS_DIR / f".{plugin_id}.backup.py"
            if plugin_id in self._pending_installs:
                raise RuntimeError("该插件已有安装任务")
            backup.unlink(missing_ok=True)
            if destination.exists():
                shutil.copy2(destination, backup)
            temp = destination.with_suffix(".py.tmp")
            temp.write_bytes(content)
            self._pending_installs[plugin_id] = (destination, backup)
            try:
                temp.replace(destination)
            except Exception:
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
            self._pending_installs[plugin_id] = (destination, backup)
            try:
                shutil.copytree(staged, destination)
            except Exception:
                self.finish(plugin_id, False)
                raise
            return destination

    def finish(self, plugin_id: str, success: bool) -> None:
        transaction = self._pending_installs.get(plugin_id)
        if transaction is None:
            return  # 下载/校验失败尚未修改插件，不能删除原安装。
        destination, backup = transaction
        if not success:
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            if backup.exists():
                backup.replace(destination)
        elif backup.is_dir():
            shutil.rmtree(backup)
        else:
            backup.unlink(missing_ok=True)
        self._pending_installs.pop(plugin_id, None)
