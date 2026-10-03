"""Old plugin sources are proved in the background without reinstalling code."""
from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import AsyncMock, patch

import httpx

from awbotnest.config import Settings
from awbotnest.market import OFFICIAL_REPO, PluginMarket


PLUGIN_ID = "source_sample"
SOURCE_PATH = f"plugins_v2/{PLUGIN_ID}"


def entry(version="1.0", *, repo="", extra=""):
    metadata = {"id": PLUGIN_ID, "name": "Source fixture", "version": version, "scope": "standalone"}
    if repo:
        metadata["repo"] = repo
    return ("__plugin__ = " + repr(metadata) + "\n\ndef setup(ctx):\n    return None\n" + extra).encode()


def git_blob(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


class GitHubFixture:
    def __init__(self):
        self.snapshots = {}
        self.history = {}
        self.urls = []
        self.offline = False
        self.truncated = set()
        self.symlink_blobs = set()
        self.before_tree = None

    def add(self, repo, files, *, ref="main", history=False):
        self.snapshots[repo, ref] = dict(files)
        if history:
            self.history.setdefault(repo, []).append(ref)

    async def __call__(self, url):
        self.urls.append(url)
        request = httpx.Request("GET", url)
        if self.offline:
            raise httpx.ConnectError("fixture offline", request=request)
        parsed = urlsplit(url)
        parts = unquote(parsed.path).strip("/").split("/")
        if parsed.hostname != "api.github.com" or len(parts) < 3 or parts[0] != "repos":
            raise AssertionError(f"Unexpected remote request: {url}")
        repo = "/".join(parts[1:3])
        route = parts[3:]
        if route[:2] == ["git", "trees"]:
            ref = "/".join(route[2:])
            files = self.snapshots.get((repo, ref))
            if files is None:
                return httpx.Response(404, json={"message": "not found"}, request=request)
            if self.before_tree:
                await self.before_tree(repo, ref)
            tree = [{"path": path, "type": "blob", "mode": "120000" if (repo, path) in self.symlink_blobs else "100644",
                     "sha": git_blob(content), "size": len(content)} for path, content in files.items()]
            return httpx.Response(200, json={"tree": tree, "truncated": repo in self.truncated,
                                           "sha": ref}, request=request)
        if route == ["commits"]:
            query = parse_qs(parsed.query)
            if "path" not in query:
                raise AssertionError("History search must be scoped to the plugin source path")
            return httpx.Response(200, json=[{"sha": ref} for ref in self.history.get(repo, [])], request=request)
        raise AssertionError(f"Unexpected remote request: {url}")


class PluginSourceMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.patch = patch("awbotnest.market.PLUGINS_DIR", self.plugins)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.remote = GitHubFixture()
        self.market = PluginMarket(Settings())
        self.market._github = self.remote
        self.market.install = AsyncMock()
        self.market.record_install = AsyncMock()
        self.heat = self.root / "data" / "plugin_heat_state.json"
        self.heat.parent.mkdir(exist_ok=True)
        self.heat.write_text('{"installs":{"source_sample":7}}', encoding="utf-8")

    def candidate(self, repo=OFFICIAL_REPO, *, path=SOURCE_PATH, version="1.0"):
        return {"id": PLUGIN_ID, "name": "Source fixture", "repo": repo, "branch": "main",
                "path": path, "version": version, "scope": "standalone", "installed": True}

    def candidates(self, *plugins, complete=True):
        self.market.settings.plugin_repos = list(dict.fromkeys(
            [OFFICIAL_REPO, *(plugin["repo"] for plugin in plugins)]))
        self.market._source_candidates = list(plugins)
        self.market._source_candidates_complete = complete

    def write_package(self, files=None):
        files = files or {"__init__.py": entry(), "helpers.py": b"VALUE = 1\n"}
        for relative, content in files.items():
            target = self.plugins / PLUGIN_ID / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return {f"{SOURCE_PATH}/{path}": content for path, content in files.items()}

    def local_files(self):
        return {path.relative_to(self.plugins).as_posix(): path.read_bytes()
                for path in self.plugins.rglob("*") if path.is_file()}

    def prepare_single_file_update(self):
        target = self.plugins / f"{PLUGIN_ID}.py"
        target.write_bytes(entry("1.0"))
        plugin = self.candidate(path=target.name)
        self.market.confirm_source(plugin)
        del self.market.install  # Exercise the actual atomic file transaction.
        self.market._download_file = AsyncMock(return_value=entry("2.0"))
        runtime = SimpleNamespace(loaded={}, display_name=lambda value: value,
                                  invalidate_scan_cache=lambda: None)
        return target, {**plugin, "version": "2.0", "update_available": True}, runtime

    async def migrate(self):
        before = self.local_files()
        heat = self.heat.read_bytes()
        result = await self.market.migrate_legacy_sources()
        self.assertEqual(self.local_files(), before)
        self.assertEqual(self.heat.read_bytes(), heat)
        self.market.install.assert_not_awaited()
        self.market.record_install.assert_not_awaited()
        return result

    async def test_official_complete_directory_binds_without_install_or_heat(self):
        files = self.write_package({"__init__.py": entry(), "helpers.py": b"VALUE = 1\n",
                                    "frontend/dist/remoteEntry.js": b"export default {};\n"})
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        result = await self.migrate()
        self.assertEqual(result["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(self.candidate()))
        self.assertTrue(PluginMarket(Settings()).source_matches(self.candidate()))

    async def test_equal_entry_but_modified_helper_does_not_bind(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, {**files, f"{SOURCE_PATH}/helpers.py": b"VALUE = 999\n"})
        self.candidates(self.candidate())
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_missing_or_extra_published_file_does_not_bind(self):
        for variant in ("missing", "extra"):
            with self.subTest(variant=variant):
                files = self.write_package()
                if variant == "missing":
                    files.pop(f"{SOURCE_PATH}/helpers.py")
                else:
                    files[f"{SOURCE_PATH}/extra.py"] = b"EXTRA = True\n"
                self.remote.add(OFFICIAL_REPO, files, ref=variant)
                self.candidates({**self.candidate(), "branch": variant})
                self.assertEqual((await self.migrate())["bound"], [])

    async def test_historical_complete_source_matches_old_installed_version(self):
        old_files = self.write_package()
        current = {**old_files, f"{SOURCE_PATH}/__init__.py": entry("2.0")}
        self.remote.add(OFFICIAL_REPO, current)
        commit = "a" * 40
        self.remote.add(OFFICIAL_REPO, old_files, ref=commit, history=True)
        self.candidates(self.candidate(version="2.0"))
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(any(f"/git/trees/{commit}" in url for url in self.remote.urls))

    async def test_metadata_version_alone_is_not_source_proof(self):
        self.write_package()
        remote_files = {f"{SOURCE_PATH}/__init__.py": entry(extra="\nMALICIOUS_CHANGE = True\n"),
                        f"{SOURCE_PATH}/helpers.py": b"VALUE = 1\n"}
        self.remote.add(OFFICIAL_REPO, remote_files)
        self.candidates(self.candidate())
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_single_third_party_copy_without_local_source_evidence_is_not_trusted(self):
        files = self.write_package()
        third_party = "unknown/AWBotNest-Plugins"
        self.remote.add(third_party, files)
        self.candidates(self.candidate(third_party))
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_local_metadata_repository_evidence_allows_exact_third_party_match(self):
        third_party = "trusted/AWBotNest-Plugins"
        files = self.write_package({"__init__.py": entry(repo=third_party), "helpers.py": b"VALUE = 1\n"})
        self.remote.add(third_party, files)
        self.candidates(self.candidate(third_party))
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(self.candidate(third_party)))

    async def test_multiple_same_id_copies_are_not_arbitrarily_selected(self):
        files = self.write_package()
        for repo in ("first/plugins", "second/plugins"):
            self.remote.add(repo, files)
        self.candidates(self.candidate("first/plugins"), self.candidate("second/plugins"))
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_exact_official_source_wins_over_identical_fork(self):
        files = self.write_package()
        fork = "aaa-copy/plugins"
        self.remote.add(fork, files)
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate(fork), self.candidate())
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(self.candidate()))
        self.assertFalse(self.market.source_matches(self.candidate(fork)))

    async def test_existing_binding_is_not_replaced_or_reverified(self):
        self.write_package()
        bound = self.candidate("trusted/plugins")
        self.market.confirm_source(bound)
        self.candidates(self.candidate())
        self.assertEqual((await self.migrate())["bound"], [])
        self.assertTrue(self.market.source_matches(bound))
        self.assertEqual(self.remote.urls, [])

    async def test_failed_source_confirmation_restores_previous_binding_and_generation(self):
        original = self.candidate("trusted/plugins")
        self.market.confirm_source(original)
        disk_state = self.market._sources_path.read_bytes()
        generation = dict(self.market._source_generation)
        with patch.object(self.market, "_save_sources", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.market.confirm_source(self.candidate())
        self.assertTrue(self.market.source_matches(original))
        self.assertFalse(self.market.source_matches(self.candidate()))
        self.assertEqual(self.market._source_generation, generation)
        self.assertEqual(self.market._sources_path.read_bytes(), disk_state)
        self.assertTrue(PluginMarket(Settings()).source_matches(original))

    async def test_same_source_confirmation_does_not_require_another_disk_write(self):
        self.market.confirm_source(self.candidate())
        disk_state = self.market._sources_path.read_bytes()
        generation = dict(self.market._source_generation)
        with patch.object(self.market, "_save_sources", side_effect=OSError("fixture disk full")) as save:
            self.market.confirm_source({**self.candidate(), "repo": OFFICIAL_REPO.swapcase(), "version": "2.0"})
        save.assert_not_called()
        self.assertEqual(self.market._sources_path.read_bytes(), disk_state)
        self.assertEqual(self.market._source_generation, generation)

    async def test_poll_source_confirmation_failure_rolls_back_real_file_update(self):
        target, update, runtime = self.prepare_single_file_update()
        source_state = self.market._sources_path.read_bytes()
        with patch.object(self.market, "confirm_source", side_effect=OSError("fixture source persistence failed")):
            result = await self.market._poll_updates(runtime, listing={"plugins": [update]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["updated"], [])
        self.assertEqual(target.read_bytes(), entry("1.0"))
        self.assertTrue(self.market.source_matches(update))
        self.assertEqual(self.market._sources_path.read_bytes(), source_state)
        self.assertNotIn(PLUGIN_ID, self.market._pending_installs)
        self.market.record_install.assert_not_awaited()

    async def test_poll_heat_persistence_failure_does_not_undo_committed_update(self):
        target, update, runtime = self.prepare_single_file_update()
        del self.market.record_install
        heat_before = self.heat.read_bytes()
        with patch.object(self.market, "_save_heat_state", side_effect=OSError("fixture heat disk full")):
            result = await self.market._poll_updates(runtime, listing={"plugins": [update]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], ["Source fixture"])
        self.assertEqual(target.read_bytes(), entry("2.0"))
        self.assertTrue(self.market.source_matches(update))
        self.assertEqual(self.heat.read_bytes(), heat_before)
        self.assertNotIn(PLUGIN_ID, self.market._pending_installs)
        self.assertEqual(self.market._download_file.await_count, 1)

    async def test_committed_backup_cleanup_failure_keeps_new_code_and_clears_transaction(self):
        target, update, runtime = self.prepare_single_file_update()
        backup = self.plugins / f".{PLUGIN_ID}.py.backup"
        original_finish = self.market.finish
        original_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == backup:
                raise OSError("fixture backup temporarily locked")
            return original_unlink(path, *args, **kwargs)
        def finish(plugin_id, success):
            if success:
                with patch.object(Path, "unlink", unlink):
                    original_finish(plugin_id, success)
            else:
                original_finish(plugin_id, success)
        with patch.object(self.market, "finish", side_effect=finish):
            result = await self.market._poll_updates(runtime, listing={"plugins": [update]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], ["Source fixture"])
        self.assertEqual(target.read_bytes(), entry("2.0"))
        self.assertEqual(backup.read_bytes(), entry("1.0"))
        self.assertNotIn(PLUGIN_ID, self.market._pending_installs)
        self.assertTrue(self.market.source_matches(update))

    async def test_failed_source_confirmation_preserves_uploaded_local_only_state(self):
        self.market.forget_source(PLUGIN_ID)
        disk_state = self.market._sources_path.read_bytes()
        generation = dict(self.market._source_generation)
        with patch.object(self.market, "_save_sources", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.market.confirm_source(self.candidate())
        self.assertFalse(self.market.source_matches(self.candidate()))
        self.assertNotIn(PLUGIN_ID, self.market._sources)
        self.assertIn(PLUGIN_ID, self.market._local_only)
        self.assertEqual(self.market._source_generation, generation)
        self.assertEqual(self.market._sources_path.read_bytes(), disk_state)
        restored = PluginMarket(Settings())
        self.assertIn(PLUGIN_ID, restored._local_only)
        self.assertFalse(restored.source_matches(self.candidate()))

    async def test_failed_source_removal_restores_previous_binding(self):
        original = self.candidate()
        self.market.confirm_source(original)
        disk_state = self.market._sources_path.read_bytes()
        generation = dict(self.market._source_generation)
        with patch.object(self.market, "_save_sources", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.market.forget_source(PLUGIN_ID)
        self.assertTrue(self.market.source_matches(original))
        self.assertNotIn(PLUGIN_ID, self.market._local_only)
        self.assertEqual(self.market._source_generation, generation)
        self.assertEqual(self.market._sources_path.read_bytes(), disk_state)

    async def test_failed_migration_save_does_not_allow_later_automatic_update(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        with patch.object(self.market, "_save_sources", side_effect=OSError("fixture disk full")):
            self.assertEqual((await self.migrate())["bound"], [])
        self.assertFalse(self.market.source_matches(self.candidate()))
        self.assertNotIn(PLUGIN_ID, self.market._sources)
        self.assertFalse(PluginMarket(Settings()).source_matches(self.candidate()))
        update = {**self.candidate(version="2.0"), "update_available": True}
        self.market.refresh = AsyncMock(return_value={"plugins": [update]})
        runtime = SimpleNamespace(loaded={}, display_name=lambda value: value)
        result = await self.market._poll_updates(runtime)
        self.assertEqual(result["updated"], [])
        self.market.install.assert_not_awaited()
        self.market.record_install.assert_not_awaited()

    async def test_poll_rechecks_local_version_after_in_flight_manual_update(self):
        self.write_package()
        self.market.confirm_source(self.candidate())
        update = {**self.candidate(version="2.0"), "update_available": True,
                  "installed_version": "1.0"}
        self.market._cache = {"plugins": [update]}
        self.market.refresh = AsyncMock(return_value={"plugins": [update]})
        async def manually_update_while_migration_is_running():
            async with self.market.install_lock:
                (self.plugins / PLUGIN_ID / "__init__.py").write_bytes(entry("2.0"))
            return {"bound": [], "checked": 0, "requests": 0}
        self.market.migrate_legacy_sources = manually_update_while_migration_is_running
        runtime = SimpleNamespace(loaded={}, display_name=lambda value: value,
                                  invalidate_scan_cache=lambda: None)
        result = await self.market.poll_updates(runtime)
        self.assertEqual(result["updated"], [])
        self.market.install.assert_not_awaited()
        self.market.record_install.assert_not_awaited()

    async def test_v1_single_file_is_not_bound_when_v2_directory_is_active(self):
        package_files = self.write_package()
        single = entry("0.5")
        (self.plugins / f"{PLUGIN_ID}.py").write_bytes(single)
        self.remote.add(OFFICIAL_REPO, {**package_files, f"legacy/{PLUGIN_ID}.py": single})
        self.candidates(self.candidate(path=f"legacy/{PLUGIN_ID}.py", version="0.5"), self.candidate())
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(self.candidate()))
        self.assertFalse(self.market.source_matches(self.candidate(path=f"legacy/{PLUGIN_ID}.py")))

    async def test_single_file_plugin_can_bind_its_exact_source(self):
        source = entry()
        (self.plugins / f"{PLUGIN_ID}.py").write_bytes(source)
        path = f"plugins/{PLUGIN_ID}.py"
        self.remote.add(OFFICIAL_REPO, {path: source})
        plugin = self.candidate(path=path)
        self.candidates(plugin)
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(plugin))

    async def test_explicit_local_upload_stays_local_across_restart(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.market.confirm_source(self.candidate())
        self.market.forget_source(PLUGIN_ID)
        self.market = PluginMarket(Settings())
        self.market._github = self.remote
        self.market.install = AsyncMock()
        self.market.record_install = AsyncMock()
        self.candidates(self.candidate())
        self.assertEqual((await self.migrate())["bound"], [])
        self.assertEqual(self.remote.urls, [])
        self.market.confirm_source(self.candidate())
        self.assertTrue(self.market.source_matches(self.candidate()))

    async def test_incomplete_repository_listing_does_not_establish_source(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate(), complete=False)
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_unrelated_failed_repository_does_not_block_verified_official_source(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.market.settings.plugin_repos = [OFFICIAL_REPO, "unreachable/plugins"]
        async def list_repo(repo):
            if repo != OFFICIAL_REPO:
                raise httpx.ConnectError("fixture unrelated repository offline")
            return {"plugins": [self.candidate()]}
        self.market.list_repo = list_repo
        self.market._install_counts = AsyncMock(return_value={})
        await self.market.list_all()
        self.assertFalse(self.market._source_candidates_complete)
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(self.candidate()))

    async def test_unrelated_failed_repository_does_not_block_explicit_third_party_source(self):
        third_party = "trusted/plugins"
        files = self.write_package({"__init__.py": entry(repo=third_party), "helpers.py": b"VALUE = 1\n"})
        self.remote.add(third_party, files)
        self.market.settings.plugin_repos = [OFFICIAL_REPO, third_party]
        async def list_repo(repo):
            if repo != third_party:
                raise httpx.ConnectError("fixture unrelated repository offline")
            return {"plugins": [self.candidate(third_party)]}
        self.market.list_repo = list_repo
        self.market._install_counts = AsyncMock(return_value={})
        await self.market.list_all()
        self.assertFalse(self.market._source_candidates_complete)
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(self.candidate(third_party)))

    async def test_truncated_remote_tree_and_symlink_modes_do_not_bind(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        self.remote.truncated.add(OFFICIAL_REPO)
        self.assertEqual((await self.migrate())["bound"], [])
        self.remote.truncated.clear()
        # Changed candidates invalidate the previous failed-match cache.
        self.candidates({**self.candidate(), "branch": "release"})
        self.remote.add(OFFICIAL_REPO, files, ref="release")
        self.remote.symlink_blobs.add((OFFICIAL_REPO, f"{SOURCE_PATH}/helpers.py"))
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_local_symlink_is_rejected_without_reading_target(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        target = self.plugins / PLUGIN_ID / "helpers.py"
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            return SimpleNamespace(st_mode=stat.S_IFLNK) if path == target else original(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat):
            self.assertEqual((await self.migrate())["bound"], [])
        self.assertEqual(self.remote.urls, [])

    async def test_local_junction_is_rejected_without_reading_target(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        target = self.plugins / PLUGIN_ID
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                                       st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat):
            self.assertEqual((await self.migrate())["bound"], [])
        self.assertEqual(self.remote.urls, [])

    async def test_empty_non_source_directories_do_not_break_background_verification(self):
        files = self.write_package()
        (self.plugins / PLUGIN_ID / "empty").mkdir()
        (self.plugins / PLUGIN_ID / "__pycache__").mkdir()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertTrue(self.market.source_matches(self.candidate()))

    async def test_resolved_file_outside_plugin_root_is_rejected_without_network(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        target = self.plugins / PLUGIN_ID / "helpers.py"
        outside = self.root / "outside.py"
        outside.write_bytes(b"OUTSIDE_SECRET = True\n")
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            return outside if path == target else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            self.assertEqual((await self.migrate())["bound"], [])
        self.assertEqual(self.remote.urls, [])

    async def test_offline_failure_is_cached_but_changed_code_retries(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        self.remote.offline = True
        self.assertEqual((await self.migrate())["bound"], [])
        count = len(self.remote.urls)
        self.assertEqual((await self.migrate())["bound"], [])
        self.assertEqual(len(self.remote.urls), count)
        changed = entry(extra="\nNEW_CODE = True\n")
        (self.plugins / PLUGIN_ID / "__init__.py").write_bytes(changed)
        self.remote.offline = False
        self.remote.add(OFFICIAL_REPO, {**files, f"{SOURCE_PATH}/__init__.py": changed})
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertGreater(len(self.remote.urls), count)

    async def test_failed_verification_retries_after_retry_deadline(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        self.remote.offline = True
        self.assertEqual((await self.migrate())["bound"], [])
        cache_key, deadline = self.market._migration_failures[PLUGIN_ID]
        self.assertGreater(deadline, time.monotonic())
        self.market._migration_failures[PLUGIN_ID] = (cache_key, time.monotonic() - 1)
        self.remote.offline = False
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertNotIn(PLUGIN_ID, self.market._migration_failures)

    async def test_changed_candidates_retry_without_waiting_for_failure_deadline(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        self.remote.offline = True
        self.assertEqual((await self.migrate())["bound"], [])
        count = len(self.remote.urls)
        self.remote.offline = False
        self.remote.add(OFFICIAL_REPO, files, ref="release")
        self.candidates({**self.candidate(), "branch": "release"})
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertGreater(len(self.remote.urls), count)

    async def test_ambiguous_official_paths_are_not_arbitrarily_selected(self):
        files = self.write_package()
        alternate = "duplicate/source_sample"
        duplicate = {path.replace(SOURCE_PATH, alternate, 1): value for path, value in files.items()}
        self.remote.add(OFFICIAL_REPO, {**files, **duplicate})
        self.candidates(self.candidate(), self.candidate(path=alternate))
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_failed_same_repository_candidate_does_not_create_false_unique_match(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate(), {**self.candidate(), "branch": "unreachable"})
        self.assertEqual((await self.migrate())["bound"], [])

    async def test_binding_reselects_deduplicated_market_copy_without_changing_heat(self):
        third_party = "trusted/plugins"
        files = self.write_package({"__init__.py": entry(repo=third_party), "helpers.py": b"VALUE = 1\n"})
        self.remote.add(third_party, files)
        copy = self.candidate("aaa-copy/plugins")
        trusted = self.candidate(third_party)
        self.candidates(copy, trusted)
        self.market._cache = {"plugins": [copy], "install_counts": {PLUGIN_ID: 7}}
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        displayed = self.market.cached()["plugins"]
        self.assertEqual(len(displayed), 1)
        self.assertEqual(displayed[0]["repo"], third_party)
        self.assertEqual(displayed[0]["install_count"], 7)
        self.assertTrue(displayed[0]["auto_update_allowed"])

    async def test_many_unrelated_forks_do_not_use_eligible_candidate_budget(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        copies = [self.candidate(f"copy{number}/plugins") for number in range(130)]
        self.candidates(*copies, self.candidate())
        self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertEqual(len(self.remote.urls), 1)

    async def test_request_budget_stops_history_walk_without_guessing(self):
        old_files = self.write_package()
        self.remote.add(OFFICIAL_REPO, {**old_files, f"{SOURCE_PATH}/__init__.py": entry("2.0")})
        self.remote.add(OFFICIAL_REPO, old_files, ref="b" * 40, history=True)
        self.candidates(self.candidate(version="2.0"))
        with patch("awbotnest.market.SOURCE_MIGRATION_MAX_REQUESTS", 1):
            result = await self.migrate()
        self.assertEqual(result["bound"], [])
        self.assertLessEqual(len(self.remote.urls), 1)

    async def test_total_response_budget_stops_after_multiple_small_trees_without_binding(self):
        third_party = "trusted/plugins"
        files = self.write_package({"__init__.py": entry(repo=third_party), "helpers.py": b"VALUE = 1\n"})
        self.remote.add(OFFICIAL_REPO, {**files, f"{SOURCE_PATH}/helpers.py": b"VALUE = 999\n"})
        self.remote.add(third_party, files)
        self.candidates(self.candidate(), self.candidate(third_party))
        response = await self.remote(f"https://api.github.com/repos/{OFFICIAL_REPO}/git/trees/main?recursive=1")
        first_tree_bytes = len(response.content)
        self.remote.urls.clear()
        # The first tree and its empty history fit individually and together;
        # the otherwise valid third-party tree exceeds the cumulative budget.
        with patch("awbotnest.market.SOURCE_MIGRATION_MAX_TOTAL_RESPONSE_BYTES", first_tree_bytes + 2):
            result = await self.migrate()
        self.assertEqual(result["bound"], [])
        self.assertEqual(len(self.remote.urls), 3)
        self.assertFalse(self.market.source_matches(self.candidate(third_party)))

    async def test_shared_tree_response_is_reused_without_charging_bytes_twice(self):
        plugin_ids = ("first_sample", "second_sample")
        files, candidates = {}, []
        for plugin_id in plugin_ids:
            directory = self.plugins / plugin_id
            directory.mkdir()
            content = entry().replace(PLUGIN_ID.encode(), plugin_id.encode())
            (directory / "__init__.py").write_bytes(content)
            source = f"plugins_v2/{plugin_id}"
            files[f"{source}/__init__.py"] = content
            candidates.append({**self.candidate(path=source), "id": plugin_id})
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(*candidates)
        response = await self.remote(f"https://api.github.com/repos/{OFFICIAL_REPO}/git/trees/main?recursive=1")
        tree_bytes = len(response.content)
        self.remote.urls.clear()
        with patch("awbotnest.market.SOURCE_MIGRATION_MAX_TOTAL_RESPONSE_BYTES", tree_bytes):
            self.assertEqual((await self.migrate())["bound"], list(plugin_ids))
        self.assertEqual(len(self.remote.urls), 1)

    async def test_oversized_local_file_set_is_not_sent_for_verification(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        with patch("awbotnest.market.SOURCE_MIGRATION_MAX_FILES", 1):
            self.assertEqual((await self.migrate())["bound"], [])
        self.assertEqual(self.remote.urls, [])

    async def test_oversized_local_bytes_are_not_sent_for_verification(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        with patch("awbotnest.market.SOURCE_MIGRATION_MAX_BYTES", 1):
            self.assertEqual((await self.migrate())["bound"], [])
        self.assertEqual(self.remote.urls, [])

    async def test_source_confirmation_is_committed_under_install_lock(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        original = self.market.confirm_source
        commits = []
        def confirm(plugin):
            self.assertTrue(self.market.install_lock.locked())
            commits.append(plugin["id"])
            original(plugin)
        with patch.object(self.market, "confirm_source", side_effect=confirm):
            self.assertEqual((await self.migrate())["bound"], [PLUGIN_ID])
        self.assertEqual(commits, [PLUGIN_ID])

    async def test_remote_check_timeout_does_not_commit_partial_proof(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        async def stall(repo, ref):
            await asyncio.sleep(0.05)
        self.remote.before_tree = stall
        with patch("awbotnest.market.SOURCE_MIGRATION_MAX_SECONDS", 0.01):
            result = await asyncio.wait_for(self.migrate(), 1)
        self.assertEqual(result["bound"], [])
        self.assertFalse(self.market.source_matches(self.candidate()))

    async def test_uploaded_local_only_marker_blocks_in_flight_binding(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        entered, resume = asyncio.Event(), asyncio.Event()
        async def pause(repo, ref):
            entered.set()
            await resume.wait()
        self.remote.before_tree = pause
        task = asyncio.create_task(self.market.migrate_legacy_sources())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async with self.market.install_lock:
                self.market.forget_source(PLUGIN_ID)
            resume.set()
            self.assertEqual((await asyncio.wait_for(task, 2))["bound"], [])
            self.assertFalse(self.market.source_matches(self.candidate()))
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_changed_local_source_while_remote_check_runs_is_not_bound(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        entered, resume = asyncio.Event(), asyncio.Event()
        async def pause(repo, ref):
            entered.set()
            await resume.wait()
        self.remote.before_tree = pause
        task = asyncio.create_task(self.market.migrate_legacy_sources())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async with self.market.install_lock:
                (self.plugins / PLUGIN_ID / "helpers.py").write_bytes(b"UPLOADED_CHANGE = True\n")
            resume.set()
            result = await asyncio.wait_for(task, 2)
            self.assertEqual(result["bound"], [])
            self.assertFalse(self.market.source_matches(self.candidate()))
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_removed_repository_while_remote_check_runs_is_not_bound(self):
        third_party = "trusted/plugins"
        self.market.settings.plugin_repos = [OFFICIAL_REPO, third_party]
        files = self.write_package({"__init__.py": entry(repo=third_party), "helpers.py": b"VALUE = 1\n"})
        self.remote.add(third_party, files)
        self.candidates(self.candidate(third_party))
        entered, resume = asyncio.Event(), asyncio.Event()
        async def pause(repo, ref):
            entered.set()
            await resume.wait()
        self.remote.before_tree = pause
        task = asyncio.create_task(self.market.migrate_legacy_sources())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async with self.market.install_lock:
                self.market.settings.plugin_repos = [OFFICIAL_REPO]
            resume.set()
            self.assertEqual((await asyncio.wait_for(task, 2))["bound"], [])
            self.assertFalse(self.market.source_matches(self.candidate(third_party)))
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_changed_candidates_while_remote_check_runs_are_not_bound(self):
        files = self.write_package()
        self.remote.add(OFFICIAL_REPO, files)
        self.candidates(self.candidate())
        entered, resume = asyncio.Event(), asyncio.Event()
        async def pause(repo, ref):
            entered.set()
            await resume.wait()
        self.remote.before_tree = pause
        task = asyncio.create_task(self.market.migrate_legacy_sources())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.candidates(self.candidate(), self.candidate("unknown/copy"))
            resume.set()
            self.assertEqual((await asyncio.wait_for(task, 2))["bound"], [])
            self.assertFalse(self.market.source_matches(self.candidate()))
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
