"""
webui/github_import.py
从 GitHub 仓库导入插件，支持「插件市场清单」(manifest)。

来源输入（parse_source 解析）：
  1. raw 文件 URL：https://raw.githubusercontent.com/owner/repo/branch/path/plugin.py
  2. 仓库 URL：https://github.com/owner/repo （可带 /tree/branch/subdir）
  3. 简写：owner/repo 或 owner/repo/subdir

列插件优先级（list_plugins）：
  A. 仓库根/子目录有 manifest.json（或 manifest.v2.json）→ 读清单，渲染插件市场
     （名称/版本/作者/图标/描述/入口路径），MoviePilot 风格。
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

import re
from typing import Optional

import httpx

GITHUB_API = "https://api.github.com"


def _proxy() -> Optional[str]:
    """读取平台代理（启用时返回 httpx 可用的代理 URL），用于访问 GitHub。"""
    try:
        import config.config as _cfg
        _cfg.reload()
        ps = getattr(_cfg, "proxy_set", {}) or {}
        if not ps.get("proxy_enable"):
            return None
        url = (ps.get("PROXY_URL") or "").strip()
        if url:
            return url
        # 无 PROXY_URL 时按 proxy 子项拼
        px = ps.get("proxy", {}) or {}
        host, port = px.get("hostname"), px.get("port")
        if host and port:
            scheme = px.get("scheme", "http")
            user, pwd = px.get("username", ""), px.get("password", "")
            auth = f"{user}:{pwd}@" if user else ""
            return f"{scheme}://{auth}{host}:{port}"
    except Exception:  # noqa: BLE001
        pass
    return None
RAW_HOST = "raw.githubusercontent.com"
MANIFEST_NAMES = ("manifest.json", "manifest.v2.json")


def _headers(token: Optional[str]) -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "AWBotNest"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


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


async def _default_branch(client: httpx.AsyncClient, owner: str, repo: str, token) -> str:
    r = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_headers(token))
    r.raise_for_status()
    return r.json().get("default_branch", "main")


def _raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://{RAW_HOST}/{owner}/{repo}/{branch}/{path.lstrip('/')}"


async def _try_manifest(client, owner, repo, branch, subdir, token) -> Optional[list[dict]]:
    """尝试读取 manifest，成功返回插件列表，失败返回 None"""
    base = f"{subdir}/" if subdir else ""
    for name in MANIFEST_NAMES:
        url = _raw_url(owner, repo, branch, f"{base}{name}")
        try:
            r = await client.get(url, headers=_headers(token))
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
                "icon": meta.get("icon", ""),
                "is_folder": is_folder,
                "path": (base + path).strip("/") + ("/" if is_folder else ""),
                "from_manifest": True,
                "owner": owner, "repo": repo, "branch": branch,
            })
        if plugins:
            return plugins
    return None


async def _list_contents(client, owner, repo, branch, subdir, token) -> list[dict]:
    """目录扫描回退：列 .py 单文件 + <id>/__init__.py 文件夹插件"""
    dirs = [subdir] if subdir else ["plugins", ""]
    for d in dirs:
        api_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{d}".rstrip("/")
        r = await client.get(api_url, headers=_headers(token), params={"ref": branch})
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
                    "description": "", "icon": "", "is_folder": False,
                    "path": it["path"], "download_url": it["download_url"],
                    "from_manifest": False, "owner": owner, "repo": repo, "branch": branch,
                })
            elif it.get("type") == "dir" and not nm.startswith("_"):
                # 探测该目录是否含 __init__.py（文件夹插件）
                sub = f"{it['path']}/__init__.py"
                rr = await client.get(_raw_url(owner, repo, branch, sub), headers=_headers(token))
                if rr.status_code == 200:
                    results.append({
                        "id": nm, "name": nm, "version": "", "author": "",
                        "description": "", "icon": "", "is_folder": True,
                        "path": it["path"] + "/",
                        "from_manifest": False, "owner": owner, "repo": repo, "branch": branch,
                    })
        if results:
            return results
    return []


async def list_plugins(src: str, token: Optional[str] = None) -> dict:
    """
    列出来源中的插件。返回 {"source_type": "manifest"|"scan"|"raw", "plugins": [...]}
    每个 plugin 含 id/name/version/author/description/icon/is_folder/path 等。
    """
    info = parse_source(src)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=_proxy()) as client:
        if info["kind"] == "raw":
            name = info["url"].rstrip("/").split("/")[-1]
            return {"source_type": "raw", "plugins": [{
                "id": name[:-3] if name.endswith(".py") else name,
                "name": name[:-3] if name.endswith(".py") else name,
                "version": "", "author": "", "description": "", "icon": "",
                "is_folder": False, "path": name, "download_url": info["url"],
                "from_manifest": False,
            }]}

        owner, repo = info["owner"], info["repo"]
        branch = info["branch"] or await _default_branch(client, owner, repo, token)
        subdir = info["subdir"]

        # 优先 manifest
        manifest = await _try_manifest(client, owner, repo, branch, subdir, token)
        if manifest is not None:
            return {"source_type": "manifest", "plugins": manifest}

        # 回退目录扫描
        scanned = await _list_contents(client, owner, repo, branch, subdir, token)
        return {"source_type": "scan", "plugins": scanned}


async def _list_dir_files(client, owner, repo, branch, dir_path, token) -> list[dict]:
    """递归列出某目录下所有文件（用于下载文件夹插件），返回 [{path, download_url}]"""
    out: list[dict] = []
    api_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{dir_path.rstrip('/')}"
    r = await client.get(api_url, headers=_headers(token), params={"ref": branch})
    if r.status_code != 200:
        return out
    for it in r.json():
        if it.get("type") == "file":
            out.append({"path": it["path"], "download_url": it["download_url"]})
        elif it.get("type") == "dir":
            out.extend(await _list_dir_files(client, owner, repo, branch, it["path"], token))
    return out


async def fetch_file(download_url: str, token: Optional[str] = None) -> bytes:
    """下载单个文件内容。限制最大体积，防超大文件耗尽内存。"""
    max_bytes = 8 * 1024 * 1024  # 单文件上限 8MB（插件源码足够）
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=_proxy()) as client:
        async with client.stream("GET", download_url, headers=_headers(token)) as r:
            r.raise_for_status()
            cl = r.headers.get("content-length")
            if cl and int(cl) > max_bytes:
                raise ValueError(f"文件过大（{int(cl)} 字节，上限 {max_bytes}）")
            chunks = bytearray()
            async for chunk in r.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise ValueError(f"文件超过大小上限 {max_bytes} 字节")
            return bytes(chunks)


async def resolve_files(plugin: dict, token: Optional[str] = None) -> list[dict]:
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
        files = await _list_dir_files(client, owner, repo, branch, dir_path, token)
    out = []
    for f in files:
        # 把仓库内 dir_path 前缀替换成插件 id，保持目录内相对结构
        rel = f["path"][len(dir_path):].lstrip("/")
        out.append({"target": f"{pid}/{rel}", "download_url": f["download_url"]})
    return out
