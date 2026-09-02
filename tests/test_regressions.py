"""Isolated regressions: never read or write the running platform's data."""
import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from awbotnest import backup, config, cookiecloud, market, migrate
from awbotnest.auth import token_matches
from awbotnest.context import PluginContext
from awbotnest.plugins import PluginRuntime
from awbotnest.services import AIService


def archive_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in entries.items():
            item = zipfile.ZipInfo(name)
            item.filename = name  # Keep malformed separators on Windows for validation tests.
            archive.writestr(item, value)
    return stream.getvalue()


class IsolatedFiles(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data = self.root / "data"
        self.data.mkdir()
        self.plugins = self.root / "plugins"
        self.sessions = self.root / "sessions"
        for module in (config, backup, market, migrate):
            for key, value in {
                "APP_ROOT": self.root, "DATA_DIR": self.data,
                "PLUGINS_DIR": self.plugins, "SESSIONS_DIR": self.sessions,
                "CONFIG_FILE": self.data / "config.json", "BACKUP_DIR": self.data / "backups",
                "PENDING_RESTORE": self.data / ".restore-pending.zip",
                "PENDING_MIGRATION": self.data / ".v1-migration-pending.zip",
            }.items():
                if hasattr(module, key):
                    self.stack.enter_context(patch.object(module, key, value))

    def test_empty_enabled_plugins_survives_restart(self):
        config.save_settings(config.Settings(enabled_plugins=[]))
        self.assertEqual(config.load_settings().enabled_plugins, [])

    def test_corrupt_config_is_not_overwritten(self):
        config.CONFIG_FILE.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            config.load_settings()
        self.assertEqual(config.CONFIG_FILE.read_text(), "{broken")

    def test_nonpersisting_load_does_not_write_legacy_config(self):
        config.CONFIG_FILE.write_text('{"API_ID": 123}', encoding="utf-8")
        config.load_settings(persist_defaults=False)
        self.assertEqual(json.loads(config.CONFIG_FILE.read_text()), {"API_ID": 123})

    def test_empty_migration_does_not_change_current_config(self):
        config.save_settings(config.Settings(enabled_plugins=["existing"]))
        original = config.CONFIG_FILE.read_bytes()
        with self.assertRaises(ValueError):
            migrate.migrate(self.root / "missing")
        self.assertEqual(config.CONFIG_FILE.read_bytes(), original)

    def test_manual_migration_only_stages_until_restart(self):
        config.save_settings(config.Settings(api_id=456, enabled_plugins=[]))
        original = config.CONFIG_FILE.read_bytes()
        payload = archive_bytes({"data/config.json": '{"API_ID": 123, "API_HASH": "example"}'})
        result = migrate.stage_migration(payload)
        self.assertTrue(result["restart_required"])
        self.assertEqual(config.CONFIG_FILE.read_bytes(), original)
        self.assertTrue(migrate.apply_pending_migration())
        self.assertEqual(config.load_settings().api_id, 123)
        self.assertFalse(migrate.apply_pending_migration())
        self.assertTrue(backup.BackupManager.list())

    def test_sqlite_backup_contains_committed_wal_data(self):
        source = sqlite3.connect(self.data / "test.sqlite")
        self.addCleanup(source.close)
        source.execute("pragma journal_mode=WAL")
        source.execute("create table sample (value text)")
        source.execute("insert into sample values ('committed')")
        source.commit()
        archive = backup.BackupManager.create()
        output = self.root / "check"
        with zipfile.ZipFile(archive) as package:
            self.assertNotIn("data/test.sqlite-wal", package.namelist())
            package.extractall(output)
        with closing(sqlite3.connect(output / "data/test.sqlite")) as restored:
            self.assertEqual(restored.execute("select value from sample").fetchone(), ("committed",))

    def test_restore_preserves_mount_roots_and_old_data(self):
        config.CONFIG_FILE.write_text("old")
        backup.BackupManager.stage(archive_bytes({"data/config.json": "new", "plugins/demo.py": "pass"}))
        original_replace = Path.rename

        def reject_mount_rename(path, target):
            self.assertNotIn(path, (self.data, self.plugins, self.sessions))
            return original_replace(path, target)

        with patch.object(Path, "rename", reject_mount_rename):
            self.assertTrue(backup.BackupManager.apply_pending())
        self.assertEqual(config.CONFIG_FILE.read_text(), "new")
        originals = list((self.data / "backups").glob("rollback-*/data/config.json"))
        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0].read_text(), "old")

    def test_restore_rolls_back_failed_move(self):
        config.CONFIG_FILE.write_text("old")
        backup.BackupManager.stage(archive_bytes({"data/config.json": "new"}))
        move = backup.shutil.move

        def fail_install(src, dst):
            if ".restore-" in str(src) and str(dst) == str(config.CONFIG_FILE):
                raise OSError("injected failure")
            return move(src, dst)

        with patch.object(backup.shutil, "move", fail_install):
            with self.assertRaises(OSError):
                backup.BackupManager.apply_pending()
        self.assertEqual(config.CONFIG_FILE.read_text(), "old")
        self.assertTrue(backup.PENDING_RESTORE.exists())

    def test_unsafe_backup_paths_rejected(self):
        for name in ("data/../../outside", "data/C:secret", "data\\file", "data"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                backup.BackupManager.stage(archive_bytes({name: "bad"}))

    def test_download_failure_preserves_installed_plugin(self):
        self.plugins.mkdir()
        existing = self.plugins / "demo.py"
        existing.write_text("old plugin")
        client = market.PluginMarket(config.Settings())
        client._download_file = AsyncMock(side_effect=OSError("offline"))
        with self.assertRaises(OSError):
            asyncio.run(client.install({"id": "demo", "repo": "owner/repo"}))
        client.finish("demo", False)
        self.assertEqual(existing.read_text(), "old plugin")

    def test_install_rollback_is_idempotent(self):
        self.plugins.mkdir()
        existing = self.plugins / "demo.py"
        existing.write_text("old plugin")
        client = market.PluginMarket(config.Settings())
        client._download_file = AsyncMock(return_value=b"__plugin__ = {'id': 'demo'}")
        asyncio.run(client.install({"id": "demo", "repo": "owner/repo"}))
        client.finish("demo", False)
        client.finish("demo", False)
        self.assertEqual(existing.read_text(), "old plugin")


class LogicTests(unittest.TestCase):
    def test_nonascii_token_rejected_without_server_error(self):
        self.assertFalse(token_matches("错误令牌", "secret"))
        self.assertFalse(token_matches("", ""))
        self.assertTrue(token_matches("secret", "secret"))

    def test_cookie_error_response_is_not_empty_cookie_set(self):
        with self.assertRaises(cookiecloud.CookieCloudError):
            cookiecloud.normalize_cookie_data({"error": "wrong password"})
        self.assertEqual(cookiecloud.normalize_cookie_data({"cookie_data": {}}), {})
        with self.assertRaises(cookiecloud.CookieCloudError):
            cookiecloud.decrypt_payload("%%%", "uuid", "password")

    def test_required_config_allows_false_and_zero_but_not_empty(self):
        for value in (False, 0):
            PluginRuntime.validate_config({"key": {"required": True}}, {"key": value})
        for values in ({}, {"key": None}, {"key": ""}):
            with self.assertRaises(ValueError):
                PluginRuntime.validate_config({"key": {"required": True}}, values)

    def test_ai_model_alias_resolves_to_configured_provider(self):
        settings = config.Settings(ai_settings={"providers": [{"id": "p", "api_key": "key", "base_url": "https://ai"}],
            "models": [{"id": "internal", "alias": "public-name", "model": "actual", "provider_id": "p", "capabilities": ["text"]}]})
        base, key, model = AIService(settings, object())._resolve("text", "public-name")
        self.assertEqual((base, key, model), ("https://ai", "key", "actual"))

    def test_plugin_users_obey_selected_sessions(self):
        from types import SimpleNamespace
        one, two = [SimpleNamespace(is_connected=lambda: True) for _ in range(2)]
        context = object.__new__(PluginContext)
        context.plugin_id = "demo"
        context.settings = config.Settings(plugin_accounts={"demo": ["one"]})
        context.accounts = SimpleNamespace(users={"one": one, "two": two})
        self.assertEqual(context.users, [one])
        context.settings.plugin_accounts["demo"] = []
        self.assertEqual(len(context.users), 2)


if __name__ == "__main__":
    unittest.main()
