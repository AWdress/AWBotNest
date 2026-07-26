from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from webui import github_import, repo_sync


class _BrokenManifestResponse:
    status_code = 200

    def json(self):
        raise ValueError("bad json")


class _BrokenManifestClient:
    async def get(self, *_args, **_kwargs):
        return _BrokenManifestResponse()


class RepoStoreResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_broken_manifest_does_not_fall_back_to_directory_scan(self):
        with self.assertRaisesRegex(ValueError, "插件清单无效"):
            await github_import._try_manifest(
                _BrokenManifestClient(), "owner", "repo", "main", ""
            )

    async def test_failed_repo_refresh_keeps_previous_metadata(self):
        cached = {
            "id": "demo",
            "name": "演示插件",
            "version": "1.2.3",
            "description": "完整描述",
            "icon": "https://example/icon.png",
            "repo_url": repo_sync.OFFICIAL_REPO,
            "official": True,
        }
        state = {
            "last_sync": "old",
            "store": [cached],
            "versions": {"demo": "1.2.0"},
            "official_ids": ["demo"],
        }
        with (
            patch.object(repo_sync, "_load_state", return_value=state),
            patch.object(repo_sync, "_save_state") as save,
            patch.object(repo_sync, "_get_repos", return_value=[
                {"url": repo_sync.OFFICIAL_REPO, "official": True},
            ]),
            patch.object(repo_sync, "_local_exists", return_value=True),
            patch.object(
                repo_sync.github_import,
                "list_plugins",
                new=AsyncMock(side_effect=ValueError("manifest broken")),
            ),
        ):
            result = await repo_sync.list_store(refresh=True)

        self.assertEqual(result["plugins"][0]["name"], "演示插件")
        self.assertEqual(result["plugins"][0]["description"], "完整描述")
        self.assertEqual(result["official_ids"], ["demo"])
        self.assertTrue(result["errors"])
        self.assertEqual(save.call_args.args[0]["store"][0]["icon"], "https://example/icon.png")


if __name__ == "__main__":
    unittest.main()
