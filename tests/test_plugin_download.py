from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kernel import state as kernel_state
from webui import repo_sync
from webui.github_import import _TREE_CACHE, _list_dir_files
from webui.repo_sync import _reload_running


class _TreeResponse:
    status_code = 200
    headers = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "truncated": False,
            "tree": [
                {"type": "blob", "path": "plugins/demo/__init__.py"},
                {"type": "blob", "path": "plugins/demo/frontend/dist/app.js"},
                {"type": "blob", "path": "plugins/other.py"},
                {"type": "tree", "path": "plugins/demo/frontend"},
            ],
        }


class _TreeClient:
    def __init__(self) -> None:
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _TreeResponse()


class _FailedReloadRuntime:
    def __init__(self) -> None:
        self.loaded = True

    def is_loaded(self, _plugin_id: str) -> bool:
        return self.loaded

    async def reload(self, _plugin_id: str):
        self.loaded = False
        return SimpleNamespace(error="示例错误")


class PluginDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_listing_uses_one_tree_request(self) -> None:
        _TREE_CACHE.clear()
        client = _TreeClient()

        files = await _list_dir_files(
            client, "owner", "repo", "feature/test", "plugins/demo"
        )

        self.assertEqual(len(client.calls), 1)
        self.assertIn("feature%2Ftest", client.calls[0][0])
        self.assertEqual(
            [item["path"] for item in files],
            ["plugins/demo/__init__.py", "plugins/demo/frontend/dist/app.js"],
        )

        await _list_dir_files(client, "owner", "repo", "feature/test", "plugins/other")
        self.assertEqual(len(client.calls), 1)

    async def test_failed_reload_is_not_reported_as_success(self) -> None:
        runtime = _FailedReloadRuntime()
        with patch.object(kernel_state, "runtime", runtime):
            reloaded, errors = await _reload_running(["demo"])

        self.assertEqual(reloaded, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("示例错误", errors[0])

    async def test_folder_plugin_download_writes_all_files_and_version(self) -> None:
        plugin = {
            "id": "demo",
            "name": "示例插件",
            "version": "1.2.0",
            "repo_url": "owner/repo",
        }
        files = [
            {"target": "demo/__init__.py", "download_url": "https://raw/init"},
            {"target": "demo/frontend/app.js", "download_url": "https://raw/app"},
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(repo_sync, "PLUGINS_DIR", root / "plugins"),
                patch.object(repo_sync, "STATE_PATH", root / "data" / "repo_sync.json"),
                patch.object(repo_sync, "_get_repos", return_value=[{"url": "owner/repo"}]),
                patch.object(repo_sync, "_refresh_registry"),
                patch.object(repo_sync.github_import, "resolve_files", AsyncMock(return_value=files)),
                patch.object(
                    repo_sync.github_import,
                    "fetch_files",
                    AsyncMock(return_value=[b"plugin code", b"frontend code"]),
                ),
                patch.object(
                    repo_sync,
                    "_reload_running",
                    AsyncMock(return_value=([], [])),
                ),
            ):
                result = await repo_sync.download_plugins([plugin])

            self.assertTrue(result["ok"])
            self.assertEqual(result["downloaded"], ["demo"])
            self.assertEqual((root / "plugins" / "demo" / "__init__.py").read_bytes(), b"plugin code")
            self.assertEqual((root / "plugins" / "demo" / "frontend" / "app.js").read_bytes(), b"frontend code")
            self.assertEqual(repo_sync.json.loads((root / "data" / "repo_sync.json").read_text())["versions"]["demo"], "1.2.0")


if __name__ == "__main__":
    unittest.main()
