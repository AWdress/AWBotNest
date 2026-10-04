"""WeCom notification acknowledgements must not hide failed or uncertain delivery."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from awbotnest.config import Settings
from awbotnest.notifier import NotificationService
from awbotnest.rich_delivery import DeliveryUncertain


def response(body=None, *, text=None, status=200):
    request = httpx.Request("POST", "https://example.test/wecom")
    return httpx.Response(status, text=text, request=request) if text is not None else httpx.Response(status, json=body, request=request)


class WeComDeliveryEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(user_sessions=["primary"], bot_routing={"plugin": "wecom"},
            notification_channels=[{"id": "wecom", "type": "wecom", "corpid": "wwfixture",
                "secret": "fixture-secret", "agentid": "1000002", "touser": "alice|bob"}])
        self.http = SimpleNamespace(get=AsyncMock(return_value=response({"errcode": 0, "access_token": "fixture-access-token"})),
                                    post=AsyncMock(return_value=response({"errcode": 0})))
        self.accounts = SimpleNamespace(users={"primary": SimpleNamespace(is_connected=lambda: True)})
        self.service = NotificationService(self.settings, self.accounts, self.http)
        self.service.history_path = Path(self.temporary.name) / "history.json"

    async def test_invalid_application_acknowledgements_are_not_reported_as_success(self):
        for reply in (response(text="<html>upstream failed</html>"), response(text=""),
                      response([]), response({}), response({"errcode": False}), response({"errcode": "0"})):
            with self.subTest(reply=reply.text):
                self.http.post.return_value = reply
                with self.assertRaises(DeliveryUncertain):
                    await self.service.send("通知", channel="wecom", _record=False)

    async def test_group_webhook_requires_an_explicit_success_acknowledgement(self):
        self.settings.notification_channels[0]["webhook"] = "https://example.test/group"
        for reply in (response(text="maintenance"), response({"ok": True}), response(None)):
            with self.subTest(reply=reply.text):
                self.http.post.return_value = reply
                with self.assertRaises(DeliveryUncertain):
                    await self.service.send("通知", channel="wecom", _record=False)
        self.http.get.assert_not_awaited()
        self.http.post.return_value = response({"errcode": 0, "errmsg": "ok"})
        self.assertEqual(await self.service.send("通知", channel="wecom", _record=False),
                         {"errcode": 0, "errmsg": "ok"})

    async def test_explicit_rejection_is_safe_and_keeps_error_code(self):
        self.http.post.return_value = response({"errcode": 40003, "errmsg": "secret=do-not-return"})
        with self.assertRaises(RuntimeError) as caught:
            await self.service.send("通知", channel="wecom", _record=False)
        self.assertNotIsInstance(caught.exception, DeliveryUncertain)
        self.assertIn("40003", str(caught.exception))
        self.assertNotIn("do-not-return", str(caught.exception))

    async def test_partial_recipient_failure_does_not_fall_back_and_duplicate_delivery(self):
        for key in ("invaliduser", "invalidparty", "invalidtag", "unlicenseduser"):
            with self.subTest(key=key):
                self.http.post.reset_mock()
                self.http.post.return_value = response({"errcode": 0, key: "bob", "msgid": "delivered-to-alice"})
                with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock) as telegram:
                    with self.assertRaises(DeliveryUncertain):
                        await self.service.send("通知", plugin_id="plugin")
                    telegram.assert_not_awaited()
                self.http.post.assert_awaited_once()

    async def test_http_error_after_post_is_not_assumed_to_be_an_undelivered_request(self):
        for status in (400, 502, 504):
            with self.subTest(status=status):
                self.http.post.return_value = response(text="gateway failure", status=status)
                with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock) as telegram:
                    with self.assertRaises(DeliveryUncertain):
                        await self.service.send("通知", plugin_id="plugin")
                    telegram.assert_not_awaited()

    async def test_lost_post_acknowledgement_never_uses_automatic_fallback(self):
        for kind in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                     httpx.WriteError, httpx.RemoteProtocolError, httpx.DecodingError,
                     httpx.TooManyRedirects):
            with self.subTest(kind=kind.__name__):
                self.http.post.side_effect = kind("fixture network uncertainty")
                with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock) as telegram:
                    with self.assertRaises(DeliveryUncertain):
                        await self.service.send("通知", plugin_id="plugin")
                    telegram.assert_not_awaited()

    async def test_connection_failure_is_safe_and_can_use_normal_notification_fallback(self):
        for kind in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError):
            with self.subTest(kind=kind.__name__):
                self.http.post.side_effect = kind("access_token=do-not-return")
                with self.assertRaises(RuntimeError) as caught:
                    await self.service.send("通知", channel="wecom", _record=False)
                self.assertNotIsInstance(caught.exception, DeliveryUncertain)
                self.assertNotIn("do-not-return", str(caught.exception))
                with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock, return_value="sent") as telegram:
                    self.assertEqual(await self.service.send("通知", plugin_id="plugin"), "sent")
                    telegram.assert_awaited_once()

    async def test_token_request_failure_still_allows_normal_notification_fallback(self):
        self.http.get.side_effect = httpx.ConnectError("fixture connection refused")
        with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock, return_value="sent") as telegram:
            self.assertEqual(await self.service.send("通知", plugin_id="plugin"), "sent")
            telegram.assert_awaited_once()
        self.http.post.assert_not_awaited()

    async def test_private_result_member_ids_are_case_insensitive_without_changing_recipient(self):
        self.settings.notification_channels[0].update(callback_enabled=True, callback_users="Alice|BOB",
            callback_token="fixture-token", callback_aes_key="A" * 43)
        with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock) as telegram:
            self.assertEqual(await self.service.send_wecom_text("wecom", "aLiCe", "完成"), {"errcode": 0})
            telegram.assert_not_awaited()
        self.assertEqual(self.http.post.await_args.kwargs["json"]["touser"], "aLiCe")
        with self.assertRaises(PermissionError):
            await self.service.send_wecom_text("wecom", "another-member", "完成")


if __name__ == "__main__":
    unittest.main()
