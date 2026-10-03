from __future__ import annotations

import json
import hashlib
import io
import re
import shutil
import stat
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import DATA_DIR, validate_config_format


BACKUP_DIR = DATA_DIR / "backups"
PENDING_RESTORE = DATA_DIR / ".restore-pending.zip"
MAX_BACKUP_SIZE = 16 * 1024 * 1024
MAX_CONFIG_SIZE = 8 * 1024 * 1024
BACKUP_FORMAT = "awbotnest-config"
BACKUP_VERSION = 1


class BackupManager:
    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def create() -> Path:
        config_path = DATA_DIR / "config.json"
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            validate_config_format(raw)
        except FileNotFoundError as exc:
            raise ValueError("系统配置文件不存在") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("系统配置文件无法读取") from exc

        system_config = dict(raw)
        plugin_config = system_config.pop("plugin_config", {})
        if not isinstance(plugin_config, dict):
            plugin_config = {}

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = BACKUP_DIR / f"AWBotNest-config-{stamp}.zip"
        temporary = BACKUP_DIR / f".{target.name}.tmp"
        manifest = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "contents": ["system_config", "plugin_config"],
        }
        system_bytes = BackupManager._json_bytes(system_config)
        plugin_bytes = BackupManager._json_bytes(plugin_config)
        if len(system_bytes) > MAX_CONFIG_SIZE or len(plugin_bytes) > MAX_CONFIG_SIZE:
            raise ValueError("系统配置或插件配置超过 8 MB")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", BackupManager._json_bytes(manifest))
                archive.writestr("config/system.json", system_bytes)
                archive.writestr("config/plugins.json", plugin_bytes)
            temporary.replace(target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def list() -> list[Path]:
        return sorted(BACKUP_DIR.glob("AWBotNest-*.zip"), reverse=True) if BACKUP_DIR.exists() else []

    @staticmethod
    def _safe_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
        members: dict[str, zipfile.ZipInfo] = {}
        for item in archive.infolist():
            name = PurePosixPath(item.filename.replace("\\", "/"))
            if (name.is_absolute() or ".." in name.parts or ":" in item.filename
                    or "\\" in item.orig_filename or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError("备份包含不安全路径")
            if not item.is_dir():
                if name.as_posix() in members:
                    raise ValueError("备份包含重复文件")
                members[name.as_posix()] = item
        return members

    @classmethod
    def _read_archive(cls, archive: zipfile.ZipFile) -> dict[str, object]:
        members = cls._safe_members(archive)
        if {"manifest.json", "config/system.json", "config/plugins.json"}.issubset(members):
            manifest = cls._read_json(archive, members["manifest.json"], 64 * 1024, "备份描述")
            if manifest.get("format") != BACKUP_FORMAT or manifest.get("version") != BACKUP_VERSION:
                raise ValueError("不支持的 AWBotNest 配置备份版本")
            system_config = cls._read_json(
                archive, members["config/system.json"], MAX_CONFIG_SIZE, "系统配置",
            )
            plugin_config = cls._read_json(
                archive, members["config/plugins.json"], MAX_CONFIG_SIZE, "插件配置",
            )
            system_config.pop("plugin_config", None)
            system_config["plugin_config"] = plugin_config
            config = system_config
        elif "data/config.json" in members:
            # 兼容旧版完整备份，但只读取其中的配置，不解压其他运行数据。
            config = cls._read_json(
                archive, members["data/config.json"], MAX_CONFIG_SIZE, "系统配置",
            )
        else:
            raise ValueError("不是有效的 AWBotNest 配置备份")

        validate_config_format(config)
        plugin_config = config.get("plugin_config", {})
        if not isinstance(plugin_config, dict):
            raise ValueError("备份中的插件配置格式不正确")
        return config

    @classmethod
    def _read_config(cls, path: Path) -> dict[str, object]:
        if not path.exists() or path.stat().st_size > MAX_BACKUP_SIZE:
            raise ValueError("配置备份不存在或超过 16 MB")
        with zipfile.ZipFile(path) as archive:
            return cls._read_archive(archive)

    @classmethod
    def _read_content(cls, content: bytes) -> dict[str, object]:
        if len(content) > MAX_BACKUP_SIZE:
            raise ValueError("配置备份超过 16 MB")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return cls._read_archive(archive)

    @staticmethod
    def _config_digest(config: dict[str, object]) -> str:
        packed = json.dumps(config, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(packed).hexdigest()

    @staticmethod
    def _current_config() -> dict[str, object]:
        path = DATA_DIR / "config.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("当前系统配置无法读取") from exc
        validate_config_format(value)
        return value

    @classmethod
    def preview(cls, content: bytes) -> dict[str, object]:
        incoming = cls._read_content(content)
        current = cls._current_config()
        incoming_system = {key: value for key, value in incoming.items() if key != "plugin_config"}
        current_system = {key: value for key, value in current.items() if key != "plugin_config"}
        incoming_plugins = incoming.get("plugin_config") if isinstance(incoming.get("plugin_config"), dict) else {}
        current_plugins = current.get("plugin_config") if isinstance(current.get("plugin_config"), dict) else {}

        def changes(before: dict[str, object], after: dict[str, object]) -> dict[str, list[str]]:
            before_keys, after_keys = set(before), set(after)
            return {
                "added": sorted(after_keys - before_keys),
                "changed": sorted(key for key in before_keys & after_keys if before[key] != after[key]),
                "removed": sorted(before_keys - after_keys),
                "unchanged": sorted(key for key in before_keys & after_keys if before[key] == after[key]),
            }

        system = changes(current_system, incoming_system)
        plugins = changes(current_plugins, incoming_plugins)
        summary = {
            "system_added": len(system["added"]),
            "system_changed": len(system["changed"]),
            "system_removed": len(system["removed"]),
            "plugins_added": len(plugins["added"]),
            "plugins_changed": len(plugins["changed"]),
            "plugins_removed": len(plugins["removed"]),
        }
        return {
            "ok": True,
            "digest": hashlib.sha256(content).hexdigest(),
            "current_digest": cls._config_digest(current),
            "summary": summary,
            "system": system,
            "plugins": plugins,
        }

    @staticmethod
    def _read_json(archive: zipfile.ZipFile, item: zipfile.ZipInfo,
                   maximum: int, label: str) -> dict[str, object]:
        if item.file_size > maximum:
            raise ValueError(f"备份中的{label}过大")
        try:
            value = json.loads(archive.read(item).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
            raise ValueError(f"备份中的{label}无法读取") from exc
        if not isinstance(value, dict):
            raise ValueError(f"备份中的{label}格式不正确")
        return value

    @classmethod
    def validate(cls, path: Path) -> None:
        cls._read_config(path)

    @classmethod
    def stage(cls, content: bytes, *, expected_digest: str = "",
              expected_current_digest: str = "") -> None:
        if len(content) > MAX_BACKUP_SIZE:
            raise ValueError("配置备份超过 16 MB")
        if expected_digest and not hashlib.sha256(content).hexdigest() == expected_digest:
            raise ValueError("配置包已发生变化，请重新预览")
        if expected_current_digest:
            current_digest = cls._config_digest(cls._current_config())
            if current_digest != expected_current_digest:
                raise ValueError("当前配置在预览后已发生变化，请重新预览")
        cls._read_content(content)
        PENDING_RESTORE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PENDING_RESTORE.with_suffix(".tmp")
        temporary.write_bytes(content)
        try:
            temporary.replace(PENDING_RESTORE)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @classmethod
    def apply_pending(cls) -> bool:
        # A previous process may have stopped between replacement and readiness.
        # Never repeatedly boot an unconfirmed restored configuration.
        if cls._transaction_path().exists():
            cls.rollback_restore()
        if not PENDING_RESTORE.exists():
            return False
        config = cls._read_config(PENDING_RESTORE)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = DATA_DIR / "config.json"
        rollback = BACKUP_DIR / f"before-config-restore-{stamp}.json"
        temporary = DATA_DIR / ".config-restore.tmp"
        if target.exists():
            shutil.copy2(target, rollback)
        transaction = cls._transaction_path()
        transaction_temp = transaction.with_suffix(".tmp")
        transaction_temp.write_text(json.dumps({
            "rollback": rollback.name, "had_config": target.exists(),
        }), encoding="utf-8")
        transaction_temp.replace(transaction)
        try:
            temporary.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            temporary.replace(target)
            PENDING_RESTORE.unlink()
            return True
        except Exception:
            temporary.unlink(missing_ok=True)
            cls.rollback_restore()
            raise

    @staticmethod
    def _transaction_path() -> Path:
        return DATA_DIR / ".restore-transaction.json"

    @classmethod
    def commit_restore(cls) -> None:
        """Confirm the imported configuration only after the system is ready."""
        cls._transaction_path().unlink(missing_ok=True)

    @classmethod
    def rollback_restore(cls) -> bool:
        """Recover the previous file after startup failure or interrupted startup."""
        transaction = cls._transaction_path()
        if not transaction.exists():
            return False
        marker = json.loads(transaction.read_text(encoding="utf-8"))
        rollback_name = marker.get("rollback", "") if isinstance(marker, dict) else ""
        if not isinstance(rollback_name, str) or not re.fullmatch(
                r"before-config-restore-[0-9-]+\.json", rollback_name):
            raise ValueError("配置恢复记录格式不正确")
        target = DATA_DIR / "config.json"
        rollback = BACKUP_DIR / rollback_name
        if marker.get("had_config") and not rollback.is_file():
            raise ValueError("恢复前的配置备份不存在")
        if target.exists():
            failed = BACKUP_DIR / rollback_name.replace("before-config-restore-", "failed-config-restore-")
            shutil.copy2(target, failed)
        temporary = DATA_DIR / ".config-rollback.tmp"
        try:
            if marker.get("had_config"):
                shutil.copy2(rollback, temporary)
                temporary.replace(target)
            else:
                target.unlink(missing_ok=True)
            PENDING_RESTORE.unlink(missing_ok=True)
            transaction.unlink()
            return True
        finally:
            temporary.unlink(missing_ok=True)
