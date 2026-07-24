"""
webui/github_import.py
从 GitHub 仓库导入插件，支持「插件市场清单」(manifest)。

来源输入（parse_source 解析）：
  1. raw 文件 URL：https://raw.githubusercontent.com/owner/repo/branch/path/plugin.py
  2. 仓库 URL：https://github.com/owner/repo （可带 /tree/branch/subdir）
  3. 简写：owner/repo 或 owner/repo/subdir

列插件优先级（list_plugins）：
  A. 仓库根/子目录有 manifest.json（或 manifest.v2.json）→ 读清单，渲染插件市场
     （名称/版本/作者/图标/描述/入口路径）。
  B. 无清单 → 回退目录扫描：列 plugins/ 或根目录下的 .py 单文件
     与 <id>/__init__.py 文件夹插件。

清单格式 manifest.json（对象，key=插件 id）：
{
  "jupai": {
    "name": "举牌", "version": "1.0.0", "author": "AW",
    "description": "...", "icon": "https://.../icon.png",
    "path": "jupai.py",            // 单文件：相对仓库根的路径
    "history": {"1.0.0": "首发"}
  },
  "lottery": {
    "name": "抽奖", "version": "2.0.0", ...,
    "path": "lottery/"            // 文件夹：以 / 结尾，下载整个目录
  }
}
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

GITHUB_API = "https://api.github.com"


def _proxy() -> Optional[str]:
    """读取平台代理（启用时返回 httpx 可用的代理 URL），用于访问 GitHub。统一走 libs.proxy。"""
    from libs.proxy import proxy_url
    return proxy_url()
RAW_HOST = "raw.githubusercontent.com"
MANIFEST_NAMES = ("manifest.json", "manifest.v2.json")
_TREE_CACHE_TTL = 30.0
_TREE_CACHE: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_BRANCH_CACHE_TTL = 600.0
_BRANCH_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def _headers() -> dict:
    return {"Accept": "application/vnd.github+json", "User-Agent": "AWBotNest"}


def _raise_if_rate_limited(response) -> None:
    if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        raise RuntimeError("GitHub 请求次数暂时已用完，请稍后再试")


def _bust(url: str) -> str:
    """给 raw URL 追加一次性查询参数，绕过 raw.githubusercontent.com 的 Fastly CDN 缓存
    （默认约 5 分钟 TTL）。否则刚推送的 manifest 版本在 TTL 内刷新仍读到旧值，
    表现为「重启才看到新版本」。Fastly 缓存键含查询串，加随机参数即取最新。"""
    import time
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_ts={int(time.time() * 1000)}"


# raw 读取（manifest / __init__.py 探测）统一带上 no-cache 请求头，配合 _bust 双保险
_NOCACHE = {"Cache-Control": "no-cache", "Pragma": "no-cache"}


def parse_source(src: str) -> dict:
    """解析用户输入 → {kind, ...}"""
    src = src.strip()
    # raw 链接：用 urlparse 精确校验 host == raw.githubusercontent.com，
    # 防 https://raw.githubusercontent.com.evil.com/x.py 这类子串伪造 SSRF
    if src.lower().startswith(("http://", "https://")) and src.endswith(".py"):
        from urllib.parse import urlparse
        if urlparse(src).hostname == RAW_HOST:
            return {"kind": "raw", "url": src}
    # GitHub 网页 blob 文件链接：https://github.com/owner/repo/blob/<branch>/<path>
    # 用户常直接复制地址栏，转成 raw 链接（.py）或带子目录的 repo 形态
    mb = re.match(r"https?://github\.com/([^/]+)/([^/]+?)/blob/([^/]+)/(.+)$", src)
    if mb:
        owner, repo, branch, path = mb.groups()
        if path.endswith(".py"):
            return {"kind": "raw", "url": _raw_url(owner, repo, branch, path)}
        # 非 .py（如指向目录里的文件）→ 当作 repo + 子目录
        subdir = path.rsplit("/", 1)[0] if "/" in path else ""
        return {"kind": "repo", "owner": owner, "repo": repo, "branch": branch, "subdir": subdir}
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.+))?)?/?$",
        src,
    )
    if m:
        owner, repo, branch, subdir = m.groups()
        return {"kind": "repo", "owner": owner, "repo": repo, "branch": branch, "subdir": subdir or ""}
    m = re.match(r"^([^/\s]+)/([^/\s]+)(?:/(.+))?$", src)
    if m:
        owner, repo, subdir = m.groups()
        return {"kind": "repo", "owner": owner, "repo": repo, "branch": None, "subdir": subdir or ""}
    raise ValueError("无法识别的来源；请填 GitHub 仓库地址、owner/repo 或 raw .py 链接")


async def _default_branch(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    cache_key = (owner, repo)
    cached = _BRANCH_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _BRANCH_CACHE_TTL:
        return cached[1]
    r = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_headers())
    _raise_if_rate_limited(r)
    r.raise_for_status()
    branch = r.json().get("default_branch", "main")
    _BRANCH_CACHE[cache_key] = (now, branch)
    return branch


def _raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://{RAW_HOST}/{owner}/{repo}/{branch}/{path.lstrip('/')}"


async def _try_manifest(client, owner, repo, branch, subdir) -> Optional[list[dict]]:
    """尝试读取 manifest，成功返回插件列表，失败返回 None"""
    base = f"{subdir}/" if subdir else ""
    for name in MANIFEST_NAMES:
        url = _raw_url(owner, repo, branch, f"{base}{name}")
        try:
            r = await client.get(_bust(url), headers={**_headers(), **_NOCACHE})
        except Exception:  # noqa: BLE001
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        plugins = []
        for pid, meta in data.items():
            if not isinstance(meta, dict):
                continue
            path = meta.get("path") or (f"{pid}.py")
            is_folder = path.endswith("/")
            plugins.append({
                "id": pid,
                "name": meta.get("name", pid),
                "version": meta.get("version", ""),
                "author": meta.get("author", ""),
                "description": meta.get("description", ""),
                "changelog": str(meta.get("changelog", "") or ""),
                "icon": meta.get("icon", ""),
                "is_folder": is_folder,
                "path": (base + path).strip("/") + ("/" if is_folder else ""),
                "from_manifest": True,
                "owner": owner, "repo": repo, "branch": branch,
            })
        if plugins:
            return plugins
    return None


async def _list_contents(client, owner, repo, branch, subdir) -> list[dict]:
    """目录扫描回退：列 .py 单文件 + <id>/__init__.py 文件夹插件"""
    dirs = [subdir] if subdir else ["plugins", ""]
    for d in dirs:
        api_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{d}".rstrip("/")
        r = await client.get(api_url, headers=_headers(), params={"ref": branch})
        _raise_if_rate_limited(r)
        if r.status_code != 200:
            continue
        items = r.json()
        if not isinstance(items, list):
            continue
        results: list[dict] = []
        for it in items:
            nm = it.get("name", "")
            if it.get("type") == "file" and nm.endswith(".py") and not nm.startswith("_"):
                results.append({
                    "id": nm[:-3], "name": nm[:-3], "version": "", "author": "",
                    "description": "", "changelog": "", "icon": "", "is_folder": False,
                    "path": it["path"], "download_url": it["download_url"],
                    "from_manifest": False, "owner": owner, "repo": repo, "branch": branch,
                })
            elif it.get("type") == "dir" and not nm.startswith("_"):
                # 探测该目录是否含 __init__.py（文件夹插件）
                sub = f"{it['path']}/__init__.py"
                rr = await client.get(_bust(_raw_url(owner, repo, branch, sub)),
                                      headers={**_headers(), **_NOCACHE})
                if rr.status_code == 200:
                    results.append({
                        "id": nm, "name": nm, "version": "", "author": "",
                        "description": "", "changelog": "", "icon": "", "is_folder": True,
                        "path": it["path"] + "/",
                        "from_manifest": False, "owner": owner, "repo": repo, "branch": branch,
                    })
        if results:
            return results
    return []


async def list_plugins(src: str) -> dict:
    """
    列出来源中的插件。返回 {"source_type": "manifest"|"scan"|"raw", "plugins": [...]}
    每个 plugin 含 id/name/version/author/description/changelog/icon/is_folder/path 等。
    """
    info = parse_source(src)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=_proxy()) as client:
        if info["kind"] == "raw":
            name = info["url"].rstrip("/").split("/")[-1]
            return {"source_type": "raw", "plugins": [{
                "id": name[:-3] if name.endswith(".py") else name,
                "name": name[:-3] if name.endswith(".py") else name,
                "version": "", "author": "", "description": "", "changelog": "", "icon": "",
                "is_folder": False, "path": name, "download_url": info["url"],
                "from_manifest": False,
            }]}

        owner, repo = info["owner"], info["repo"]
        branch = info["branch"] or await _default_branch(client, owner, repo)
        subdir = info["subdir"]

        # 优先 manifest
        manifest = await _try_manifest(client, owner, repo, branch, subdir)
        if manifest is not None:
            return {"source_type": "manifest", "plugins": manifest}

        # 回退目录扫描
        scanned = await _list_contents(client, owner, repo, branch, subdir)
        return {"source_type": "scan", "plugins": scanned}


async def _list_dir_files(client, owner, repo, branch, dir_path) -> list[dict]:
    """用一次 Git Trees 请求列出目录文件，避免逐层请求耗尽 GitHub 限额。"""
    cache_key = (str(owner), str(repo), str(branch))
    now = time.monotonic()
    cached = _TREE_CACHE.get(cache_key)
    if cached and now - cached[0] < _TREE_CACHE_TTL:
        tree = cached[1]
    else:
        ref = quote(str(branch), safe="")
        api_url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
        r = await client.get(
            api_url,
            headers={**_headers(), **_NOCACHE},
            params={"recursive": "1"},
        )
        _raise_if_rate_limited(r)
        r.raise_for_status()
        payload = r.json()
        if payload.get("truncated"):
            raise RuntimeError("GitHub 返回的仓库文件列表不完整，请稍后重试")
        tree = payload.get("tree") or []
        _TREE_CACHE[cache_key] = (now, tree)

    prefix = dir_path.strip("/") + "/"
    out: list[dict] = []
    for item in tree:
        path = str(item.get("path") or "")
        if item.get("type") == "blob" and path.startswith(prefix):
            out.append({
                "path": path,
                "download_url": _raw_url(owner, repo, branch, path),
            })
    return out


async def fetch_file(download_url: str) -> bytes:
    """下载单个文件内容（不限大小）。"""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=_proxy()) as client:
        return await _fetch_file(client, download_url)


async def _fetch_file(client: httpx.AsyncClient, download_url: str) -> bytes:
    async with client.stream(
        "GET", _bust(download_url), headers={**_headers(), **_NOCACHE}
    ) as r:
        r.raise_for_status()
        chunks = bytearray()
        async for chunk in r.aiter_bytes():
            chunks.extend(chunk)
        return bytes(chunks)


async def fetch_files(files: list[dict], concurrency: int = 6) -> list[bytes]:
    """复用一个连接池并发下载文件，结果顺序与传入清单一致。"""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=_proxy()) as client:
        async def fetch(item: dict) -> bytes:
            async with semaphore:
                return await _fetch_file(client, item["download_url"])

        return await asyncio.gather(*(fetch(item) for item in files))


async def resolve_files(plugin: dict) -> list[dict]:
    """
    把一个待导入插件解析成具体文件列表（相对插件根的目标路径 + 下载地址）。
    - 单文件：返回 [{target: "<id>.py", download_url}]
    - 文件夹：递归列目录，返回 [{target: "<id>/sub/file.py", download_url}, ...]
    """
    pid = plugin["id"]
    if not plugin.get("is_folder"):
        url = plugin.get("download_url")
        if not url:
            owner, repo, branch = plugin["owner"], plugin["repo"], plugin["branch"]
            url = _raw_url(owner, repo, branch, plugin["path"])
        return [{"target": f"{pid}.py", "download_url": url}]

    # 文件夹插件：递归列出目录内所有文件，target 保留相对结构
    owner, repo, branch = plugin["owner"], plugin["repo"], plugin["branch"]
    dir_path = plugin["path"].rstrip("/")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=_proxy()) as client:
        files = await _list_dir_files(client, owner, repo, branch, dir_path)
    out = []
    for f in files:
        # 把仓库内 dir_path 前缀替换成插件 id，保持目录内相对结构
        rel = f["path"][len(dir_path):].lstrip("/")
        out.append({"target": f"{pid}/{rel}", "download_url": f["download_url"]})
    return out
