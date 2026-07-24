"""
webui/repo_sync.py
插件仓库 / 插件商店 —— 从配置的多个 GitHub 仓库聚合插件列表，按需下载。

模型（见 SPEC §7.6）：
- **插件商店**：聚合所有已配置仓库里的插件，标记哪些「已安装」。前端「插件商店」分区展示，
  用户逐个点「下载」。下载 = 落盘 plugins/，**不自动启用**（启用 = 执行远程代码，须手动）。
- **自动轮询**（默认 20 分钟）：只做两件事 —— ①刷新商店列表缓存；②给「已安装」插件检查
  manifest 版本更新，有更新则下载覆盖（仍不启用，运行中实例需手动重载）。**不自动下载新插件**。
- 商店列表缓存 + 各插件已知版本存 data/repo_sync.json。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:
    httpx = None

from core import logger
from webui import github_import

PLUGINS_DIR = Path("plugins")
STATE_PATH = Path("data") / "repo_sync.json"
JOB_ID = "插件仓库轮询"

# 官方插件仓库：始终内置在商店里，其插件打「官方」标签
OFFICIAL_REPO = "AWdress/AWBotNest-Plugins"


# ──────────────────────────────────────────────
# 状态持久化
# ──────────────────────────────────────────────
def _load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_sync": None, "store": [], "versions": {}}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────
# 路径安全 / 本地探测
# ──────────────────────────────────────────────
def _safe_target(target: str) -> str:
    """校验下载目标相对路径，防路径穿越 / 覆盖模板辅助文件。非法则抛 ValueError。"""
    import os as _os
    t = target.replace("\\", "/")
    if (t.startswith("/") or ".." in t.split("/") or t.startswith("_")
            or _os.path.splitdrive(t)[0] or _os.path.isabs(t)):
        raise ValueError(f"非法目标路径: {target}")
    return t


def _local_exists(plugin_id: str) -> bool:
    """本地是否已存在该插件（单文件或文件夹形态）。"""
    return (PLUGINS_DIR / f"{plugin_id}.py").exists() or (PLUGINS_DIR / plugin_id / "__init__.py").exists()


def _read_local_version(plugin_id: str) -> str:
    """读取本地插件文件中的 __plugin__["version"] 字段。"""
    try:
        # 尝试单文件插件
        single_file = PLUGINS_DIR / f"{plugin_id}.py"
        if single_file.exists():
            content = single_file.read_text(encoding="utf-8")
            return _extract_version_from_content(content)

        # 尝试文件夹插件
        folder_init = PLUGINS_DIR / plugin_id / "__init__.py"
        if folder_init.exists():
            content = folder_init.read_text(encoding="utf-8")
            return _extract_version_from_content(content)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _extract_version_from_content(content: str) -> str:
    """从插件代码中提取版本号（解析 __plugin__ 字典）。"""
    import ast
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__plugin__":
                        if isinstance(node.value, ast.Dict):
                            for key, value in zip(node.value.keys, node.value.values):
                                if (isinstance(key, ast.Constant) and key.value == "version" and
                                    isinstance(value, ast.Constant) and isinstance(value.value, str)):
                                    return value.value
    except Exception:  # noqa: BLE001
        pass
    return ""


def _get_repos() -> list[dict[str, Any]]:
    """读取公开仓库列表：官方仓库始终置顶，再接用户配置的仓库（去重）。"""
    import config.config as cfg
    cfg.reload()
    out: list[dict[str, Any]] = [{"url": OFFICIAL_REPO, "official": True}]
    seen = {OFFICIAL_REPO.casefold()}
    repos = getattr(cfg, "PLUGIN_REPOS", None) or []
    for r in repos:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        key = url.casefold()
        if not url or key in seen:
            continue
        seen.add(key)
        out.append({"url": url, "official": False})
    return out


def _user_repos() -> list[dict[str, Any]]:
    """仅用户配置的仓库（不含官方），用于判断是否需要轮询。"""
    return [r for r in _get_repos() if not r.get("official")]


# ──────────────────────────────────────────────
# 商店列表（聚合多仓库）
# ──────────────────────────────────────────────
async def list_store(refresh: bool = True) -> dict[str, Any]:
    """
    聚合所有已配置仓库的插件列表，标记 installed。
    refresh=True 实时拉取并刷新缓存；refresh=False 直接返回缓存。
    返回 {ok, repos:int, plugins:[...], errors:[...], last_sync}。
    """
    state = _load_state()
    versions: dict[str, str] = state.get("versions") or {}
    if not refresh:
        plugins = state.get("store") or []
        for p in plugins:
            p["installed"] = _local_exists(p["id"])
            # local_version = 平台记录的「已下载版本」，供前端判断是否有更新。
            # 只有从商店下载过的插件才有记录；本地上传/手动导入的没有，前端据此不提示更新。
            p["local_version"] = versions.get(p["id"])
        return {"ok": True, "cached": True, "plugins": plugins,
                "official_ids": state.get("official_ids") or [],
                "errors": [], "last_sync": state.get("last_sync")}

    repos = _get_repos()
    aggregated: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    official_ids: list[str] = []

    for repo in repos:
        try:
            listing = await github_import.list_plugins(repo["url"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{repo['url']}: {e}")
            continue
        for p in listing.get("plugins") or []:
            pid = p.get("id")
            if not pid or pid in seen:
                continue  # 多仓库同 id，先到先得（官方仓库置顶，优先生效）
            seen.add(pid)
            # 注意：不要覆盖 p["repo"]——github_import 用它存「仓库名」来拼 raw URL。
            # 仓库来源地址另存 repo_url，供展示与下载时定位来源。
            p["repo_url"] = repo["url"]
            p["official"] = bool(repo.get("official"))
            p["installed"] = _local_exists(pid)
            p["local_version"] = versions.get(pid)
            if p["official"]:
                official_ids.append(pid)
            aggregated.append(p)

    state["store"] = aggregated
    state["official_ids"] = official_ids
    state["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_state(state)
    return {"ok": True, "cached": False, "plugins": aggregated, "official_ids": official_ids,
            "errors": errors, "last_sync": state["last_sync"]}


def get_store_status() -> dict[str, Any]:
    """供前端读取的轮询状态摘要。"""
    state = _load_state()
    store = state.get("store") or []
    return {
        "last_sync": state.get("last_sync"),
        "store_count": len(store),
        "repos": len(_get_repos()),          # 含官方仓库
        "user_repos": len(_user_repos()),    # 用户自配仓库数
    }


# ──────────────────────────────────────────────
# 下载（按需，单个或多个）
# ──────────────────────────────────────────────
async def download_plugins(plugins: list[dict[str, Any]]) -> dict[str, Any]:
    """
    下载指定插件到 plugins/（不启用）。plugins 为商店列表里的插件对象（含 owner/repo/path 等）。
    返回 {ok, downloaded:[], errors:[]}。
    """
    result: dict[str, Any] = {"ok": False, "downloaded": [], "errors": []}
    state = _load_state()
    versions: dict[str, str] = state.get("versions") or {}
    repos = {r["url"] for r in _get_repos()}

    for plugin in plugins:
        pid = plugin.get("id")
        if not pid or "/" in pid or "\\" in pid or pid.startswith("_"):
            result["errors"].append(f"非法插件 id: {pid}")
            continue
        if plugin.get("repo_url") not in repos:
            result["errors"].append(f"{pid}: 插件来源仓库未配置")
            continue
        try:
            files = await github_import.resolve_files(plugin)
            if not files:
                result["errors"].append(f"{pid}: 无可下载文件")
                continue
            # 先全部下载到内存，全部成功后再落盘——避免文件夹插件中途失败留半截文件
            staged = []
            contents = await github_import.fetch_files(files)
            for f, content in zip(files, contents):
                target = _safe_target(f["target"])
                staged.append((target, content))
            for target, content in staged:
                dest = PLUGINS_DIR / target
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"{pid}: {e}")
            continue
        ver = str(plugin.get("version") or "")
        if ver:
            versions[pid] = ver
        result["downloaded"].append(pid)
        from kernel.registry import registry as _reg
        logger.info("插件商店下载：%s（%d 文件，来自 %s）",
                    _reg.display_name(pid), len(files), plugin.get("repo_url"))

    state["versions"] = versions
    _save_state(state)
    _refresh_registry()
    # 自动重载「更新成功且当前正在运行」的插件，让新代码立即生效。
    # 未加载的插件只更新文件、不启用（保持 §7.5：启用=执行远程代码须手动）。
    reloaded, reload_errors = await _reload_running(result["downloaded"])
    if reloaded:
        result["reloaded"] = reloaded
    if reload_errors:
        result["reload_errors"] = reload_errors
    result["ok"] = bool(result["downloaded"])
    return result


async def _reload_running(plugin_ids: list[str]) -> tuple[list[str], list[str]]:
    """对传入插件中「当前已加载」者执行热重载，使更新后的新代码生效。
    返回成功列表和失败说明。运行时不可用或某插件未加载则跳过。"""
    reloaded: list[str] = []
    errors: list[str] = []
    try:
        from kernel import state as _state
        runtime = _state.runtime
    except Exception:  # noqa: BLE001
        runtime = None
    if runtime is None:
        return reloaded, errors
    for pid in plugin_ids:
        try:
            if not runtime.is_loaded(pid):
                continue  # 未运行的只更新文件，不自动启用
            meta = await runtime.reload(pid)
            if not runtime.is_loaded(pid) or getattr(meta, "error", None):
                detail = getattr(meta, "error", None) or "插件没有重新进入运行状态"
                errors.append(f"{pid}: 文件已更新，但重新加载失败（{detail}）")
                continue
            reloaded.append(pid)
            from kernel.registry import registry as _reg
            logger.info("插件 [%s] 更新后已自动重载", _reg.display_name(pid))
        except Exception as e:  # noqa: BLE001
            from kernel.registry import registry as _reg
            logger.warning("插件 [%s] 更新后自动重载失败: %r", _reg.display_name(pid), e)
            errors.append(f"{pid}: 文件已更新，但重新加载失败（{e}）")
    return reloaded, errors


def _refresh_registry() -> None:
    """重新扫描注册表，让新下载的插件元数据生效（仍不启用）。"""
    try:
        from kernel.registry import registry
        registry.invalidate_scan_cache()
        registry.scan()
    except Exception as e:  # noqa: BLE001
        logger.warning("刷新注册表失败: %r", e)


# ──────────────────────────────────────────────
# 轮询：刷新商店 + 更新已安装插件
# ──────────────────────────────────────────────
async def sync_once() -> dict[str, Any]:
    """
    轮询任务体：①刷新商店列表缓存；②对「已安装」插件，若 manifest 版本变化则下载更新。
    **不自动下载未安装的新插件。**
    """
    listing = await list_store(refresh=True)
    store = listing.get("plugins") or []

    state = _load_state()
    versions: dict[str, str] = state.get("versions") or {}
    updated: list[str] = []
    reloaded: list[str] = []
    errors: list[str] = list(listing.get("errors") or [])

    to_update = []
    versions_changed = False
    for p in store:
        if not p.get("installed"):
            continue  # 只更新已安装的
        if not p.get("from_manifest"):
            continue  # 无版本信号不动
        pid = p["id"]
        prev = versions.get(pid)
        remote_ver = str(p.get("version") or "")

        # 关键：只有「确实从仓库下载过」(versions 里有记录) 的插件才自动更新。
        # 本地上传 / 手动 GitHub 导入 / 与仓库撞 id 的本地插件没有版本记录，
        # 绝不自动覆盖——否则用户的本地改动会被官方仓库同名插件静默冲掉。
        if prev is None:
            # 对于来自已配置仓库的插件，读取本地版本作为基线，下次就能检测更新。
            # 这样预装的官方插件和第三方仓库插件都能进入自动更新流程。
            if remote_ver and p.get("repo_url") in {r["url"] for r in _get_repos()}:
                local_ver = _read_local_version(pid)
                if local_ver:
                    versions[pid] = local_ver
                    versions_changed = True
                    # 如果本地版本低于远程版本，立即加入更新列表
                    if local_ver != remote_ver:
                        to_update.append(p)
            continue

        if remote_ver and prev != remote_ver:
            to_update.append(p)

    if versions_changed:
        state["versions"] = versions
        _save_state(state)

    if to_update:
        dl = await download_plugins(to_update)
        updated = dl.get("downloaded", [])
        reloaded = dl.get("reloaded", [])
        errors.extend(dl.get("errors", []))
        errors.extend(dl.get("reload_errors", []))

    if updated:
        logger.info("插件仓库轮询：更新已安装插件 %d 个 %s%s", len(updated), updated,
                    f"，其中 %d 个已自动重载 %s" % (len(reloaded), reloaded) if reloaded else "")
    return {"ok": True, "store_count": len(store), "updated": updated,
            "reloaded": reloaded, "errors": errors}


# ──────────────────────────────────────────────
# 自动发现插件仓库
# ──────────────────────────────────────────────
async def discover_and_add_repos() -> dict[str, Any]:
    """
    自动发现 GitHub 上的插件仓库：
    1. fork 了官方仓库的项目
    2. 名为 AWBotNest-Plugins 的所有仓库
    检查是否有 manifest.json，有的话自动添加到插件仓库列表中。
    """
    import config.config as cfg

    added: list[str] = []
    errors: list[str] = []
    candidates: set[str] = set()

    if httpx is None:
        errors.append("httpx 未安装，无法发现仓库")
        return {"ok": False, "added": added, "errors": errors}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            from webui.github_import import _headers, _proxy

            # 方式1：列出官方仓库的所有 forks
            try:
                url = f"https://api.github.com/repos/{OFFICIAL_REPO}/forks"
                params = {"per_page": 100, "sort": "newest"}
                resp = await client.get(url, headers=_headers(), params=params, timeout=30)
                resp.raise_for_status()
                forks = resp.json()
                for fork in forks:
                    if isinstance(fork, dict) and fork.get("full_name"):
                        candidates.add(fork["full_name"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"获取 fork 列表失败: {e}")

            # 方式2：搜索所有名为 AWBotNest-Plugins 的仓库
            try:
                search_url = "https://api.github.com/search/repositories"
                search_params = {"q": "AWBotNest-Plugins in:name", "per_page": 100}
                resp = await client.get(search_url, headers=_headers(), params=search_params, timeout=30)
                resp.raise_for_status()
                search_result = resp.json()
                for repo in search_result.get("items", []):
                    if isinstance(repo, dict) and repo.get("full_name"):
                        candidates.add(repo["full_name"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"搜索仓库失败: {e}")

            # 检查每个候选仓库是否有 manifest.json
            existing_repos = {r.get("url", "").casefold() for r in (getattr(cfg, "PLUGIN_REPOS", None) or [])}

            for full_name in candidates:
                # 跳过官方仓库本身
                if full_name.casefold() == OFFICIAL_REPO.casefold():
                    continue

                # 跳过已经添加的仓库
                if full_name.casefold() in existing_repos:
                    continue

                # 检查是否有 manifest.json
                try:
                    owner, repo = full_name.split("/")
                    # 尝试 main 和 master 分支
                    for branch in ["main", "master"]:
                        manifest_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/manifest.json"
                        try:
                            manifest_resp = await client.get(manifest_url, timeout=10)
                            if manifest_resp.status_code == 200:
                                # 尝试解析 JSON 确保格式正确
                                manifest_resp.json()

                                # 添加到配置
                                repos_list = list(getattr(cfg, "PLUGIN_REPOS", None) or [])
                                repos_list.append({"url": full_name})
                                cfg.PLUGIN_REPOS = repos_list
                                cfg.save()

                                added.append(full_name)
                                existing_repos.add(full_name.casefold())
                                logger.info("自动发现并添加插件仓库: %s", full_name)
                                break  # 找到 manifest 就不再尝试其他分支
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    pass

    except Exception as e:  # noqa: BLE001
        errors.append(f"发现仓库失败: {e}")

    return {"ok": True, "added": added, "errors": errors}


# ──────────────────────────────────────────────
# 定时任务注册 / 重排
# ──────────────────────────────────────────────
def reschedule() -> Optional[object]:
    """注册 / 重排轮询任务（强制常开）。设置变更后调用以刷新间隔。"""
    import config.config as cfg
    cfg.reload()
    from schedulers import scheduler

    repos = _get_repos()  # 始终含官方仓库
    interval = int(getattr(cfg, "PLUGIN_REPO_INTERVAL", 20) or 20)
    interval = max(1, interval)
    job = scheduler.add_job(
        sync_once, "interval", minutes=interval, id=JOB_ID, replace_existing=True,
    )
    logger.info("插件仓库轮询已注册：每 %d 分钟，%d 个仓库（含官方）", interval, len(repos))

    # 注册每天0点自动发现插件仓库的任务
    discover_job = scheduler.add_job(
        discover_and_add_repos,
        "cron",
        hour=0,
        minute=0,
        id="插件仓库自动发现",
        replace_existing=True,
    )
    logger.info("插件仓库自动发现已注册：每天 0:00 执行")

    return job
