from __future__ import annotations

import asyncio
import importlib.metadata
import importlib
import subprocess
import sys
import re
import logging
import os
import shutil
import stat
from pathlib import Path
from weakref import WeakKeyDictionary, WeakValueDictionary

from .config import DATA_DIR, Settings
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion

logger = logging.getLogger("awbotnest.deps")


class DependencyManager:
    # One target can be shared by the plugin runtime and browser component manager.
    # Weak values avoid retaining closed event loops through a contended Lock.
    _install_locks: WeakKeyDictionary = WeakKeyDictionary()

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.target = DATA_DIR / "plugin_deps"
        self.target.mkdir(parents=True, exist_ok=True)
        target_text = str(self.target)
        if target_text not in sys.path:
            sys.path.append(target_text)

    def _install_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        locks = self._install_locks.setdefault(loop, WeakValueDictionary())
        target = os.path.normcase(str(self.target.resolve()))
        lock = locks.get(target)
        if lock is None:
            lock = asyncio.Lock()
            locks[target] = lock
        return lock

    @staticmethod
    def _metadata_path(distribution: importlib.metadata.Distribution) -> Path | None:
        path = getattr(distribution, "_path", None)
        return Path(path) if isinstance(path, (str, os.PathLike)) else None

    @classmethod
    def _metadata_stamp(cls, distribution: importlib.metadata.Distribution) -> tuple[int, ...]:
        path = cls._metadata_path(distribution)
        if path is None:
            return (0, 0, 0, 0)
        result = []
        # pip writes RECORD at installation time, even if wheel contents are older.
        # Choose the last installation, not the highest version (downgrades exist).
        for item in (path / "RECORD", path / "METADATA"):
            try:
                stat = item.stat()
                result.extend((stat.st_mtime_ns, stat.st_size))
            except OSError:
                result.extend((0, 0))
        return tuple(result)

    @classmethod
    def _metadata_rank(cls, distribution: importlib.metadata.Distribution) -> tuple[int, ...]:
        stamp = cls._metadata_stamp(distribution)
        path = cls._metadata_path(distribution)
        try:
            directory_time = path.stat().st_mtime_ns if path is not None else 0
        except OSError:
            directory_time = 0
        # File sizes identify a changed record, not which version was installed last.
        return (stamp[0], stamp[2], directory_time)

    @staticmethod
    def _metadata_link(path: Path) -> bool:
        try:
            info = path.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            return stat.S_ISLNK(info.st_mode) or bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        except OSError:
            return True

    def _distribution(self, package: str, *, target_only: bool) -> importlib.metadata.Distribution | None:
        wanted = canonicalize_name(package)
        kwargs = {"name": package}
        if target_only:
            kwargs["path"] = [str(self.target)]
        matches = []
        first_root = None
        for distribution in importlib.metadata.distributions(**kwargs):
            if canonicalize_name(distribution.metadata.get("Name") or "") != wanted:
                continue
            root = os.path.normcase(str(Path(str(distribution.locate_file(""))).resolve()))
            if first_root is None:
                first_root = root
            # Preserve normal import precedence: a later target must not hide an
            # incompatible dependency in an earlier global site-packages directory.
            if root == first_root:
                matches.append(distribution)
        if not matches:
            return None
        newest = max(matches, key=self._metadata_rank)
        rank = self._metadata_rank(newest)
        if any(self._metadata_rank(item) == rank and item.version != newest.version for item in matches):
            # Identical copied timestamps cannot prove which code was installed.
            # Let ensure repair ambiguous metadata rather than report a guessed version.
            return None
        return newest

    def _satisfied(self, parsed: Requirement, *, target_only: bool,
                   inspect_dependencies: bool, seen: set, distributions: dict) -> bool:
        name = canonicalize_name(parsed.name)
        distribution_key = (name, target_only)
        if distribution_key not in distributions:
            distributions[distribution_key] = self._distribution(parsed.name, target_only=target_only)
        distribution = distributions[distribution_key]
        if distribution is None or not distribution.version:
            return False
        try:
            if parsed.specifier and not parsed.specifier.contains(distribution.version, prereleases=True):
                return False
        except InvalidVersion:
            return False
        if not inspect_dependencies:
            return True
        key = (name, tuple(sorted(parsed.extras)), target_only)
        if key in seen:
            return True
        seen.add(key)
        for declaration in distribution.requires or []:
            try:
                dependency = Requirement(declaration)
            except InvalidRequirement:
                return False
            if dependency.marker and not any(
                dependency.marker.evaluate({"extra": extra})
                for extra in {"", *parsed.extras}
            ):
                continue
            # The root component may require persisted installation, but its
            # imports still use normal sys.path precedence for other packages.
            if not self._satisfied(dependency, target_only=False,
                                   inspect_dependencies=True, seen=seen, distributions=distributions):
                return False
        return True

    def missing(self, requirements: list[str], *, target_only: bool = False) -> list[str]:
        result = []
        distributions = {}
        for requirement in requirements:
            parsed = Requirement(requirement)
            # An installed base package does not imply requested extras are present.
            # Follow their dependency closure, including Requires-Dist markers.
            if not self._satisfied(parsed, target_only=target_only,
                                   inspect_dependencies=bool(parsed.extras),
                                   seen=set(), distributions=distributions):
                result.append(requirement)
        return result

    def target_version(self, package: str) -> str:
        distribution = self._distribution(package, target_only=True)
        return (distribution.version or "") if distribution is not None else ""

    def _extra_install_requirements(self, requirements: list[str], *, target_only: bool) -> list[str]:
        result = []
        for item in requirements:
            parsed = Requirement(item)
            if parsed.extras:
                distribution = self._distribution(parsed.name, target_only=target_only)
                if distribution is not None and distribution.version:
                    try:
                        if parsed.specifier.contains(distribution.version, prereleases=True):
                            # Filling an extra must not silently replace a root package
                            # already imported by another plugin with a newer version.
                            parsed.specifier &= SpecifierSet(f"=={distribution.version}")
                            item = str(parsed)
                    except InvalidVersion:
                        pass
            result.append(item)
        return result

    def _target_metadata(self) -> dict[Path, tuple[str, tuple[int, ...]]]:
        target = self.target.resolve()
        result = {}
        for distribution in importlib.metadata.distributions(path=[str(self.target)]):
            path = self._metadata_path(distribution)
            if (path is None or not path.name.endswith(".dist-info") or self._metadata_link(path)
                    or path.parent.resolve() != target):
                continue
            name = canonicalize_name(distribution.metadata.get("Name") or "")
            if name:
                result[path] = (name, self._metadata_stamp(distribution))
        return result

    def _remove_stale_metadata(self, before: dict) -> None:
        current = self._target_metadata()
        changed = {}
        for path, entry in current.items():
            if before.get(path) != entry:
                changed.setdefault(entry[0], []).append(path)
        target = self.target.resolve()
        for name, paths in changed.items():
            keep = max(paths, key=lambda path: (current[path][1][0], current[path][1][2]))
            rank = (current[keep][1][0], current[keep][1][2])
            if sum((current[path][1][0], current[path][1][2]) == rank for path in paths) > 1:
                continue
            for path, entry in current.items():
                if entry[0] != name or path == keep:
                    continue
                # Only obsolete metadata for packages pip actually replaced. Never
                # remove package code, other packages, symlinks or global metadata.
                if self._metadata_link(path) or path.resolve().parent != target:
                    continue
                try:
                    shutil.rmtree(path)
                except OSError:
                    logger.warning("依赖旧版本信息清理失败：%s", name)

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
        if not requirements:
            return
        async with self._install_lock():
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
            command.extend(missing if upgrade else self._extra_install_requirements(missing, target_only=target_only))
            metadata_before = self._target_metadata()
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
            self._remove_stale_metadata(metadata_before)
            importlib.invalidate_caches()
            remaining = self.missing(requirements, target_only=target_only)
            if remaining:
                raise RuntimeError("依赖安装后版本仍不满足（可能与系统依赖冲突）：" + ", ".join(remaining))
            logger.info("%s 依赖安装完成：%s", plugin_name, ", ".join(missing))
