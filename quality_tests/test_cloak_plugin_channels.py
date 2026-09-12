from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from awbotnest.config import Settings
from awbotnest.deps import DependencyManager
from awbotnest.plugin_runtime import PluginResolver, PluginScanner


class CloakPluginChannelTests(unittest.TestCase):
    def test_source_detection_distinguishes_channels_and_version_pins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stable = root / "stable.py"
            preview = root / "preview.py"
            pinned = root / "pinned.py"
            dynamic = root / "dynamic.py"
            platform_browser = root / "platform_browser.py"
            stable.write_text(
                "from cloakbrowser import launch\nlaunch()\n", encoding="utf-8",
            )
            preview.write_text(
                "import cloakbrowser as cb\ncb.launch_async(release_channel='preview')\n",
                encoding="utf-8",
            )
            pinned.write_text(
                "from cloakbrowser.browser import launch\n"
                "launch(browser_version='152.0.7977.82.1')\n",
                encoding="utf-8",
            )
            dynamic.write_text(
                "from cloakbrowser import launch\nlaunch(**options)\n", encoding="utf-8",
            )
            platform_browser.write_text(
                "async def run(ctx):\n    await ctx.browser.run('https://example.test', str)\n",
                encoding="utf-8",
            )

            detect = PluginScanner.source_cloakbrowser_channels
            self.assertEqual(detect(stable), {"stable"})
            self.assertEqual(detect(preview), {"preview"})
            self.assertEqual(detect(pinned), set())
            self.assertEqual(detect(dynamic), set())
            self.assertEqual(detect(platform_browser), {"stable"})

    def test_only_enabled_plugins_contribute_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "stable.py").write_text(
                "__plugin__ = {'id': 'stable', 'name': 'Stable', 'version': '1', "
                "'scope': 'standalone'}\n"
                "from cloakbrowser import launch\nlaunch()\n",
                encoding="utf-8",
            )
            (root / "preview.py").write_text(
                "__plugin__ = {'id': 'preview', 'name': 'Preview', 'version': '1', "
                "'scope': 'standalone'}\n"
                "from cloakbrowser import launch\nlaunch(release_channel='preview')\n",
                encoding="utf-8",
            )
            settings = Settings(enabled_plugins=["preview"])
            scanner = PluginScanner(
                settings, DependencyManager(settings), root, {}, PluginResolver(),
            )
            self.assertEqual(scanner.cloakbrowser_channels(), ("preview",))
            settings.enabled_plugins = ["stable"]
            self.assertEqual(scanner.cloakbrowser_channels(), ("stable",))


if __name__ == "__main__":
    unittest.main()
