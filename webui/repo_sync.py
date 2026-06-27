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
    t = target.replace("\\", "/")
    if t.startswith("/") or ".." in t.split("/") or t.startswith("_"):
        raise ValueError(f"非法目标路径: {target}")
    return t


def _local_exists(plugin_id: str) -> bool:
    """本地是否已存在该插件（单文件或文件夹形态）。"""
    return (PLUGINS_DIR / f"{plugin_id}.py").exists() or (PLUGINS_DIR / plugin_id / "__init__.py").exists()


def _get_repos() -> list[dict[str, Any]]:
    """读取仓库列表 [{url, token, official}]：官方仓库始终置顶，再接用户配置的仓库（去重）。"""
    import config.config as cfg
    cfg.reload()
    out: list[dict[str, Any]] = [{"url": OFFICIAL_REPO, "token": None, "official": True}]
    seen = {OFFICIAL_REPO}
    repos = getattr(cfg, "PLUGIN_REPOS", None) or []
    for r in repos:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "token": (r.get("token") or "").strip() or None, "official": False})
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
    if not refresh:
        plugins = state.get("store") or []
        for p in plugins:
            p["installed"] = _local_exists(p["id"])
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
            listing = await github_import.list_plugins(repo["url"], repo["token"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{repo['url']}: {e}")
            continue
        for p in listing.get("plugins") or []:
            pid = p.get("id")
            if not pid or pid in seen:
                continue  # 多仓库同 id，先到先得（官方仓库置顶，优先生效）
            seen.add(pid)
            # 注意：不要覆盖 p["repo"]——github_import 用它存「仓库名」来拼 raw URL。
            # 仓库来源地址另存 repo_url，供展示与下载时按 token 匹配。
            p["repo_url"] = repo["url"]
            p["official"] = bool(repo.get("official"))
            p["installed"] = _local_exists(pid)
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
    repos = {r["url"]: r["token"] for r in _get_repos()}

    for plugin in plugins:
        pid = plugin.get("id")
        if not pid or "/" in pid or "\\" in pid or pid.startswith("_"):
            result["errors"].append(f"非法插件 id: {pid}")
            continue
        token = repos.get(plugin.get("repo_url"))  # 用该插件所属仓库的 token
        try:
            files = await github_import.resolve_files(plugin, token)
            if not files:
                result["errors"].append(f"{pid}: 无可下载文件")
                continue
            for f in files:
                target = _safe_target(f["target"])
                content = await github_import.fetch_file(f["download_url"], token)
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
        logger.info("插件商店下载：%s（%d 文件，来自 %s）", pid, len(files), plugin.get("repo_url"))

    state["versions"] = versions
    _save_state(state)
    _refresh_registry()
    result["ok"] = bool(result["downloaded"])
    return result


def _refresh_registry() -> None:
    """重新扫描注册表，让新下载的插件元数据生效（仍不启用）。"""
    try:
        from kernel.registry import registry
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
    errors: list[str] = list(listing.get("errors") or [])

    to_update = []
    for p in store:
        if not p.get("installed"):
            continue  # 只更新已安装的
        if not p.get("from_manifest"):
            continue  # 无版本信号不动
        remote_ver = str(p.get("version") or "")
        if remote_ver and versions.get(p["id"]) != remote_ver:
            to_update.append(p)

    if to_update:
        dl = await download_plugins(to_update)
        updated = dl.get("downloaded", [])
        errors.extend(dl.get("errors", []))

    if updated:
        logger.info("插件仓库轮询：更新已安装插件 %d 个 %s", len(updated), updated)
    return {"ok": True, "store_count": len(store), "updated": updated, "errors": errors}


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
    return job
