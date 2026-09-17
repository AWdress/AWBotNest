import tempfile
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


if __name__ == "__main__":
    unittest.main()
