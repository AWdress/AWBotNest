"""插件在单文件与目录两种形态之间迁移时的回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awbotnest.config import Settings
from awbotnest.deps import DependencyManager
from awbotnest.market import PluginMarket
from awbotnest.plugin_runtime.resolver import PluginResolver
from awbotnest.plugin_runtime.scanner import PluginScanner

PLUGIN_ID = "juai_checkin"
SINGLE_SOURCE = '__plugin__ = {"name": "JUAI", "id": "juai_checkin", "version": "1.5.0", "scope": "standalone"}\n'
PACKAGE_SOURCE = '__plugin__ = {"name": "JUAI", "id": "juai_checkin", "version": "2.0.2", "scope": "standalone"}\n'


class FakeTreeResponse:
    def json(self) -> dict:
        return {"tree": [{"type": "blob", "path": f"plugins_v2/{PLUGIN_ID}/__init__.py"}]}

    def raise_for_status(self) -> None:
        return None


class OfflineHTTPClient:
    """热度中心不可达：安装热度是增强信息，离线时也必须给出本地统计。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        raise RuntimeError("离线")

    async def __aexit__(self, *exc_info) -> bool:
        return False


class PluginFormMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.plugins = Path(self.temporary.name) / "plugins"
        self.plugins.mkdir(parents=True)
        self.addCleanup(self.temporary.cleanup)

    def write_single(self, source: str = SINGLE_SOURCE) -> Path:
        path = self.plugins / f"{PLUGIN_ID}.py"
        path.write_text(source, encoding="utf-8")
        return path

    def write_package(self, source: str = PACKAGE_SOURCE) -> Path:
        package = self.plugins / PLUGIN_ID
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text(source, encoding="utf-8")
        return package

    def package_plugin(self) -> dict:
        return {"id": PLUGIN_ID, "name": "JUAI", "version": "2.0.2",
                "repo": "yyned2501/AWBotNest-Plugins", "branch": "main",
                "path": f"plugins_v2/{PLUGIN_ID}/"}

    def market(self) -> PluginMarket:
        with patch("awbotnest.market.PLUGINS_DIR", self.plugins):
            market = PluginMarket(Settings())
            market._download_file = self.download
            market._github = self.github
            return market

    async def download(self, repo: str, branch: str, path) -> bytes:
        # 单文件与目录入口都以 .py 结尾，只能按远端路径区分。
        if path.as_posix().endswith("__init__.py"):
            return PACKAGE_SOURCE.encode()
        return SINGLE_SOURCE.encode()

    async def github(self, url: str) -> FakeTreeResponse:
        return FakeTreeResponse()

    async def test_installed_version_prefers_package_entry(self):
        """两种形态并存时版本必须读运行时真正加载的目录入口。"""
        self.write_single()
        self.write_package()
        with patch("awbotnest.market.PLUGINS_DIR", self.plugins):
            self.assertEqual(PluginMarket._installed_version(PLUGIN_ID), "2.0.2")
        self.assertFalse(PluginMarket._newer("2.0.2", "2.0.2"))

    async def test_scanner_reports_one_entry_when_both_forms_exist(self):
        """残留旧单文件不得让同一插件在列表里出现两条。"""
        self.write_single()
        self.write_package()
        scanner = PluginScanner(Settings(), DependencyManager(Settings()),
                               self.plugins, {}, PluginResolver())
        metas = scanner.scan()
        self.assertEqual([meta.id for meta in metas], [PLUGIN_ID])
        self.assertEqual(metas[0].version, "2.0.2")

    async def test_package_install_removes_legacy_single_file(self):
        self.write_single()
        with patch("awbotnest.market.PLUGINS_DIR", self.plugins):
            market = self.market()
            await market.install(self.package_plugin())
            market.finish(PLUGIN_ID, True)
            self.assertFalse((self.plugins / f"{PLUGIN_ID}.py").exists())
            self.assertEqual(market._installed_version(PLUGIN_ID), "2.0.2")
        self.assertEqual(sorted(path.name for path in self.plugins.iterdir()), [PLUGIN_ID])

    async def test_single_file_install_removes_legacy_package(self):
        self.write_package()
        plugin = {**self.package_plugin(), "path": f"{PLUGIN_ID}.py"}
        with patch("awbotnest.market.PLUGINS_DIR", self.plugins):
            market = self.market()
            await market.install(plugin)
            market.finish(PLUGIN_ID, True)
            self.assertFalse((self.plugins / PLUGIN_ID).exists())
            self.assertEqual(market._installed_version(PLUGIN_ID), "1.5.0")
        self.assertEqual(sorted(path.name for path in self.plugins.iterdir()), [f"{PLUGIN_ID}.py"])

    async def test_failed_install_restores_both_forms(self):
        self.write_single()
        self.write_package()
        with patch("awbotnest.market.PLUGINS_DIR", self.plugins):
            market = self.market()
            await market.install(self.package_plugin())
            market.finish(PLUGIN_ID, False)
            self.assertTrue((self.plugins / f"{PLUGIN_ID}.py").exists())
            self.assertEqual(market._installed_version(PLUGIN_ID), "2.0.2")

    async def test_failed_single_file_install_restores_package(self):
        """单文件安装失败时，被移走的目录形态必须完整还原。"""
        self.write_package()
        plugin = {**self.package_plugin(), "path": f"{PLUGIN_ID}.py"}
        with patch("awbotnest.market.PLUGINS_DIR", self.plugins):
            market = self.market()
            await market.install(plugin)
            market.finish(PLUGIN_ID, False)
            self.assertTrue((self.plugins / PLUGIN_ID / "__init__.py").exists())
            self.assertFalse((self.plugins / f"{PLUGIN_ID}.py").exists())
            self.assertEqual(market._installed_version(PLUGIN_ID), "2.0.2")

    async def test_heat_inventory_ignores_backups_and_templates(self):
        self.write_single()
        (self.plugins / f"_{PLUGIN_ID}.py.backup").write_text(SINGLE_SOURCE, encoding="utf-8")
        (self.plugins / "_TEMPLATE.py").write_text("__plugin__ = {}\n", encoding="utf-8")
        with patch("awbotnest.market.PLUGINS_DIR", self.plugins), \
             patch("awbotnest.market.httpx.AsyncClient", OfflineHTTPClient):
            counts = await self.market()._install_counts()
        self.assertIn(PLUGIN_ID, counts)
        self.assertNotIn("_TEMPLATE", counts)
        self.assertNotIn(f"_{PLUGIN_ID}", counts)


if __name__ == "__main__":
    unittest.main()
