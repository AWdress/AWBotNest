"""Run backup regression checks against isolated fixtures; optional read-only source check."""
from __future__ import annotations

import asyncio
from contextlib import closing
import io
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from webui import backup


class BackupChecks(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temp = tempfile.TemporaryDirectory(prefix="awbotnest-backup-test-")
        os.chdir(self.temp.name)
        for root in backup.BACKUP_ROOTS:
            Path(root).mkdir()
        Path("data/config.json").write_text('{"test": true}', encoding="utf-8")
        Path("plugins/example.py").write_text("# test plugin\n", encoding="utf-8")
        with closing(sqlite3.connect("sessions/test.session")) as conn:
            conn.execute("CREATE TABLE sample (value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('fixture')")
            conn.commit()

    def tearDown(self):
        # API imports initialize file logging; close only handlers inside this fixture.
        for name in ("main", "error"):
            log = logging.getLogger(name)
            for handler in list(log.handlers):
                filename = getattr(handler, "baseFilename", None)
                if filename and Path(filename).is_relative_to(self.temp.name):
                    log.removeHandler(handler)
                    handler.close()
        os.chdir(self.previous)
        self.temp.cleanup()

    def export(self):
        return backup.create_backup_archive("test", Path("output"))

    def test_export_and_restore_roundtrip(self):
        archive, _ = self.export()
        inspection = backup.inspect_backup_archive(archive)
        self.assertEqual(inspection.file_count, 3)
        Path("data/config.json").write_text("changed", encoding="utf-8")
        incoming = Path("incoming.zip")
        shutil.copyfile(archive, incoming)
        _, rollback = backup.stage_restore_archive(incoming, "test")
        self.assertEqual(backup.apply_pending_restore(), 3)
        self.assertEqual(Path("data/config.json").read_text(), '{"test": true}')
        self.assertTrue((backup.BACKUP_STORE / rollback).is_file())
        with closing(sqlite3.connect("sessions/test.session")) as conn:
            self.assertEqual(conn.execute("SELECT value FROM sample").fetchone(), ("fixture",))

    def test_locked_database_has_deadline_and_cleans_partial_archive(self):
        locked = sqlite3.connect("sessions/test.session", check_same_thread=False)
        locked.execute("BEGIN EXCLUSIVE")
        # A safety release makes the old, unbounded implementation fail rather than hang tests.
        timer = threading.Timer(2, locked.rollback)
        timer.start()
        started = time.monotonic()
        try:
            with patch.object(backup, "SQLITE_BACKUP_TIMEOUT", 0.2, create=True):
                with self.assertRaisesRegex(backup.BackupError, "超时"):
                    self.export()
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertFalse(list(Path("output").glob("*.zip")))
        finally:
            timer.cancel()
            timer.join()
            locked.rollback()
            locked.close()

    def test_runtime_files_excluded(self):
        backup.BACKUP_STORE.mkdir()
        (backup.BACKUP_STORE / "old.zip").write_bytes(b"not included")
        Path("sessions/test.session-wal").write_bytes(b"not included")
        # Remove the fake WAL before opening the database for a snapshot.
        self.assertTrue(backup._is_excluded(Path("sessions/test.session-wal")))
        Path("sessions/test.session-wal").unlink()
        archive, _ = self.export()
        with zipfile.ZipFile(archive) as zf:
            self.assertNotIn("data/backups/old.zip", zf.namelist())

    def test_progress_logs(self):
        with self.assertLogs("main", level="INFO") as captured:
            self.export()
        messages = "\n".join(captured.output)
        self.assertIn("开始", messages)
        self.assertIn("SQLite", messages)
        self.assertIn("完成", messages)

    def test_overall_deadline(self):
        with patch.object(backup, "BACKUP_TIMEOUT", 0):
            with self.assertRaisesRegex(backup.BackupError, "超时"):
                self.export()
        self.assertFalse(list(Path("output").glob("*.zip")))

    def test_http_download_and_response_cleanup(self):
        import httpx
        from webui import api as routes

        original = backup.create_backup_archive
        routes.app.dependency_overrides[routes._auth_pwc] = lambda: {"username": "test"}

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=routes.app), base_url="http://test") as client:
                response = await client.post("/api/system/backup")
                self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else "")
                self.assertEqual(response.headers["content-type"], "application/zip")
                self.assertIn("attachment", response.headers["content-disposition"])
                with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                    self.assertIsNone(zf.testzip())
                    self.assertEqual(zf.read("data/config.json"), b'{"test": true}')
                self.assertFalse(list(Path("output").glob("*.zip")), "Download must remove temporary archive")
                Path("data/config.json").write_text("changed", encoding="utf-8")
                restored = await client.post(
                    "/api/system/restore",
                    files={"file": ("backup.zip", response.content, "application/zip")},
                )
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertTrue(restored.json()["restore_pending"])
                self.assertEqual(backup.apply_pending_restore(), 3)
                self.assertEqual(Path("data/config.json").read_text(), '{"test": true}')

        try:
            with patch.object(routes, "create_backup_archive", lambda version: original(version, Path("output"))):
                asyncio.run(check())
        finally:
            routes.app.dependency_overrides.clear()

    def test_http_remains_responsive_and_returns_lock_error(self):
        import httpx
        from webui import api as routes

        locked = sqlite3.connect("sessions/test.session")
        locked.execute("BEGIN EXCLUSIVE")
        original = backup.create_backup_archive
        entered = threading.Event()

        def generate(version):
            entered.set()
            return original(version, Path("output"))

        routes.app.dependency_overrides[routes._auth_pwc] = lambda: {"username": "test"}

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=routes.app), base_url="http://test") as client:
                task = asyncio.create_task(client.post("/api/system/backup"))
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                other = await asyncio.wait_for(client.get("/openapi.json"), 0.5)
                self.assertEqual(other.status_code, 200)
                self.assertFalse(task.done(), "Other requests should complete while backup is waiting")
                response = await asyncio.wait_for(task, 3)
                self.assertEqual(response.status_code, 500)
                self.assertIn("超时", response.json()["detail"])
                self.assertIn("sessions/test.session", response.json()["detail"])
                self.assertFalse(list(Path("output").glob("*.zip")))

        try:
            with patch.object(routes, "create_backup_archive", generate), patch.object(backup, "SQLITE_BACKUP_TIMEOUT", 1):
                asyncio.run(check())
        finally:
            locked.rollback()
            locked.close()
            routes.app.dependency_overrides.clear()


