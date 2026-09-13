from __future__ import annotations

import asyncio
import importlib.metadata
import importlib
import subprocess
import sys
import re
import logging
from pathlib import Path

from .config import DATA_DIR, Settings
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

logger = logging.getLogger("awbotnest.deps")


class DependencyManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.target = DATA_DIR / "plugin_deps"
        self.target.mkdir(parents=True, exist_ok=True)
        target_text = str(self.target)
        if target_text not in sys.path:
            sys.path.append(target_text)
        self._lock = asyncio.Lock()

    def missing(self, requirements: list[str], *, target_only: bool = False) -> list[str]:
        result = []
        target_versions = {
            canonicalize_name(distribution.metadata.get("Name") or ""): distribution.version
            for distribution in importlib.metadata.distributions(path=[str(self.target)])
        } if target_only else {}
        for requirement in requirements:
            parsed = Requirement(requirement)
            name = parsed.name
            if not name:
                continue
            try:
                version = (target_versions.get(canonicalize_name(name))
                           if target_only else importlib.metadata.version(name))
                if version is None:
                    raise importlib.metadata.PackageNotFoundError(name)
                if parsed.specifier and not parsed.specifier.contains(version, prereleases=True):
                    result.append(requirement)
            except importlib.metadata.PackageNotFoundError:
                result.append(requirement)
        return result

    def target_version(self, package: str) -> str:
        wanted = canonicalize_name(package)
        for distribution in importlib.metadata.distributions(path=[str(self.target)]):
            if canonicalize_name(distribution.metadata.get("Name") or "") == wanted:
                return distribution.version
        return ""

    @staticmethod
    def validate(requirements: list[str]) -> None:
        if len(requirements) > 50:
            raise ValueError("单个插件最多声明 50 个 Python 依赖")
        for requirement in requirements:
            try:
                parsed = Requirement(requirement)
            except InvalidRequirement as exc:
                raise ValueError(f"插件依赖声明不合法：{requirement}") from exc
            if parsed.url or parsed.marker:
                raise ValueError(f"插件依赖声明不合法：{requirement}")

    async def ensure(self, requirements: list[str], *, plugin_name: str = "插件",
                     target_only: bool = False, upgrade: bool = False) -> None:
        self.validate(requirements)
        missing = list(requirements) if upgrade else self.missing(requirements, target_only=target_only)
        if not missing:
            return
        async with self._lock:
            missing = (list(requirements) if upgrade
                       else self.missing(requirements, target_only=target_only))
            if not missing:
                return
            command = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--target",
                str(self.target),
                "--upgrade",
            ]
            if self.settings.proxy_url:
                command.extend(["--proxy", self.settings.proxy_url])
            if self.settings.pip_index_url:
                command.extend(["--index-url", self.settings.pip_index_url])
            command.extend(missing)
            logger.info("%s 正在安装依赖：%s", plugin_name, ", ".join(missing))
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=300)
            except TimeoutError as exc:
                process.kill()
                await process.communicate()
                logger.error("%s 依赖安装失败：超过 5 分钟", plugin_name)
                raise RuntimeError("插件依赖安装超过 5 分钟，已停止") from exc
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                raise
            if process.returncode:
                tail = output.decode(errors="replace")[-2000:]
                tail = re.sub(r"(://)[^/@\s:]+:[^/@\s]+@", r"\1***:***@", tail)
                logger.error("%s 依赖安装失败：%s", plugin_name, tail)
                raise RuntimeError(f"插件依赖安装失败：{tail}")
            importlib.invalidate_caches()
            remaining = self.missing(requirements, target_only=target_only)
            if remaining:
                raise RuntimeError("依赖安装后版本仍不满足（可能与系统依赖冲突）：" + ", ".join(remaining))
            logger.info("%s 依赖安装完成：%s", plugin_name, ", ".join(missing))
