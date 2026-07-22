import unittest
import asyncio
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from config import config
from kernel.account_manager import AccountManager
from kernel import notifier
from kernel.registry import PluginRegistry


class BotDefaultTests(unittest.TestCase):
    def test_only_existing_bot_can_be_default(self) -> None:
        bots = [{"id": "alerts"}, {"id": "orders"}]

        self.assertEqual(config.normalize_default_bot_id("orders", bots), "orders")
        self.assertEqual(config.normalize_default_bot_id("missing", bots), "default")

    def test_selected_default_and_builtin_can_both_be_resolved(self) -> None:
        accounts = AccountManager.__new__(AccountManager)
        accounts.bot_apps = {"default": "builtin-client", "alerts": "alerts-client"}
        accounts.default_bot_id = "alerts"

        self.assertEqual(accounts.get_bot(), "alerts-client")
        self.assertEqual(accounts.get_bot("default"), "builtin-client")
        self.assertEqual(accounts.get_bot("missing,alerts"), "alerts-client")
        self.assertEqual(accounts.get_bot("missing"), "alerts-client")

    def test_default_extra_bot_uses_its_own_chat_id(self) -> None:
        settings = {
            "DEFAULT_BOT_CHAT_ID": "100",
            "BOTS": [{"id": "alerts", "chat_id": "-200"}],
        }

        with patch("config.config.load", return_value=settings):
            self.assertEqual(notifier._bot_chat_id("alerts"), -200)
            self.assertEqual(notifier._bot_chat_id("default"), 100)

    def test_plugin_can_explicitly_select_builtin_bot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = PluginRegistry(root / "plugins", root / "state.json")

            registry.set_bot_choice("demo", "default")
            self.assertEqual(registry.get_bot_choice("demo"), "default")

            registry.set_bot_choice("demo", "")
            self.assertEqual(registry.get_bot_choice("demo"), "")

    def test_removing_one_channel_keeps_other_selected_channels(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = PluginRegistry(root / "plugins", root / "state.json")
            registry.set_bot_choice("demo", "work,phone")

            affected = registry.purge_bot("work")

            self.assertEqual(affected, ["demo"])
            self.assertEqual(registry.get_bot_choice("demo"), "phone")

    def test_plugin_scan_cache_refreshes_state_and_file_changes(self) -> None:
        source = "__plugin__ = {'name': '示例', 'id': 'demo', 'version': '%s', 'scope': 'user'}\n"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = PluginRegistry(root / "plugins", root / "state.json")
            entry = root / "plugins" / "demo.py"
            entry.write_text(source % "1.0.0", encoding="utf-8")

            with patch.object(registry, "parse_meta", wraps=registry.parse_meta) as parse:
                self.assertEqual(registry.scan()[0].version, "1.0.0")
                registry.set_enabled("demo", True)
                self.assertTrue(registry.scan()[0].enabled)
                self.assertEqual(parse.call_count, 1)

                entry.write_text(source % "1.0.1", encoding="utf-8")
                self.assertEqual(registry.scan()[0].version, "1.0.1")
                self.assertEqual(parse.call_count, 2)


class FakeBot:
    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.me = None
        self.start_count = 0
        self.stop_count = 0

    async def start(self) -> None:
        self.start_count += 1
        self.is_connected = True

    async def stop(self) -> None:
        self.stop_count += 1
        self.is_connected = False


class BotHotSyncTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, bots: dict[str, FakeBot], default: str = "default") -> AccountManager:
        accounts = AccountManager.__new__(AccountManager)
        accounts.bot_apps = dict(bots)
        accounts.default_bot_id = default
        accounts._bot_names = {bot_id: bot_id for bot_id in bots}
        accounts._bot_sync_lock = asyncio.Lock()
        return accounts

    async def test_new_bot_starts_and_becomes_default_immediately(self) -> None:
        builtin = FakeBot()
        alerts = FakeBot()
        accounts = self.manager({"default": builtin})
        accounts._start_bot_client = AsyncMock(return_value=(alerts, ""))
        previous = {"BOT_TOKEN": "old", "BOT_NAME": "内置", "BOTS": [], "DEFAULT_BOT_ID": "default"}
        current = {
            **previous,
            "BOTS": [{"id": "alerts", "name": "提醒", "token": "new"}],
            "DEFAULT_BOT_ID": "alerts",
        }

        with patch("kernel.account_manager.manager", SimpleNamespace(bot_app=None)):
            result = await accounts.sync_bots(previous, current)

        self.assertIs(accounts.bot_apps["alerts"], alerts)
        self.assertEqual(accounts.default_bot_id, "alerts")
        self.assertTrue(result["needs_resync"])
        self.assertEqual(result["failed"], [])

    async def test_bad_replacement_token_keeps_old_connection(self) -> None:
        old_bot = FakeBot()
        accounts = self.manager({"default": old_bot})
        accounts._start_bot_client = AsyncMock(return_value=(None, "Token 无效"))
        previous = {"BOT_TOKEN": "old", "BOT_NAME": "内置", "BOTS": [], "DEFAULT_BOT_ID": "default"}
        current = {**previous, "BOT_TOKEN": "bad"}

        with patch("kernel.account_manager.manager", SimpleNamespace(bot_app=None)):
            result = await accounts.sync_bots(previous, current)

        self.assertIs(accounts.bot_apps["default"], old_bot)
        self.assertTrue(old_bot.is_connected)
        self.assertEqual(old_bot.stop_count, 0)
        self.assertEqual(old_bot.start_count, 0)
        self.assertEqual(result["failed"][0]["id"], "default")

    async def test_valid_replacement_starts_before_old_bot_stops(self) -> None:
        old_bot = FakeBot()
        new_bot = FakeBot()
        accounts = self.manager({"default": old_bot})
        accounts._start_bot_client = AsyncMock(return_value=(new_bot, ""))
        previous = {"BOT_TOKEN": "old", "BOT_NAME": "内置", "BOTS": [], "DEFAULT_BOT_ID": "default"}
        current = {**previous, "BOT_TOKEN": "new"}

        with patch("kernel.account_manager.manager", SimpleNamespace(bot_app=None)):
            result = await accounts.sync_bots(previous, current)

        self.assertIs(accounts.bot_apps["default"], new_bot)
        self.assertEqual(old_bot.stop_count, 1)
        self.assertEqual(result["failed"], [])

    async def test_removed_bot_disconnects_immediately(self) -> None:
        builtin = FakeBot()
        alerts = FakeBot()
        accounts = self.manager({"default": builtin, "alerts": alerts}, default="alerts")
        previous = {
            "BOT_TOKEN": "old",
            "BOT_NAME": "内置",
            "BOTS": [{"id": "alerts", "name": "提醒", "token": "new"}],
            "DEFAULT_BOT_ID": "alerts",
        }
        current = {"BOT_TOKEN": "old", "BOT_NAME": "内置", "BOTS": [], "DEFAULT_BOT_ID": "default"}

        with patch("kernel.account_manager.manager", SimpleNamespace(bot_app=None)):
            result = await accounts.sync_bots(previous, current)

        self.assertNotIn("alerts", accounts.bot_apps)
        self.assertFalse(alerts.is_connected)
        self.assertEqual(accounts.default_bot_id, "default")
        self.assertTrue(result["needs_resync"])


if __name__ == "__main__":
    unittest.main()
