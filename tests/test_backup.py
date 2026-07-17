from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from webui.backup import (
    BACKUP_ROOTS,
    BACKUP_STORE,
    PENDING_RESTORE,
    BackupError,
    apply_pending_restore,
    create_backup_archive,
    inspect_backup_archive,
    stage_restore_archive,
)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cwd = Path.cwd()
        self._temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self._temp_dir.name)
        for root in BACKUP_ROOTS:
            Path(root).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._temp_dir.cleanup()

    def test_staged_restore_replaces_snapshot_and_preserves_rollback(self) -> None:
        Path("data/config.json").write_text('{"state":"backup"}', encoding="utf-8")
        Path("plugins/keep.py").write_text("VALUE = 1\n", encoding="utf-8")

        session = sqlite3.connect("sessions/account.session")
        try:
            session.execute("CREATE TABLE sample (value TEXT)")
            session.execute("INSERT INTO sample VALUES ('saved')")
            session.commit()

            export_dir = Path("exports")
            archive, _ = create_backup_archive("test", export_dir)
        finally:
            session.close()

        Path("data/config.json").write_text('{"state":"current"}', encoding="utf-8")
        Path("plugins/orphan.py").write_text("STALE = True\n", encoding="utf-8")
        inspection, rollback_name = stage_restore_archive(archive, "test")

        self.assertGreaterEqual(inspection.file_count, 3)
        self.assertTrue(PENDING_RESTORE.is_file())
        self.assertTrue((BACKUP_STORE / rollback_name).is_file())

        restored = apply_pending_restore()

        self.assertEqual(restored, inspection.file_count)
        self.assertEqual(Path("data/config.json").read_text(encoding="utf-8"), '{"state":"backup"}')
        self.assertTrue(Path("plugins/keep.py").is_file())
        self.assertFalse(Path("plugins/orphan.py").exists())
        self.assertFalse(PENDING_RESTORE.exists())
        self.assertTrue((BACKUP_STORE / rollback_name).is_file())
        with closing(sqlite3.connect("sessions/account.session")) as restored_session:
            row = restored_session.execute("SELECT value FROM sample").fetchone()
        self.assertEqual(row, ("saved",))

    def test_invalid_member_is_rejected_before_platform_files_change(self) -> None:
        archive = Path("unsafe.zip")
        manifest = {
            "app": "AWBotNest",
            "version": "test",
            "format": 1,
            "included_roots": list(BACKUP_ROOTS),
        }
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("backup_manifest.json", json.dumps(manifest))
            zf.writestr("data/good.txt", "would be written first")
            zf.writestr("data/../escape.txt", "unsafe")

        with self.assertRaises(BackupError):
            inspect_backup_archive(archive)

        self.assertFalse(Path("data/good.txt").exists())
        self.assertFalse(Path("escape.txt").exists())

    def test_temporary_upload_is_not_included_in_rollback_snapshot(self) -> None:
        Path("data/config.json").write_text("{}", encoding="utf-8")
        Path("data/.restore-upload-test.zip").write_bytes(b"temporary upload")

        archive, _ = create_backup_archive("test", Path("exports"))
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())

        self.assertNotIn("data/.restore-upload-test.zip", names)


if __name__ == "__main__":
    unittest.main()
