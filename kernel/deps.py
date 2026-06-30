"""
kernel/deps.py
插件依赖管理：插件在 __plugin__["requirements"] 里**声明**依赖（PEP 508 字符串），
平台在「启用」时统一代为安装。插件自己不调 pip。

为什么这么设计（见 SPEC §3.5）：
- 平台是**单进程热插拔**，所有插件 import 进同一个解释器。同一个包在一个进程里
  只能有一个版本生效——无法为不同插件隔离版本。
- 因此唯一正确的做法是：装之前先拿「当前已安装环境」做冲突检测。已安装环境就是
  事实来源（它已反映先前启用插件装进来的版本）。
    · 已满足 → 跳过
    · 缺失   → pip 安装
    · 冲突（已装了不兼容的版本）→ **拒绝启用**，明确告诉用户冲突在哪，
      绝不强行覆盖（否则会把别的插件/平台依赖的库静默换掉）。
- 安全：每条 requirement 都先经 packaging.Requirement 解析，只把规范化后的形式
  作为独立 argv 传给 pip（不走 shell），杜绝参数注入。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packaging.requirements import Requirement

from libs.log import logger

# 插件运行时依赖的安装目录。装进**已挂载的 data/ 卷**而非镜像 site-packages，
# 这样容器重建/拉新镜像后依赖不丢（site-packages 在容器可写层，重建即丢失）。
# 用 pip --target 装到这里，并把该目录加进 sys.path 供 import 与版本探测。
PLUGIN_DEPS_DIR = Path(os.getcwd()) / "data" / "plugin_deps"


def ensure_on_path() -> str:
    """确保插件依赖目录存在且在 sys.path 上（幂等）。返回目录绝对路径字符串。
    放到 sys.path **末尾**：平台自带依赖优先，plugin_deps 只补平台没有的，
    避免插件装的包意外遮蔽平台/其它插件正在用的版本。"""
    PLUGIN_DEPS_DIR.mkdir(parents=True, exist_ok=True)
    p = str(PLUGIN_DEPS_DIR.resolve())
    if p not in sys.path:
        sys.path.append(p)
    return p


# 模块导入即挂上 sys.path——deps 在启用插件前就被 import，保证 check() 与后续
# 插件 setup 里的 import 都能看到 data/plugin_deps 里已持久化的包。
ensure_on_path()


def _proxy() -> str | None:
    """读取平台代理（启用时返回 pip 可用的代理 URL）。墙内环境装依赖需走代理，
    否则连不上 pypi.org。复用 config.proxy_set，与 GitHub/AI 访问同一套设置。"""
    try:
        import config.config as _cfg
        _cfg.reload()
        ps = getattr(_cfg, "proxy_set", {}) or {}
        if not ps.get("proxy_enable"):
            return None
        url = (ps.get("PROXY_URL") or "").strip()
        if url:
            return url
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


def check(reqs: list[str]) -> dict[str, Any]:
    """对照当前已安装环境分类依赖。
    返回 {satisfied, missing, conflict, invalid}：
      - satisfied: [规范化 spec]   已装且版本满足
      - missing:   [规范化 spec]   未安装，需 pip 装
      - conflict:  [{req, installed}]  已装但版本不兼容（同进程无法共存）
      - invalid:   [(原始串, 原因)]   依赖声明本身无法解析
    """
    satisfied: list[str] = []
    missing: list[str] = []
    conflict: list[dict[str, str]] = []
    invalid: list[tuple[str, str]] = []

    for raw in reqs:
        if not isinstance(raw, str) or not raw.strip():
            invalid.append((str(raw), "空或非字符串"))
            continue
        try:
            req = Requirement(raw.strip())
        except Exception as e:  # noqa: BLE001 - InvalidRequirement 等
            invalid.append((raw, str(e)))
            continue
        # 环境标记不匹配（如 sys_platform）则该依赖在本环境无需安装
        if req.marker is not None and not req.marker.evaluate():
            continue
        spec = str(req)  # 规范化形式，安全传给 pip
        try:
            installed = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            missing.append(spec)
            continue
        except Exception as e:  # noqa: BLE001 - 元数据损坏等异常，当作缺失让 pip 重装
            logger.warning("读取已装包 [%s] 版本异常，按缺失处理: %r", req.name, e)
            missing.append(spec)
            continue
        # 带 extras（如 httpx[socks]）：本体已装不代表 extra 的附带依赖也在，
        # importlib.metadata 无法可靠判断 extra 是否齐全。交给 pip（幂等，已满足会快速跳过），
        # 不走「已满足短路」，避免漏装 extra 依赖。
        if req.extras:
            missing.append(spec)
            continue
        if not req.specifier:
            satisfied.append(spec)  # 无版本约束，已装即可
            continue
        try:
            ok = req.specifier.contains(installed, prereleases=True)
        except Exception:  # noqa: BLE001 - 畸形已装版本号
            ok = False
        if ok:
            satisfied.append(spec)
        else:
            conflict.append({"req": spec, "installed": installed})

    return {"satisfied": satisfied, "missing": missing,
            "conflict": conflict, "invalid": invalid}


def _index_url() -> str | None:
    """读取 pip 镜像源（PIP_INDEX_URL）。默认清华源，留空则走官方 pypi。"""
    try:
        import config.config as _cfg
        _cfg.reload()
        url = (getattr(_cfg, "PIP_INDEX_URL", "") or "").strip()
        return url or None
    except Exception:  # noqa: BLE001
        return None


def _pip_install(specs: list[str]) -> tuple[bool, str]:
    """同步调用 pip 安装（在线程里跑，勿直接在事件循环调用）。
    装到 data/plugin_deps（已挂载卷，容器重建不丢）；优先走 pip 镜像源（PIP_INDEX_URL，
    默认清华，境内直连不经墙），未配镜像才回退官方 pypi 并套平台代理出墙。
    限制重试/超时，连不上时快速失败，不长时间占着 pip 锁。"""
    target = ensure_on_path()
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
           "--retries", "1", "--timeout", "15",
           # --target 装到持久化目录；--upgrade 让已存在的包可被覆盖更新（幂等）
           "--target", target, "--upgrade"]
    index = _index_url()
    if index:
        # 走境内镜像：直连即可，不套代理（代理会把请求绕出境，反而更慢/更不稳）。
        cmd += ["--index-url", index]
        host = urlparse(index).hostname
        if host:
            # 个别网络对镜像也做 TLS 干扰，trusted-host 容错（镜像本身有合法证书时此项无害）
            cmd += ["--trusted-host", host]
    else:
        # 官方 pypi：墙内需走平台代理
        proxy = _proxy()
        if proxy:
            cmd += ["--proxy", proxy]
    cmd += specs
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:  # noqa: BLE001 - 超时/找不到 pip 等
        return False, f"pip 调用失败: {e!r}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def _pip_check() -> set[str]:
    """跑 `pip check`，返回环境不一致行的集合（空集=一致）。
    用于装前/装后差分，只把「新增」的不一致归因到本次安装。
    通过 PYTHONPATH 把 data/plugin_deps 纳入检查范围，否则 pip check 看不到 --target
    装进去的包，体检形同虚设。"""
    cmd = [sys.executable, "-m", "pip", "check", "--disable-pip-version-check"]
    env = dict(os.environ)
    target = str(PLUGIN_DEPS_DIR.resolve())
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = target + (os.pathsep + prev if prev else "")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except Exception:  # noqa: BLE001 - pip check 失败不该挡启用，忽略
        return set()
    if proc.returncode == 0:
        return set()
    text = (proc.stdout or proc.stderr or "")
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


async def ensure(plugin_id: str, reqs: list[str]) -> dict[str, Any]:
    """启用插件前确保依赖就绪。
    返回 {ok, installed:[], error}：
      - 依赖声明非法 / 版本冲突 → ok=False，error 说明原因，**不安装**。
      - 缺失依赖 → pip 安装；成功 ok=True 并列出 installed。
      - 全部已满足 → ok=True，installed 为空。
    """
    if not reqs:
        return {"ok": True, "installed": []}

    info = check(reqs)

    if info["invalid"]:
        msg = "；".join(f"{r}（{why}）" for r, why in info["invalid"])
        return {"ok": False, "installed": [], "error": f"依赖声明非法: {msg}"}

    if info["conflict"]:
        msg = "；".join(
            f"需要 {c['req']}，但已安装 {c['installed']}（同进程不能共存两个版本）"
            for c in info["conflict"]
        )
        logger.warning("插件 [%s] 依赖冲突，拒绝启用: %s", plugin_id, msg)
        return {"ok": False, "installed": [], "error": f"依赖冲突，已拒绝启用: {msg}"}

    missing = info["missing"]
    if not missing:
        return {"ok": True, "installed": []}

    # 装前快照环境不一致状态，用于事后差分（排除本来就存在、与本插件无关的不一致）
    before = await asyncio.to_thread(_pip_check)

    logger.info("插件 [%s] 安装依赖: %s", plugin_id, missing)
    ok, out = await asyncio.to_thread(_pip_install, missing)
    if not ok:
        tail = " | ".join(out.strip().splitlines()[-5:]) or "无输出"
        logger.warning("插件 [%s] 依赖安装失败: %s", plugin_id, tail)
        return {"ok": False, "installed": [], "error": f"依赖安装失败: {tail}"}

    # 让 import 系统看到新装进 site-packages 的包
    importlib.invalidate_caches()
    logger.info("插件 [%s] 依赖安装完成: %s", plugin_id, missing)

    # 装完体检：pip 装缺失包时可能顺带升/降级了平台/别的插件依赖的包，
    # 这类传递冲突不在本插件声明里、check 看不到。用装前/装后差分只报「新增」的
    # 不一致，避免把环境里本就存在、与本插件无关的问题扣到插件头上。
    # 发现新增不一致就警告（不挡启用——包确实装上了，但让用户知道环境可能被搞乱了）。
    result: dict[str, Any] = {"ok": True, "installed": missing}
    after = await asyncio.to_thread(_pip_check)
    new_broken = after - before
    if new_broken:
        warn = " | ".join(sorted(new_broken))
        logger.warning("插件 [%s] 装依赖后环境出现新的不一致（可能殃及平台/其它插件）: %s",
                       plugin_id, warn)
        result["warning"] = f"依赖已装，但环境出现新的不一致，可能影响平台或其它插件: {warn}"
    return result
