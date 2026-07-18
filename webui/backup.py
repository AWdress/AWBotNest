"""Safe backup creation and staged restore support."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath


BACKUP_ROOTS = ("data", "sessions", "db_file", "plugins")
BACKUP_STORE = Path("data") / "backups"
PENDING_RESTORE = Path("data") / ".restore_pending.zip"

MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024  # 1 GiB compressed upload
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB extracted data
MAX_ARCHIVE_FILES = 100_000
COPY_CHUNK_SIZE = 1024 * 1024

_MANIFEST_NAME = "backup_manifest.json"
_SKIPPED_SUFFIXES = ("-journal", "-wal", "-shm")


class BackupError(ValueError):
    """Raised when a backup archive is unsafe or invalid."""


@dataclass(frozen=True)
class BackupInspection:
    file_count: int
    expanded_bytes: int
    members: tuple[zipfile.ZipInfo, ...]


def _is_excluded(path: Path) -> bool:
    normalized = path.as_posix()
    return (
        normalized == BACKUP_STORE.as_posix()
        or normalized.startswith(f"{BACKUP_STORE.as_posix()}/")
        or normalized == PENDING_RESTORE.as_posix()
        or path.name.startswith(".restore-upload-")
        or path.name.endswith(_SKIPPED_SUFFIXES)
    )


def _iter_backup_files():
    for root_name in BACKUP_ROOTS:
        root = Path(root_name)
        if not root.exists():
            continue
        if root.is_file():
            if not _is_excluded(root):
                yield root
            continue
        for item in root.rglob("*"):
            if item.is_file() and not _is_excluded(item):
                yield item


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _write_sqlite_snapshot(zf: zipfile.ZipFile, source: Path, arcname: str) -> None:
    fd, snapshot_name = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    snapshot = Path(snapshot_name)
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, timeout=30, uri=True)) as src:
            with closing(sqlite3.connect(str(snapshot))) as dst:
                src.backup(dst)
        zf.write(snapshot, arcname=arcname)
    finally:
        snapshot.unlink(missing_ok=True)


def create_backup_archive(app_version: str, output_dir: Path | None = None) -> tuple[Path, str]:
    """Create a unique backup archive and return its path and download name."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"awbotnest-backup-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    target_dir = output_dir or (Path(tempfile.gettempdir()) / "awbotnest-backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    archive_path = target_dir / filename

    manifest = {
        "app": "AWBotNest",
        "version": app_version,
        "format": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "included_roots": list(BACKUP_ROOTS),
    }

    try:
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            for root_name in BACKUP_ROOTS:
                zf.writestr(f"{root_name}/", b"")
            for file_path in _iter_backup_files():
                arcname = file_path.as_posix()
                if _is_sqlite_file(file_path):
                    _write_sqlite_snapshot(zf, file_path, arcname)
                else:
                    zf.write(file_path, arcname=arcname)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    return archive_path, filename


def _safe_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw_name = info.filename.replace("\\", "/")
    if not raw_name or raw_name.startswith("/") or "\x00" in raw_name:
        raise BackupError(f"备份包路径非法: {info.filename}")

    path = PurePosixPath(raw_name)
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise BackupError(f"备份包路径非法: {info.filename}")
    if ":" in parts[0]:
        raise BackupError(f"备份包路径非法: {info.filename}")
    if parts[0] not in BACKUP_ROOTS:
        raise BackupError(f"备份包包含不允许的路径: {info.filename}")
    if parts[:2] == ("data", "backups") or (parts[0] == "data" and parts[-1].startswith(".restore-")):
        raise BackupError(f"备份包包含平台保留路径: {info.filename}")

    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise BackupError(f"备份包不允许符号链接: {info.filename}")
    if info.flag_bits & 0x1:
        raise BackupError(f"备份包不支持加密条目: {info.filename}")
    return parts


def inspect_backup_archive(archive_path: Path) -> BackupInspection:
    """Validate every member before any platform file is changed."""
    try:
        archive_size = archive_path.stat().st_size
    except OSError as exc:
        raise BackupError(f"无法读取备份包: {exc}") from exc
    if archive_size <= 0:
        raise BackupError("备份包为空")
    if archive_size > MAX_ARCHIVE_BYTES:
        raise BackupError("备份包超过 1 GiB 上传限制")

    try:
        with zipfile.ZipFile(archive_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise BackupError("备份包文件数量过多")

            names: set[str] = set()
            members: list[zipfile.ZipInfo] = []
            expanded_bytes = 0
            manifest_info: zipfile.ZipInfo | None = None

            for info in infos:
                normalized = info.filename.replace("\\", "/").rstrip("/")
                if normalized == _MANIFEST_NAME:
                    if normalized in names:
                        raise BackupError(f"备份包包含重复路径: {info.filename}")
                    names.add(normalized)
                    manifest_info = info
                    continue
                parts = _safe_member_parts(info)
                canonical_name = "/".join(parts)
                if canonical_name in names:
                    raise BackupError(f"备份包包含重复路径: {info.filename}")
                names.add(canonical_name)
                if info.is_dir():
                    continue
                if info.file_size < 0 or info.compress_size < 0:
                    raise BackupError(f"备份包条目大小非法: {info.filename}")
                expanded_bytes += info.file_size
                if expanded_bytes > MAX_EXPANDED_BYTES:
                    raise BackupError("备份包解压后超过 4 GiB 限制")
                members.append(info)

            if manifest_info is None or manifest_info.file_size > 64 * 1024:
                raise BackupError("不是有效的 AWBotNest 备份包")
            try:
                manifest = json.loads(zf.read(manifest_info).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
                raise BackupError("备份清单格式无效") from exc
            if not isinstance(manifest, dict):
                raise BackupError("备份清单格式无效")
            if manifest.get("app") != "AWBotNest" or manifest.get("format", 1) != 1:
                raise BackupError("备份包版本或来源无效")
            included_roots = manifest.get("included_roots")
            if not isinstance(included_roots, list) or not all(isinstance(item, str) for item in included_roots):
                raise BackupError("备份包目录清单无效")
            if set(included_roots) != set(BACKUP_ROOTS):
                raise BackupError("备份包目录清单不完整")

            try:
                damaged_member = zf.testzip()
            except (RuntimeError, OSError) as exc:
                raise BackupError("备份包内容校验失败") from exc
            if damaged_member:
                raise BackupError(f"备份包文件已损坏: {damaged_member}")

            return BackupInspection(len(members), expanded_bytes, tuple(members))
    except zipfile.BadZipFile as exc:
        raise BackupError("备份包格式无效") from exc


def _extract_archive(archive_path: Path, target: Path, inspection: BackupInspection) -> None:
    target_resolved = target.resolve()
    for root_name in BACKUP_ROOTS:
        (target / root_name).mkdir(parents=True, exist_ok=True)

    total_written = 0
    with zipfile.ZipFile(archive_path) as zf:
        for info in inspection.members:
            parts = _safe_member_parts(info)
            destination = target.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            resolved = destination.resolve()
            try:
                resolved.relative_to(target_resolved)
            except ValueError:
                raise BackupError(f"备份包路径越界: {info.filename}")

            member_written = 0
            with zf.open(info, "r") as source, destination.open("xb") as output:
                while True:
                    chunk = source.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    member_written += len(chunk)
                    total_written += len(chunk)
                    if member_written > info.file_size or total_written > MAX_EXPANDED_BYTES:
                        raise BackupError(f"备份包条目大小异常: {info.filename}")
                    output.write(chunk)


def stage_restore_archive(upload_path: Path, app_version: str) -> tuple[BackupInspection, str]:
    """Validate an upload, save a rollback snapshot, and stage it for restart."""
    inspection = inspect_backup_archive(upload_path)
    rollback_path, rollback_name = create_backup_archive(app_version, BACKUP_STORE)
    try:
        PENDING_RESTORE.parent.mkdir(parents=True, exist_ok=True)
        os.replace(upload_path, PENDING_RESTORE)
    except Exception:
        rollback_path.unlink(missing_ok=True)
        raise
    return inspection, rollback_name


def prune_stored_backups(keep: int = 5) -> None:
    """Bound sensitive rollback snapshots if a browser never downloads them."""
    if not BACKUP_STORE.is_dir():
        return
    backups = sorted(BACKUP_STORE.glob("awbotnest-backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in backups[max(keep, 0):]:
        stale.unlink(missing_ok=True)


def stored_backup_path(filename: str) -> Path:
    if not filename or Path(filename).name != filename or not filename.endswith(".zip"):
        raise BackupError("备份文件名非法")
    path = (BACKUP_STORE / filename).resolve()
    store = BACKUP_STORE.resolve()
    if store not in path.parents or not path.is_file():
        raise BackupError("备份文件不存在")
    return path


def apply_pending_restore() -> int:
    """Apply a staged restore before services open databases or session files."""
    if not PENDING_RESTORE.is_file():
        return 0

    inspection = inspect_backup_archive(PENDING_RESTORE)
    cwd = Path.cwd().resolve()
    stage = Path(tempfile.mkdtemp(prefix=".awbotnest-restore-", dir=str(cwd)))
    rollback = Path(tempfile.mkdtemp(prefix=".awbotnest-rollback-", dir=str(cwd)))
    preserved_backups = rollback / "stored-backups"
    replaced: list[str] = []

    try:
        _extract_archive(PENDING_RESTORE, stage, inspection)

        if BACKUP_STORE.exists():
            preserved_backups.parent.mkdir(parents=True, exist_ok=True)
            os.replace(BACKUP_STORE, preserved_backups)

        for root_name in BACKUP_ROOTS:
            current = cwd / root_name
            old = rollback / root_name
            incoming = stage / root_name
            if current.exists():
                os.replace(current, old)
            replaced.append(root_name)
            os.replace(incoming, current)

        if preserved_backups.exists():
            BACKUP_STORE.parent.mkdir(parents=True, exist_ok=True)
            os.replace(preserved_backups, BACKUP_STORE)
    except Exception:
        for root_name in reversed(replaced):
            current = cwd / root_name
            old = rollback / root_name
            if current.exists():
                shutil.rmtree(current) if current.is_dir() else current.unlink()
            if old.exists():
                os.replace(old, current)
        if preserved_backups.exists() and not BACKUP_STORE.exists():
            BACKUP_STORE.parent.mkdir(parents=True, exist_ok=True)
            os.replace(preserved_backups, BACKUP_STORE)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    shutil.rmtree(rollback, ignore_errors=True)
    PENDING_RESTORE.unlink(missing_ok=True)
    return inspection.file_count
