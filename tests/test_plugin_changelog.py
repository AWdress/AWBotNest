from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kernel.registry import PluginRegistry
from webui.github_import import _try_manifest


class _ManifestResponse:
    status_code = 200

    def json(self):
        return {
            "demo": {
                "name": "演示插件",
                "version": "1.2.0",
                "changelog": "增加历史记录入口",
                "path": "demo.py",
            }
        }


class _ManifestClient:
    async def get(self, *_args, **_kwargs):
        return _ManifestResponse()


class PluginChangelogTests(unittest.IsolatedAsyncioTestCase):
    def test_local_plugin_changelog_is_returned_to_frontend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_file = root / "plugins" / "demo.py"
            plugin_file.parent.mkdir(parents=True)
            plugin_file.write_text(
                "__plugin__ = {\n"
                "    'name': '演示插件', 'id': 'demo', 'version': '1.2.0',\n"
                "    'scope': 'user', 'changelog': '增加历史记录入口',\n"
                "}\n",
                encoding="utf-8",
            )

            registry = PluginRegistry(plugin_file.parent, root / "data" / "plugins_state.json")
            meta = registry.parse_meta(plugin_file)

            self.assertEqual(meta.changelog, "增加历史记录入口")
            self.assertEqual(meta.to_dict()["changelog"], "增加历史记录入口")

    async def test_market_manifest_keeps_changelog(self):
        plugins = await _try_manifest(_ManifestClient(), "owner", "repo", "main", "")

        self.assertIsNotNone(plugins)
        self.assertEqual(plugins[0]["changelog"], "增加历史记录入口")


if __name__ == "__main__":
    unittest.main()
