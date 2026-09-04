from __future__ import annotations

import asyncio
import importlib.metadata
import subprocess
import sys
import re
import logging
from pathlib import Path

from .config import DATA_DIR, Settings
from packaging.requirements import InvalidRequirement, Requirement

logger = logging.getLogger("awbotnest.deps")


class DependencyManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.target = DATA_DIR / "plugin_deps"
        self.target.mkdir(parents=True, exist_ok=True)
        target_text = str(self.target)
        if target_text not in sys.path:
            sys.path.insert(0, target_text)
        self._lock = asyncio.Lock()

    def missing(self, requirements: list[str]) -> list[str]:
        result = []
        for requirement in requirements:
            parsed = Requirement(requirement)
            name = parsed.name
            if not name:
                continue
            try:
                version = importlib.metadata.version(name)
                if parsed.specifier and not parsed.specifier.contains(version, prereleases=True):
                    result.append(requirement)
            except importlib.metadata.PackageNotFoundError:
                result.append(requirement)
        return result

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

    async def ensure(self, requirements: list[str], *, plugin_name: str = "插件") -> None:
        self.validate(requirements)
        missing = self.missing(requirements)
        if not missing:
            return
        logger.info("%s 需要安装依赖：%s", plugin_name, ", ".join(missing))
        async with self._lock:
            missing = self.missing(requirements)
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
            logger.info("%s 依赖已安装：%s", plugin_name, ", ".join(missing))
