from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from awbotnest.api import create_app
from awbotnest.backup import BackupManager
from awbotnest.config import Settings, validate_config_format
from awbotnest.logs import MemoryLogHandler, RedactingFormatter, install_secret_filters
from awbotnest.market import PluginMarket, OFFICIAL_REPO
from awbotnest.plugin_config import mask_config, restore_config_secrets, reveal_secret
from awbotnest.plugins import PluginRuntime
from awbotnest.routing import PluginRoutes
from awbotnest.scheduler import PluginScheduler


def archive(config):
    config = dict(config)
    plugin_config = config.pop("plugin_config", {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("manifest.json", json.dumps({"format": "awbotnest-config", "version": 1}))
        zipped.writestr("config/system.json", json.dumps(config))
        zipped.writestr("config/plugins.json", json.dumps(plugin_config))
    return buffer.getvalue()


class RestoreIntegrityTests(unittest.TestCase):
    def test_malformed_fields_are_rejected_before_staging(self):
        invalid = {
            "api_id": "not-an-integer", "web_port": 99999, "bots": 1,
            "user_sessions": 1, "plugin_repo_interval": False,
            "notification_channels": {}, "plugin_accounts": {"p": "one"},
            "plugin_config": {"p": []}, "admin_password_hash": "bad",
            "log_cleaner": {"hour": "later"}, "ai_settings": {"providers": {}},
            "cookie_settings": {"remote_interval_minutes": "later"},
        }
        for field, value in invalid.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                BackupManager._read_content(archive({field: value}))
        validate_config_format({"api_id": "123", "web_port": 18001, "plugin_config": {}})
        for nested in ({"models": 1}, {"capabilities": []}, {"plugin_permissions": {"p": "wrong"}},
                       {"models": [{"capabilities": 1}]}):
            with self.subTest(ai=nested), self.assertRaises(ValueError):
                BackupManager._read_content(archive({"ai_settings": nested}))

    def test_unconfirmed_restore_rolls_back_after_interrupted_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            original = {"web_port": 18001, "admin_token": "previous-test-token"}
            (data / "config.json").write_text(json.dumps(original), encoding="utf-8")
            with patch.multiple("awbotnest.backup", DATA_DIR=data, BACKUP_DIR=data / "backups",
                                PENDING_RESTORE=data / ".restore-pending.zip"):
                BackupManager.stage(archive({"web_port": 18002}))
                self.assertTrue(BackupManager.apply_pending())
                self.assertEqual(json.loads((data / "config.json").read_text())["web_port"], 18002)
                self.assertFalse(BackupManager.apply_pending())
                self.assertEqual(json.loads((data / "config.json").read_text()), original)
                self.assertTrue(list((data / "backups").glob("failed-config-restore-*.json")))
                BackupManager.stage(archive({"web_port": 18003}))
                BackupManager.apply_pending()
                BackupManager.commit_restore()
                self.assertFalse(BackupManager.rollback_restore())
                self.assertEqual(json.loads((data / "config.json").read_text())["web_port"], 18003)


class UpdateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_install_skips_unsafe_auto_update(self):
        with tempfile.TemporaryDirectory() as folder, patch("awbotnest.market.PLUGINS_DIR", Path(folder) / "plugins"):
            market = PluginMarket(Settings())
            untrusted = {"id": "custom", "repo": "unknown/AWBotNest-Plugins", "path": "custom",
                         "version": "9", "installed": True, "update_available": True}
            market.refresh = AsyncMock(return_value={"plugins": [untrusted]})
            market.install = AsyncMock()
            runtime = SimpleNamespace(loaded={}, display_name=lambda value: value)
            await market._poll_updates(runtime)
            market.install.assert_not_awaited()
            trusted = {**untrusted, "repo": "trusted/plugins"}
            market.confirm_source(trusted)
            restored = PluginMarket(Settings())
            self.assertTrue(restored.source_matches(trusted))
            self.assertFalse(restored.source_matches(untrusted))
            self.assertFalse(restored.source_matches({**trusted, "path": "another/path"}))
            market.forget_install("custom")
            self.assertFalse(PluginMarket(Settings()).source_matches(trusted))

    async def test_bound_repository_wins_over_same_id_from_other_repositories(self):
        with tempfile.TemporaryDirectory() as folder, patch("awbotnest.market.PLUGINS_DIR", Path(folder) / "plugins"):
            market = PluginMarket(Settings(plugin_repos=["unknown/plugins", "trusted/plugins"]))
            market.confirm_source({"id": "custom", "repo": "trusted/plugins", "path": "custom"})
            async def listing(repo):
                return {"plugins": [{"id": "custom", "repo": repo, "path": "custom", "version": "9"}]}
            market.list_repo = listing
            market._install_counts = AsyncMock(return_value={})
            with patch.object(PluginMarket, "_installed_version_for_source", return_value="1"):
                result = await market.list_all()
            self.assertEqual(result["plugins"][0]["repo"], "trusted/plugins")
            self.assertTrue(result["plugins"][0]["auto_update_allowed"])


SCHEMA = {
    "accounts": {"type": "list", "fields": {"name": {"type": "string"}, "password": {"type": "password"}}},
    "tokens": {"type": "list", "secret": True},
    "groups": {"type": "array", "items": {"type": "object", "properties": {
        "api/key": {"type": "password"}, "tilde~key": {"secret": True}}}},
}
VALUES = {"accounts": [{"name": "first", "password": "first-test-secret"},
                       {"name": "second", "password": "second-test-secret"}],
          "tokens": [{"key": "list-test-secret"}],
          "groups": [{"api/key": "escaped-test-secret", "tilde~key": "tilde-test-secret"}]}


class RecursiveSecretsTests(unittest.TestCase):
    def test_recursive_masks_round_trip_without_mutating_original(self):
        masked = mask_config(SCHEMA, VALUES)
        self.assertEqual(masked["accounts"][1]["password"], "********")
        self.assertEqual(masked["tokens"], "********")
        self.assertEqual(masked["groups"][0]["api/key"], "********")
        self.assertEqual(restore_config_secrets(SCHEMA, masked, VALUES), VALUES)
        self.assertEqual(VALUES["accounts"][0]["password"], "first-test-secret")
        self.assertEqual(reveal_secret(SCHEMA, VALUES, "/accounts/1/password"), "second-test-secret")
        self.assertEqual(reveal_secret(SCHEMA, VALUES, "/groups/0/api~1key"), "escaped-test-secret")
        self.assertEqual(reveal_secret(SCHEMA, VALUES, "/groups/0/tilde~0key"), "tilde-test-secret")
        self.assertEqual(reveal_secret(SCHEMA, VALUES, "tokens"), VALUES["tokens"])
        with self.assertRaises(ValueError):
            reveal_secret(SCHEMA, VALUES, "/accounts/1/name")
        with self.assertRaises(KeyError):
            reveal_secret(SCHEMA, VALUES, "/accounts/-1/password")
        with self.assertRaises(ValueError):
            reveal_secret(SCHEMA, VALUES, "/groups/0/api~2key")

    def test_masked_rows_follow_unique_identity_after_delete_and_reorder(self):
        masked = mask_config(SCHEMA, VALUES)
        deleted = {**masked, "accounts": masked["accounts"][1:]}
        saved = restore_config_secrets(SCHEMA, deleted, VALUES)
        self.assertEqual(saved["accounts"][0]["password"], "second-test-secret")
        reordered = {**masked, "accounts": list(reversed(masked["accounts"]))}
        saved = restore_config_secrets(SCHEMA, reordered, VALUES)
        self.assertEqual(saved["accounts"][0]["password"], "second-test-secret")
        ambiguous_values = {"accounts": [{"name": "same", "password": "one"}, {"name": "same", "password": "two"}]}
        ambiguous = mask_config(SCHEMA, ambiguous_values)
        with self.assertRaises(ValueError):
            restore_config_secrets(SCHEMA, {"accounts": ambiguous["accounts"][1:]}, ambiguous_values)
        reordered["accounts"][0] = {"name": "edited-second", "password": "********"}
        with self.assertRaises(ValueError):
            restore_config_secrets(SCHEMA, reordered, VALUES)


class SecurityApiTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        settings = Settings(admin_token="security-test-token", api_key="security-test-api",
                            plugin_config={"secret": json.loads(json.dumps(VALUES))})
        plugin = SimpleNamespace(id="secret", render_mode="vue", config_schema=SCHEMA)
        runtime = SimpleNamespace(scan=lambda: [plugin], has_frontend=lambda value: False,
                                  loaded={}, validate_config=PluginRuntime.validate_config,
                                  frontend_dist_dir=lambda value: Path(__file__).parent / ".missing-test-frontend")
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        return create_app(settings, SimpleNamespace(), runtime, scheduler, PluginRoutes(), market=SimpleNamespace()), settings

    async def test_nested_secrets_are_masked_in_admin_and_open_api_responses(self):
        app, settings = self.app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            for url, header, key in (("/api/plugins/secret/config", {"Authorization": "Bearer security-test-token"}, "values"),
                                     ("/api/v1/plugins/secret/config", {"X-API-Key": "security-test-api"}, "config")):
                response = await client.get(url, headers=header)
                self.assertEqual(response.status_code, 200, response.text)
                masked = response.json()[key]
                self.assertEqual(masked["accounts"][0]["password"], "********")
                with patch("awbotnest.api.plugins.save_settings"), patch("awbotnest.open_api.save_settings"):
                    saved = await client.put(url, headers=header, json={key: masked})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(settings.plugin_config["secret"], VALUES)
            reveal = await client.post("/api/plugins/secret/config/reveal",
                                       headers={"Authorization": "Bearer security-test-token"},
                                       json={"field": "/accounts/1/password"})
            self.assertEqual(reveal.json()["value"], "second-test-secret")
            self.assertEqual(reveal.headers["cache-control"], "no-store")

    async def test_password_and_token_rotation_refresh_resource_cookie(self):
        app, settings = self.app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            await client.post("/api/auth/login", json={"username": "admin", "password": "security-test-token"})
            with patch("awbotnest.api.system.save_settings"):
                invalid = await client.post("/api/auth/change_credentials", headers={"Authorization": "Bearer security-test-token"},
                                            json={"old_password": "security-test-token", "new_password": "x"})
                self.assertEqual(invalid.status_code, 400)
                changed = await client.post("/api/auth/change_credentials", headers={"Authorization": "Bearer security-test-token"},
                                            json={"old_password": "security-test-token", "new_password": "new-test-password"})
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertEqual(client.cookies.get("awbotnest_resource"), settings.admin_token)
                resource = await client.get("/api/plugins/secret/fe/remoteEntry.js")
                self.assertEqual(resource.status_code, 404)  # Auth succeeded; no real file is required.
                rotated = await client.post("/api/auth/rotate_token", headers={"Authorization": "Bearer " + settings.admin_token})
                self.assertEqual(rotated.status_code, 200)
                self.assertEqual(client.cookies.get("awbotnest_resource"), settings.admin_token)

    async def test_upload_waits_for_in_progress_market_transaction(self):
        app, settings = self.app()
        market = SimpleNamespace(install_lock=asyncio.Lock())
        # The router closes over its supplied market, so compose a fresh app.
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        app = create_app(settings, SimpleNamespace(), SimpleNamespace(), scheduler, PluginRoutes(), market=market)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            for url, headers in (("/api/plugins/upload", {"Authorization": "Bearer security-test-token"}),
                                 ("/api/v1/plugins/upload", {"X-API-Key": "security-test-api"})):
                await market.install_lock.acquire()
                task = asyncio.create_task(client.post(url, headers=headers, files={"file": ("bad.txt", b"x")}))
                try:
                    await asyncio.sleep(0.02)
                    self.assertFalse(task.done())
                finally:
                    market.install_lock.release()
                response = await task
                self.assertEqual(response.status_code, 400)

    async def test_successful_upload_clears_previous_auto_update_source(self):
        settings = Settings(admin_token="upload-test-token", api_key="upload-test-api")
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        with tempfile.TemporaryDirectory() as folder:
            plugins = Path(folder) / "plugins"
            plugins.mkdir()
            forgotten = []
            market = SimpleNamespace(install_lock=asyncio.Lock(), forget_source=forgotten.append)
            plugin = SimpleNamespace(id="uploaded", error="", enabled=False,
                                     to_dict=lambda: {"id": "uploaded"})
            runtime = SimpleNamespace(plugins_dir=plugins, scan=lambda: [plugin],
                                      invalidate_scan_cache=lambda: None, display_name=lambda name: name)
            app = create_app(settings, SimpleNamespace(), runtime, scheduler, PluginRoutes(), market=market)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                for url, headers in (("/api/plugins/upload", {"Authorization": "Bearer upload-test-token"}),
                                     ("/api/v1/plugins/upload", {"X-API-Key": "upload-test-api"})):
                    with patch("awbotnest.api.plugins.PLUGINS_DIR", plugins):
                        response = await client.post(url, headers=headers,
                                                     files={"file": ("uploaded.py", b"__plugin__ = {'id': 'uploaded'}")})
                    self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(forgotten, ["uploaded", "uploaded"])

    async def test_upload_source_persistence_failure_restores_previous_code_and_binding(self):
        settings = Settings(admin_token="upload-test-token", api_key="upload-test-api")
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        with tempfile.TemporaryDirectory() as folder:
            plugins = Path(folder) / "plugins"
            plugins.mkdir()
            original = b"__plugin__ = {'id': 'uploaded', 'version': '1'}\n"
            changed = b"__plugin__ = {'id': 'uploaded', 'version': '2'}\nCUSTOM = True\n"
            target = plugins / "uploaded.py"
            target.write_bytes(original)
            with patch("awbotnest.market.PLUGINS_DIR", plugins):
                market = PluginMarket(settings)
                source = {"id": "uploaded", "repo": OFFICIAL_REPO, "path": "uploaded.py"}
                market.confirm_source(source)
                source_state = market._sources_path.read_bytes()
                plugin = SimpleNamespace(id="uploaded", error="", enabled=False,
                                         to_dict=lambda: {"id": "uploaded"})
                runtime = SimpleNamespace(plugins_dir=plugins, scan=lambda: [plugin],
                                          invalidate_scan_cache=lambda: None, display_name=lambda name: name)
                app = create_app(settings, SimpleNamespace(), runtime, scheduler, PluginRoutes(), market=market)
                transport = httpx.ASGITransport(app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    for url, headers in (("/api/plugins/upload", {"Authorization": "Bearer upload-test-token"}),
                                         ("/api/v1/plugins/upload", {"X-API-Key": "upload-test-api"})):
                        with self.subTest(url=url), patch("awbotnest.api.plugins.PLUGINS_DIR", plugins), \
                                patch.object(market, "_save_sources", side_effect=OSError("fixture disk full")):
                            response = await client.post(url, headers=headers,
                                                         files={"file": ("uploaded.py", changed)})
                        self.assertGreaterEqual(response.status_code, 400)
                        self.assertEqual(target.read_bytes(), original)
                        self.assertTrue(market.source_matches(source))
                        self.assertNotIn("uploaded", market._local_only)
                        self.assertEqual(market._sources_path.read_bytes(), source_state)
                        self.assertFalse((plugins / ".uploaded.py.upload").exists())

    async def test_store_source_persistence_failure_restores_code_before_backup_is_removed(self):
        settings = Settings(admin_token="store-test-token")
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        with tempfile.TemporaryDirectory() as folder:
            plugins = Path(folder) / "plugins"
            plugins.mkdir()
            original = b"__plugin__ = {'id': 'sample', 'name': 'Fixture', 'version': '1'}\n"
            changed = b"__plugin__ = {'id': 'sample', 'name': 'Fixture', 'version': '2'}\nNEW_CODE = True\n"
            target = plugins / "sample.py"
            target.write_bytes(original)
            with patch("awbotnest.market.PLUGINS_DIR", plugins):
                market = PluginMarket(settings)
                previous = {"id": "sample", "repo": "previous/plugins", "path": "sample.py"}
                market.confirm_source(previous)
                source_state = market._sources_path.read_bytes()
                market._download_file = AsyncMock(return_value=changed)
                plugin = SimpleNamespace(id="sample", name="Fixture", error="", enabled=False,
                                         to_dict=lambda: {"id": "sample"})
                runtime = SimpleNamespace(loaded={}, scan=lambda: [plugin],
                                          invalidate_scan_cache=lambda: None, display_name=lambda name: name)
                app = create_app(settings, SimpleNamespace(), runtime, scheduler, PluginRoutes(), market=market)
                transport = httpx.ASGITransport(app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    with patch.object(market, "_save_sources", side_effect=OSError("fixture disk full")):
                        response = await client.post("/api/plugins/store/install",
                                                     headers={"Authorization": "Bearer store-test-token"},
                                                     json={"plugin": {"id": "sample", "repo": "new/plugins",
                                                                      "path": "sample.py", "version": "2"}})
                self.assertGreaterEqual(response.status_code, 400)
                self.assertEqual(target.read_bytes(), original)
                self.assertTrue(market.source_matches(previous))
                self.assertEqual(market._sources_path.read_bytes(), source_state)
                self.assertNotIn("sample", market._pending_installs)
                self.assertFalse((plugins / ".sample.py.backup").exists())

    async def test_store_heat_persistence_failure_keeps_successful_update(self):
        settings = Settings(admin_token="store-test-token")
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        with tempfile.TemporaryDirectory() as folder:
            plugins = Path(folder) / "plugins"
            plugins.mkdir()
            original = b"__plugin__ = {'id': 'sample', 'name': 'Fixture', 'version': '1'}\n"
            changed = b"__plugin__ = {'id': 'sample', 'name': 'Fixture', 'version': '2'}\n"
            target = plugins / "sample.py"
            target.write_bytes(original)
            with patch("awbotnest.market.PLUGINS_DIR", plugins):
                market = PluginMarket(settings)
                source = {"id": "sample", "repo": "trusted/plugins", "path": "sample.py"}
                market.confirm_source(source)
                market._download_file = AsyncMock(return_value=changed)
                plugin = SimpleNamespace(id="sample", name="Fixture", error="", enabled=False,
                                         to_dict=lambda: {"id": "sample"})
                runtime = SimpleNamespace(loaded={}, scan=lambda: [plugin],
                                          invalidate_scan_cache=lambda: None, display_name=lambda name: name)
                app = create_app(settings, SimpleNamespace(), runtime, scheduler, PluginRoutes(), market=market)
                transport = httpx.ASGITransport(app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    with patch.object(market, "_save_heat_state", side_effect=OSError("fixture heat disk full")):
                        response = await client.post("/api/plugins/store/install",
                                                     headers={"Authorization": "Bearer store-test-token"},
                                                     json={"plugin": {**source, "version": "2"}})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(target.read_bytes(), changed)
                self.assertTrue(market.source_matches(source))
                self.assertNotIn("sample", market._pending_installs)
                self.assertEqual(market._download_file.await_count, 1)


class LoggingSecretsTests(unittest.TestCase):
    def test_console_file_and_exception_formats_share_redaction(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.Logger("test-redaction")
        logger.addHandler(handler)
        install_secret_filters(logger)
        install_secret_filters(logger)
        self.assertIsInstance(handler.formatter, RedactingFormatter)
        try:
            raise RuntimeError('password="test exception password" CLOAKBROWSER_LICENSE_KEY=cb_test_secret')
        except RuntimeError:
            logger.exception("token=test-log-token https://api.telegram.org/bot12345:test-bot-secret/sendMessage")
        text = stream.getvalue()
        for secret in ("test exception password", "cb_test_secret", "test-log-token", "test-bot-secret"):
            self.assertNotIn(secret, text)
        record = logging.LogRecord("plugin", logging.WARNING, "", 0,
                                   "remote_password=test-remote-password Cookie='sid=test-cookie; other=value'", (), None)
        memory = MemoryLogHandler()
        with patch.object(memory, "load_persisted"), patch.object(memory, "_persist_locked"):
            memory.handle(record)
        self.assertNotIn("test-remote-password", memory.records[0]["message"])
        self.assertNotIn("test-cookie", memory.records[0]["message"])


if __name__ == "__main__":
    unittest.main()
