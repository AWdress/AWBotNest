from __future__ import annotations

import shutil
import zipfile
import sqlite3
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import APP_ROOT, DATA_DIR, PLUGINS_DIR, SESSIONS_DIR

BACKUP_DIR = DATA_DIR / "backups"
PENDING_RESTORE = DATA_DIR / ".restore-pending.zip"
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
                    def ignore(directory, names):
                        return [name for name in names if name in {"backups", ".restore-pending.zip", ".restore-pending.tmp", ".v1-migration-pending.zip", ".v1-migration-pending.tmp"}
                                or (Path(directory) / name).is_symlink()
                                or name.endswith(("-wal", "-shm", "-journal"))]

                    def copy_file(src, dst):
                        with open(src, "rb") as stream:
                            sqlite = stream.read(16) == b"SQLite format 3\x00"
                        if sqlite:
                            original = sqlite3.connect(Path(src).resolve().as_uri() + "?mode=ro", uri=True)
                            snapshot = sqlite3.connect(dst)
                            try:
                                original.backup(snapshot)
                            finally:
                                snapshot.close()
                                original.close()
                            return dst
                        return shutil.copy2(src, dst)

                    shutil.copytree(source, temporary / name, ignore=ignore, copy_function=copy_file)
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
                if (name.is_absolute() or ".." in name.parts or ":" in item.filename
                        or "\\" in item.orig_filename or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError("备份包含不安全路径")
                if len(name.parts) == 1 and not item.is_dir():
                    raise ValueError("备份根节点必须是目录")
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
        PENDING_RESTORE.parent.mkdir(parents=True, exist_ok=True)
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
        staging = DATA_DIR / "backups" / f".restore-{stamp}"
        rollback = DATA_DIR / "backups" / f"rollback-{stamp}"
        staging.mkdir(parents=True)
        rollback.mkdir(parents=True)
        targets = {"data": DATA_DIR, "plugins": PLUGINS_DIR, "sessions": SESSIONS_DIR}
        moved: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        rolled_back = False
        try:
            with zipfile.ZipFile(PENDING_RESTORE) as archive:
                archive.extractall(staging)
            for name, target in targets.items():
                incoming = staging / name
                if not incoming.exists():
                    continue
                # Docker 的挂载根目录不能 rename；只移动目录内容，并保留备份与待恢复包。
                target.mkdir(parents=True, exist_ok=True)
                old = rollback / name
                old.mkdir()
                for child in list(target.iterdir()):
                    if target == DATA_DIR and child.name in {"backups", PENDING_RESTORE.name}:
                        continue
                    saved = old / child.name
                    shutil.move(str(child), str(saved))
                    moved.append((saved, child))
                for child in incoming.iterdir():
                    if target == DATA_DIR and child.name in {"backups", PENDING_RESTORE.name}:
                        continue
                    destination = target / child.name
                    installed.append(destination)
                    shutil.move(str(child), str(destination))
            PENDING_RESTORE.unlink()
            return True
        except Exception:
            for target in reversed(installed):
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
            for old, target in reversed(moved):
                shutil.move(str(old), str(target))
            rolled_back = True
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            # 成功时也保留旧数据；回滚本身失败时绝不能删除最后一份原件。
            if rolled_back:
                shutil.rmtree(rollback, ignore_errors=True)
