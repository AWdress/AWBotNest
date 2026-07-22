import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kernel import notifier
from kernel.account_manager import _bots_from_settings
from webui import api as web_api


class FakeClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.send_message = AsyncMock(return_value=True)


class NotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_channels_are_all_sent(self) -> None:
        channels = {
            "work": {"id": "work", "type": "wechat", "enabled": True, "config": {}},
            "phone": {"id": "phone", "type": "bark", "enabled": True, "config": {}},
        }
        accounts = SimpleNamespace(primary_user_app=None, bot_apps={})
        send = AsyncMock(return_value=True)

        with (
            patch.object(notifier, "_plugin_bot_id", return_value="work,phone"),
            patch.object(notifier, "_get_channel_config", side_effect=channels.get),
            patch("kernel.notification_channels.send_notification", send),
        ):
            result = await notifier.submit(accounts, "demo", "示例插件", "测试通知")

        self.assertTrue(result)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(
            {call.kwargs["channel_type"] for call in send.await_args_list},
            {"wechat", "bark"},
        )

    async def test_all_failed_channels_fall_back_to_saved_messages(self) -> None:
        user = FakeClient()
        accounts = SimpleNamespace(primary_user_app=user, bot_apps={})
        channel = {"id": "phone", "type": "bark", "enabled": True, "config": {}}

        with (
            patch.object(notifier, "_plugin_bot_id", return_value="phone"),
            patch.object(notifier, "_get_channel_config", return_value=channel),
            patch("kernel.notification_channels.send_notification", AsyncMock(return_value=False)),
        ):
            result = await notifier.submit(accounts, "demo", "示例插件", "测试通知")

        self.assertTrue(result)
        user.send_message.assert_awaited_once()
        self.assertEqual(user.send_message.await_args.args[0], "me")

    async def test_disabled_channel_is_not_sent(self) -> None:
        user = FakeClient()
        accounts = SimpleNamespace(primary_user_app=user, bot_apps={})
        disabled = {"id": "phone", "type": "bark", "enabled": False, "config": {}}
        send = AsyncMock(return_value=True)

        with (
            patch.object(notifier, "_plugin_bot_id", return_value="phone"),
            patch.object(notifier, "_get_channel_config", return_value=disabled),
            patch("kernel.notification_channels.send_notification", send),
        ):
            await notifier.submit(accounts, "demo", "示例插件", "测试通知")

        send.assert_not_awaited()
        user.send_message.assert_awaited_once()


class NotificationConfigTests(unittest.TestCase):
    def test_disabled_telegram_channel_is_not_started(self) -> None:
        settings = {
            "BOT_TOKEN": "",
            "BOTS": [],
            "NOTIFICATION_CHANNELS": [
                {
                    "id": "disabled",
                    "name": "已停用",
                    "type": "telegram",
                    "enabled": False,
                    "config": {"token": "secret"},
                },
                {
                    "id": "enabled",
                    "name": "工作通知",
                    "type": "telegram",
                    "enabled": True,
                    "config": {"token": "active"},
                },
            ],
        }

        bots = _bots_from_settings(settings)

        self.assertEqual([bot["id"] for bot in bots], ["enabled"])

    def test_masked_channel_secrets_keep_original_values(self) -> None:
        current = [
            {
                "id": "work",
                "name": "企业微信",
                "type": "wechat",
                "enabled": True,
                "is_default": True,
                "config": {"corpid": "corp", "secret": "real-secret"},
                "plugins": [],
            }
        ]
        incoming = [
            {
                **current[0],
                "name": "工作通知",
                "config": {"corpid": "corp", "secret": "********"},
            }
        ]

        cleaned = web_api._clean_notification_channels(incoming, current)

        self.assertEqual(cleaned[0]["config"]["secret"], "real-secret")
        self.assertEqual(cleaned[0]["name"], "工作通知")
        self.assertNotIn("plugins", cleaned[0])

    def test_first_migration_restores_masked_legacy_bot_tokens(self) -> None:
        incoming = [
            {
                "id": "default",
                "name": "主要通知渠道",
                "type": "telegram",
                "enabled": True,
                "is_default": True,
                "config": {"token": "********"},
                "plugins": [],
            },
            {
                "id": "alerts",
                "name": "提醒",
                "type": "telegram",
                "enabled": True,
                "is_default": False,
                "config": {"token": "********"},
                "plugins": [],
            },
        ]
        legacy = {
            "BOT_TOKEN": "builtin-token",
            "BOTS": [{"id": "alerts", "token": "alerts-token"}],
        }

        cleaned = web_api._clean_notification_channels(incoming, [], legacy)

        self.assertEqual(cleaned[0]["config"]["token"], "builtin-token")
        self.assertEqual(cleaned[1]["config"]["token"], "alerts-token")


class NotificationSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_telegram_default_channel_is_reported_as_the_only_default(self) -> None:
        accounts = SimpleNamespace(list_bots=AsyncMock(return_value=[{
            "id": "default", "name": "Telegram", "online": True,
            "username": "bot", "is_default": True, "is_builtin": True,
        }]))
        settings = {
            "NOTIFICATION_CHANNELS": [
                {
                    "id": "default", "name": "Telegram", "type": "telegram",
                    "enabled": True, "is_default": False, "config": {"token": "token"},
                },
                {
                    "id": "phone", "name": "手机", "type": "bark",
                    "enabled": True, "is_default": True, "config": {"device_key": "key"},
                },
            ]
        }

        with (
            patch.object(web_api, "_get_accounts", return_value=accounts),
            patch("config.config.load", return_value=settings),
            patch.object(web_api.registry, "scan", return_value=[]),
        ):
            response = await web_api.get_bots_routing(user={})

        defaults = [item["id"] for item in response["bots"] if item["is_default"]]
        self.assertEqual(defaults, ["phone"])

    async def test_bot_plugin_cannot_route_to_non_telegram_channel(self) -> None:
        accounts = SimpleNamespace(list_bots=AsyncMock(return_value=[]))
        meta = SimpleNamespace(scope="bot")
        settings = {
            "NOTIFICATION_CHANNELS": [{
                "id": "phone", "name": "手机", "type": "bark",
                "enabled": True, "is_default": False, "config": {"device_key": "key"},
            }]
        }

        with (
            patch.object(web_api, "_get_accounts", return_value=accounts),
            patch("config.config.load", return_value=settings),
            patch.object(web_api.registry, "get_meta", return_value=meta),
        ):
            with self.assertRaises(web_api.HTTPException) as raised:
                await web_api.set_bots_routing(
                    {"plugin_id": "demo", "bot_id": "phone"}, user={},
                )

        self.assertEqual(raised.exception.status_code, 400)

    async def test_channel_secrets_are_masked_in_settings_response(self) -> None:
        stored = {
            "API_HASH": "hash",
            "BOT_TOKEN": "bot-token",
            "PLUGIN_REPOS": [],
            "NOTIFICATION_CHANNELS": [
                {
                    "id": "work",
                    "name": "企业微信",
                    "type": "wechat",
                    "enabled": True,
                    "config": {"corpid": "corp", "secret": "real-secret"},
                },
                {
                    "id": "phone",
                    "name": "Bark",
                    "type": "bark",
                    "enabled": True,
                    "config": {"device_key": "device-secret"},
                },
            ],
        }

        with (
            patch("config.config.load", return_value=stored),
            patch("schedulers.universal.log_cleaner.get_log_cleaner_settings", return_value={}),
        ):
            response = await web_api.get_settings_api(user={})

        channels = response["settings"]["NOTIFICATION_CHANNELS"]
        self.assertEqual(channels[0]["config"]["secret"], "********")
        self.assertEqual(channels[1]["config"]["device_key"], "********")
        self.assertEqual(stored["NOTIFICATION_CHANNELS"][0]["config"]["secret"], "real-secret")

    async def test_disabling_channel_removes_only_that_route_and_resyncs(self) -> None:
        current = {
            "BOT_TOKEN": "",
            "BOT_NAME": "主要通知渠道",
            "DEFAULT_BOT_ID": "default",
            "BOTS": [],
            "NOTIFICATION_CHANNELS": [
                {
                    "id": "work",
                    "name": "工作通知",
                    "type": "wechat",
                    "enabled": True,
                    "is_default": False,
                    "config": {"corpid": "corp", "secret": "secret"},
                }
            ],
        }
        incoming = [{**current["NOTIFICATION_CHANNELS"][0], "enabled": False}]
        accounts = SimpleNamespace(sync_bots=AsyncMock(return_value={
            "default_id": "default", "failed": [], "needs_resync": False,
        }))
        runtime = SimpleNamespace(resync=AsyncMock())

        with (
            patch("config.config.load", return_value=current),
            patch("config.config.save"),
            patch.object(web_api, "_get_accounts", return_value=accounts),
            patch.object(web_api, "_get_runtime", return_value=runtime),
            patch.object(web_api.registry, "purge_bot", return_value=["demo"]) as purge,
        ):
            response = await web_api.put_settings_api(
                {"settings": {"NOTIFICATION_CHANNELS": incoming}}, user={},
            )

        self.assertEqual(response["status"], "success")
        purge.assert_called_once_with("work")
        runtime.resync.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
