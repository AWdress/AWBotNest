import tempfile
import asyncio
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from awbotnest.config import Settings
from awbotnest.notifier import NotificationService
from awbotnest.rich_delivery import send_rich
from awbotnest.telegram import TelegramAccounts
from awbotnest.plugins import PluginRuntime
from telethon.errors import ApiIdInvalidError, AccessTokenInvalidError


class TelegramReconnectTests(unittest.IsolatedAsyncioTestCase):
    def test_clients_keep_reconnecting_until_network_recovers(self):
        settings = Settings(api_id=12345, api_hash="hash")
        with tempfile.TemporaryDirectory() as directory:
            accounts = TelegramAccounts(settings, Path(directory))
            client = SimpleNamespace()
            with patch("awbotnest.telegram.TelegramClient", return_value=client) as constructor, \
                    patch("awbotnest.telegram.install_rich_methods", side_effect=lambda value: value), \
                    patch("awbotnest.telegram.install_client_hooks", side_effect=lambda value: value):
                self.assertIs(accounts._client(str(Path(directory) / "account")), client)

        kwargs = constructor.call_args.kwargs
        self.assertTrue(kwargs["auto_reconnect"])
        self.assertIsNone(kwargs["connection_retries"])
        self.assertEqual(kwargs["retry_delay"], 5)

    def test_first_user_id_is_saved_as_bot_notification_target(self):
        settings = Settings(api_id=12345, api_hash="hash")
        with tempfile.TemporaryDirectory() as directory, \
                patch("awbotnest.config.save_settings") as save_settings:
            accounts = TelegramAccounts(settings, Path(directory))
            accounts._remember_notification_target("account", {"user_id": 123456})

        self.assertEqual(settings.default_bot_chat_id, "123456")
        save_settings.assert_called_once_with(settings)

    async def test_offline_user_does_not_block_bot_notification(self):
        settings = Settings(
            api_id=12345,
            api_hash="hash",
            bot_token="token",
            user_sessions=["account"],
        )
        with tempfile.TemporaryDirectory() as directory:
            accounts = TelegramAccounts(settings, Path(directory))
            accounts._profiles_path = Path(directory) / "profiles.json"
            accounts._profiles_path.write_text(
                json.dumps({"account": {"user_id": 123456}}), encoding="utf-8",
            )
            bot = SimpleNamespace(is_connected=lambda: False)
            accounts.bots["default"] = bot
            accounts.users["account"] = SimpleNamespace(is_connected=lambda: False)
            service = NotificationService(settings, accounts, SimpleNamespace())

            with patch("awbotnest.notifier.send_rich", AsyncMock(return_value="sent")) as send:
                result = await service.send("hello", _record=False)

        self.assertEqual(result, "sent")
        self.assertIs(send.await_args.args[0], bot)
        self.assertEqual(send.await_args.args[1], 123456)
        self.assertEqual(send.await_args.kwargs["token"], "token")

    async def test_bot_rich_fallback_uses_standard_api_without_restart(self):
        requests = []

        async def handler(request):
            requests.append((request.url.path, json.loads(request.content)))
            if request.url.path.endswith("/sendRichMessage"):
                return httpx.Response(404, json={"ok": False, "error_code": 404})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

        with patch(
            "awbotnest.rich_delivery.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            result = await send_rich(
                None, 123456, "<b>通知</b>", "通知", token="test-token",
            )

        self.assertEqual(result, {"message_id": 7})
        self.assertEqual(requests, [
            ("/bottest-token/sendRichMessage", {
                "chat_id": 123456, "rich_message": {"html": "<b>通知</b>"},
            }),
            ("/bottest-token/sendMessage", {"chat_id": 123456, "text": "通知"}),
        ])


class TelegramStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(prefix="awbotnest-account-recovery-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data_patch = patch("awbotnest.telegram.DATA_DIR", self.root)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.save_patch = patch("awbotnest.config.save_settings")
        self.save_patch.start()
        self.addCleanup(self.save_patch.stop)
        self.settings = Settings(api_id=12345, api_hash="hash", user_sessions=["account"])
        (self.root / "account.session").touch()
        self.accounts = TelegramAccounts(self.settings, self.root)
        self.accounts._retry_delay = .005
        self.accounts._cache_profile = AsyncMock(return_value={"user_id": 123456})
        self.accounts.on_recovered = AsyncMock()
        self.addAsyncCleanup(self.accounts.stop)

    @staticmethod
    def client(*, connect_error=None, bot_error=None, authorized=True):
        return SimpleNamespace(
            connect=AsyncMock(side_effect=connect_error),
            start=AsyncMock(side_effect=bot_error),
            disconnect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=authorized),
            is_connected=lambda: True,
        )

    async def finish_recovery(self, kind="user", name="account"):
        task = self.accounts._recovery_tasks[(kind, name)]
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
        await asyncio.sleep(0)

    async def test_first_user_network_failure_recovers_and_rebinds_once(self):
        failed = self.client(connect_error=TimeoutError("offline"))
        recovered = self.client()
        with patch.object(self.accounts, "_client", side_effect=[failed, recovered]) as constructor:
            await self.accounts.start()
            self.assertNotIn("account", self.accounts.users)
            await self.finish_recovery()
        self.assertIs(self.accounts.users["account"], recovered)
        self.assertEqual(constructor.call_count, 2)
        failed.disconnect.assert_awaited_once()
        self.accounts.on_recovered.assert_awaited_once_with("user", "account")
        self.assertFalse(self.accounts._recovery_tasks)

    async def test_first_bot_network_failure_recovers_and_rebinds(self):
        self.settings.user_sessions = []
        self.settings.bot_token = "token"
        failed = self.client(bot_error=ConnectionError("offline"))
        recovered = self.client()
        with patch.object(self.accounts, "_client", side_effect=[failed, recovered]):
            await self.accounts.start()
            await self.finish_recovery("bot", "default")
        self.assertIs(self.accounts.bots["default"], recovered)
        self.accounts.on_recovered.assert_awaited_once_with("bot", "default")

    async def test_invalid_credentials_and_expired_sessions_are_not_retried(self):
        for client in (self.client(connect_error=ApiIdInvalidError(None)), self.client(authorized=False)):
            with self.subTest(client=client), patch.object(self.accounts, "_client", return_value=client) as constructor:
                await self.accounts.start()
                self.assertEqual(constructor.call_count, 1)
                self.assertFalse(self.accounts.users)
                self.assertFalse(self.accounts._recovery_tasks)
        self.accounts.on_recovered.assert_not_awaited()

    async def test_invalid_bot_token_is_not_retried(self):
        self.settings.user_sessions = []
        self.settings.bot_token = "invalid"
        failed = self.client(bot_error=AccessTokenInvalidError(None))
        with patch.object(self.accounts, "_client", return_value=failed):
            await self.accounts.start()
        self.assertFalse(self.accounts.bots)
        self.assertFalse(self.accounts._recovery_tasks)

    async def test_manual_disconnect_cancels_recovery_until_manual_connect(self):
        failed = self.client(connect_error=TimeoutError("offline"))
        in_flight = self.client()
        manual = self.client()
        connecting = asyncio.Event()
        async def wait_for_network():
            connecting.set()
            await asyncio.Event().wait()
        in_flight.connect.side_effect = wait_for_network
        with patch.object(self.accounts, "_client", side_effect=[failed, in_flight, manual]):
            await self.accounts.start()
            await asyncio.wait_for(connecting.wait(), timeout=1)
            self.assertTrue(await self.accounts.disconnect_user("account"))
            self.assertFalse(self.accounts._recovery_tasks)
            self.assertFalse(self.accounts.users)
            in_flight.disconnect.assert_awaited_once()
            self.assertTrue(await self.accounts.connect_user("account"))
            self.assertIs(self.accounts.users["account"], manual)
        self.accounts.on_recovered.assert_not_awaited()

    async def test_deleting_offline_account_removes_pending_recovery(self):
        self.accounts._retry_delay = 100
        failed = self.client(connect_error=TimeoutError("offline"))
        with patch.object(self.accounts, "_client", return_value=failed) as constructor:
            await self.accounts.start()
            self.assertTrue(await self.accounts.delete_user("account"))
            self.assertFalse(self.accounts._recovery_tasks)
            self.assertFalse(self.accounts.users)
            self.assertNotIn("account", self.settings.user_sessions)
            self.assertFalse((self.root / "account.session").exists())
            self.assertEqual(constructor.call_count, 1)

    async def test_shutdown_cancels_inflight_retry_and_cleans_client(self):
        failed = self.client(connect_error=TimeoutError("offline"))
        retry_client = self.client()
        connecting = asyncio.Event()
        async def wait_for_network():
            connecting.set()
            await asyncio.Event().wait()
        retry_client.connect.side_effect = wait_for_network
        with patch.object(self.accounts, "_client", side_effect=[failed, retry_client]):
            await self.accounts.start()
            await asyncio.wait_for(connecting.wait(), timeout=1)
            await self.accounts.stop()
        retry_client.disconnect.assert_awaited_once()
        self.assertFalse(self.accounts.users)
        self.assertFalse(self.accounts._recovery_tasks)
        self.accounts.on_recovered.assert_not_awaited()

    async def test_binding_failure_retries_without_recreating_connected_account(self):
        failed = self.client(connect_error=TimeoutError("offline"))
        recovered = self.client()
        self.accounts.on_recovered.side_effect = [RuntimeError("binding failure"), None]
        with patch.object(self.accounts, "_client", side_effect=[failed, recovered]) as constructor:
            await self.accounts.start()
            await self.finish_recovery()
        self.assertEqual(constructor.call_count, 2)
        self.assertEqual(self.accounts.on_recovered.await_count, 2)
        self.assertIs(self.accounts.users["account"], recovered)

    async def test_recovery_refresh_preserves_plugins_selected_for_other_accounts(self):
        runtime = object.__new__(PluginRuntime)
        runtime.settings = SimpleNamespace(
            enabled_plugins=["selected", "other", "bot"],
            plugin_accounts={"selected": ["account"], "other": ["other_account"]},
        )
        runtime.scan = lambda: [SimpleNamespace(id=name, scope=scope) for name, scope in
                                (("selected", "user"), ("other", "user"), ("bot", "bot"))]
        runtime.loaded = {name: object() for name in runtime.settings.enabled_plugins}
        runtime.disable = AsyncMock()
        runtime.restore = AsyncMock()
        await runtime.refresh_telegram_plugins(scopes={"user", "both"}, account_name="account")
        runtime.disable.assert_awaited_once_with("selected", persist=False)
        runtime.restore.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
