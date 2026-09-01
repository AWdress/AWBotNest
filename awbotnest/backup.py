from __future__ import annotations

import shutil
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import APP_ROOT, DATA_DIR, PLUGINS_DIR, SESSIONS_DIR

BACKUP_DIR = DATA_DIR / "backups"
PENDING_RESTORE = APP_ROOT / ".restore-pending.zip"
MAX_BACKUP_SIZE = 512 * 1024 * 1024


class BackupManager:
    @staticmethod
    def create() -> Path:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        base = BACKUP_DIR / f"AWBotNest-{stamp}"
        temporary = APP_ROOT / f".backup-{stamp}"
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            for source, name in ((DATA_DIR, "data"), (PLUGINS_DIR, "plugins"), (SESSIONS_DIR, "sessions")):
                if source.exists():
                    shutil.copytree(source, temporary / name, ignore=shutil.ignore_patterns("backups"))
            return Path(shutil.make_archive(str(base), "zip", temporary))
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def list() -> list[Path]:
        return sorted(BACKUP_DIR.glob("AWBotNest-*.zip"), reverse=True) if BACKUP_DIR.exists() else []

    @staticmethod
    def validate(path: Path) -> None:
        if not path.exists() or path.stat().st_size > MAX_BACKUP_SIZE:
            raise ValueError("备份不存在或超过 512 MB")
        with zipfile.ZipFile(path) as archive:
            roots: set[str] = set()
            total = 0
            for item in archive.infolist():
                name = PurePosixPath(item.filename.replace("\\", "/"))
                if name.is_absolute() or ".." in name.parts:
                    raise ValueError("备份包含不安全路径")
                if name.parts:
                    roots.add(name.parts[0])
                total += item.file_size
                if total > MAX_BACKUP_SIZE * 2:
                    raise ValueError("备份解压内容过大")
            if not roots or not roots.issubset({"data", "plugins", "sessions"}):
                raise ValueError("不是有效的 AWBotNest 备份")

    @classmethod
    def stage(cls, content: bytes) -> None:
        if len(content) > MAX_BACKUP_SIZE:
            raise ValueError("备份超过 512 MB")
        temporary = PENDING_RESTORE.with_suffix(".tmp")
        temporary.write_bytes(content)
        try:
            cls.validate(temporary)
            temporary.replace(PENDING_RESTORE)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @classmethod
    def apply_pending(cls) -> bool:
        if not PENDING_RESTORE.exists():
            return False
        cls.validate(PENDING_RESTORE)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        staging = APP_ROOT / f".restore-{stamp}"
        rollback = APP_ROOT / f".rollback-{stamp}"
        staging.mkdir()
        rollback.mkdir()
        targets = {"data": DATA_DIR, "plugins": PLUGINS_DIR, "sessions": SESSIONS_DIR}
        moved: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        try:
            with zipfile.ZipFile(PENDING_RESTORE) as archive:
                archive.extractall(staging)
            for name, target in targets.items():
                incoming = staging / name
                if not incoming.exists():
                    continue
                if target.exists():
                    old = rollback / name
                    target.replace(old)
                    moved.append((old, target))
                incoming.replace(target)
                installed.append(target)
            PENDING_RESTORE.unlink()
            return True
        except Exception:
            for target in reversed(installed):
                if target.exists():
                    shutil.rmtree(target)
            for old, target in reversed(moved):
                old.replace(target)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(rollback, ignore_errors=True)