def real_data_check():
    # Only reads live sources. Archive output is isolated, deleted after validation.
    with tempfile.TemporaryDirectory(prefix="awbotnest-backup-real-") as output:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker", output],
            cwd=REPO, text=True, timeout=90,
        )
        return result.returncode


def serve_fixture():
    """Run the real web UI/API against disposable data, without starting Telegram."""
    with tempfile.TemporaryDirectory(prefix="awbotnest-backup-browser-") as fixture:
        previous = Path.cwd()
        os.chdir(fixture)
        try:
            for root in backup.BACKUP_ROOTS:
                Path(root).mkdir()
            Path("data/fixture.bin").write_bytes(os.urandom(2 * 1024 * 1024))
            with closing(sqlite3.connect("sessions/fixture.session")) as conn:
                conn.execute("CREATE TABLE fixture (id INTEGER)")
                conn.commit()
            from webui import api as routes
            from webui import auth
            import uvicorn
            auth.setup_credentials("backup-test", "Backup-test-2026!")
            # All paths are fixture-relative; no platform lifecycle or Telegram is started.
            uvicorn.run(routes.app, host="127.0.0.1", port=8769, log_level="info")
        finally:
            logging.shutdown()
            os.chdir(previous)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--lock":
        with closing(sqlite3.connect(sys.argv[2])) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            print("Fixture database locked for 60 seconds", flush=True)
            time.sleep(60)
            conn.rollback()
    elif len(sys.argv) > 1 and sys.argv[1] == "--validate":
        inspection = backup.inspect_backup_archive(Path(sys.argv[2]))
        print(f"VALIDATED files={inspection.file_count} bytes={inspection.expanded_bytes}")
    elif len(sys.argv) > 1 and sys.argv[1] == "--worker":
        logging.basicConfig(level=logging.INFO)
        started = time.monotonic()
        archive, _ = backup.create_backup_archive("test", Path(sys.argv[2]))
        print(f"EXPORT seconds={time.monotonic() - started:.2f} bytes={archive.stat().st_size}", flush=True)
        inspection = backup.inspect_backup_archive(archive)
        print(f"VALIDATED files={inspection.file_count} bytes={inspection.expanded_bytes}", flush=True)
    elif "--real-data" in sys.argv:
        raise SystemExit(real_data_check())
    elif "--serve" in sys.argv:
        serve_fixture()
    else:
        unittest.main(verbosity=2)
