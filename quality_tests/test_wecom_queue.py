"""Admission and shutdown remain safe under retries and cancellation."""
from __future__ import annotations

import asyncio
import base64
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from awbotnest.routing import PluginRoutes
from awbotnest.wecom_commands import WeComCommandService


CHANNEL = "wecom-queue"
MEMBER = "queue.admin"


def message(message_id, action="check"):
    return {"FromUserName": MEMBER, "MsgType": "text", "MsgId": str(message_id),
            "Content": f"/run sample {action}"}


class WeComQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store_path = Path(temporary.name) / "dedup.sqlite"
        self.settings = SimpleNamespace(notification_channels=[{
            "id": CHANNEL, "type": "wecom", "enabled": True, "corpid": "wwqueue",
            "agentid": "1000002", "secret": "queue-secret", "callback_enabled": True,
            "callback_token": "queue-token",
            "callback_aes_key": base64.b64encode(bytes(range(32))).decode().rstrip("="),
            "callback_users": MEMBER,
        }], bot_routing={"sample": CHANNEL})
        self.notifier = SimpleNamespace(send_wecom_text=AsyncMock(return_value={"errcode": 0}))
        self.runtime = SimpleNamespace(loaded={"sample": object()}, notifier=self.notifier,
                                       display_name=lambda _: "Queue fixture")
        self.routes = PluginRoutes()
        self.action = AsyncMock(return_value=True)
        self.routes.action("sample", "check", self.action)
        self.service = WeComCommandService(self.settings, self.runtime, self.routes, store_path=self.store_path)
        self.addAsyncCleanup(self.service.close)

    async def idle(self):
        await asyncio.wait_for(self.service._queue.join(), 2)

    async def test_repeated_accepted_message_is_acknowledged_even_when_queue_is_full(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def block(payload):
            entered.set()
            await release.wait()
        self.routes.action("sample", "block", block)
        try:
            await self.service.handle(CHANNEL, message(1, "block"))
            await asyncio.wait_for(entered.wait(), 1)
            for message_id in range(2, 18):
                await self.service.handle(CHANNEL, message(message_id))
            self.assertTrue(self.service._queue.full())
            reply = await self.service.handle(CHANNEL, message(2))
            self.assertIn("已接收", reply)
            with self.assertRaises(HTTPException) as raised:
                await self.service.handle(CHANNEL, message(18))
            self.assertEqual(raised.exception.status_code, 503)
        finally:
            release.set()
        await self.idle()
        self.assertIn("已提交", await self.service.handle(CHANNEL, message(18)))
        await self.idle()
        self.assertEqual(self.action.await_count, 17)

    async def test_repeated_request_cancellation_cannot_cancel_a_committing_admission(self):
        entered, release, committed = threading.Event(), threading.Event(), threading.Event()
        original = self.service._claim
        def claim(channel_id, message_id):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Fixture claim did not resume")
            result = original(channel_id, message_id)
            committed.set()
            return result
        with patch.object(self.service, "_claim", side_effect=claim):
            request = asyncio.create_task(self.service.handle(CHANNEL, message(20)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                request.cancel()
                await asyncio.sleep(0)
                request.cancel()
                await asyncio.sleep(0)
                release.set()
                self.assertTrue(await asyncio.to_thread(committed.wait, 1))
                with self.assertRaises(asyncio.CancelledError):
                    await request
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
        await self.idle()
        self.assertIn("已接收", await self.service.handle(CHANNEL, message(20)))
        self.action.assert_awaited_once()

    async def test_plugin_cancelled_error_does_not_strand_other_accepted_commands(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def cancel_itself(payload):
            entered.set()
            await release.wait()
            raise asyncio.CancelledError()
        self.routes.action("sample", "cancel", cancel_itself)
        try:
            await self.service.handle(CHANNEL, message(30, "cancel"))
            await asyncio.wait_for(entered.wait(), 1)
            await self.service.handle(CHANNEL, message(31))
            release.set()
            await self.idle()
            self.action.assert_awaited_once()
            self.assertFalse(self.service._worker.done())
        finally:
            release.set()

    async def test_cancelled_result_delivery_does_not_strand_other_accepted_commands(self):
        self.notifier.send_wecom_text.side_effect = [asyncio.CancelledError(), {"errcode": 0}]
        await self.service.handle(CHANNEL, message(32))
        await self.service.handle(CHANNEL, message(33))
        await self.idle()
        self.assertEqual(self.action.await_count, 2)
        self.assertEqual(self.notifier.send_wecom_text.await_count, 2)
        self.assertFalse(self.service._worker.done())

    async def test_plugin_cancelling_its_current_task_does_not_stop_the_worker(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def deliver(*args, **kwargs):
            await asyncio.sleep(0)
            return {"errcode": 0}
        self.notifier.send_wecom_text.side_effect = deliver
        async def gate(payload):
            entered.set()
            await release.wait()
        def cancel_current(payload):
            asyncio.current_task().cancel()
            return True
        self.routes.action("sample", "gate", gate)
        self.routes.action("sample", "cancel-current", cancel_current)
        try:
            await self.service.handle(CHANNEL, message(34, "gate"))
            await asyncio.wait_for(entered.wait(), 1)
            await self.service.handle(CHANNEL, message(35, "cancel-current"))
            await self.service.handle(CHANNEL, message(36))
            release.set()
            await self.idle()
            self.action.assert_awaited_once()
            self.assertFalse(self.service._worker.done())
        finally:
            release.set()

    async def test_shutdown_is_bounded_when_a_plugin_swallows_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def stubborn(payload):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        self.routes.action("sample", "stubborn", stubborn)
        await self.service.handle(CHANNEL, message(40, "stubborn"))
        await asyncio.wait_for(entered.wait(), 1)
        await self.service.handle(CHANNEL, message(41))
        with patch("awbotnest.wecom_commands.WECOM_SHUTDOWN_SECONDS", 0.03, create=True):
            closing = asyncio.create_task(self.service.close())
            try:
                done, _ = await asyncio.wait({closing}, timeout=0.15)
                self.assertIn(closing, done, "A stubborn plugin blocked system shutdown")
                self.assertTrue(self.service._closed)
            finally:
                release.set()
                await asyncio.gather(closing, return_exceptions=True)
        await asyncio.wait_for(self.service._worker, 1)
        await self.idle()
        self.action.assert_not_awaited()
        self.notifier.send_wecom_text.assert_not_awaited()

    async def test_shutdown_does_not_wait_indefinitely_for_an_admission_store(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._claim
        def claim(channel_id, message_id):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Fixture claim did not resume")
            return original(channel_id, message_id)
        with patch.object(self.service, "_claim", side_effect=claim), patch(
                "awbotnest.wecom_commands.WECOM_SHUTDOWN_SECONDS", 0.03, create=True):
            request = asyncio.create_task(self.service.handle(CHANNEL, message(50)))
            closing = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                closing = asyncio.create_task(self.service.close())
                done, _ = await asyncio.wait({closing}, timeout=0.15)
                self.assertIn(closing, done, "Admission I/O blocked system shutdown")
                self.assertTrue(self.service._closed)
            finally:
                release.set()
                await asyncio.gather(request, *(tuple([closing]) if closing else ()), return_exceptions=True)
        self.action.assert_not_awaited()
        self.assertTrue(self.service._queue.empty())

    async def test_permissions_revoked_during_admission_prevent_plugin_execution(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._claim
        def claim(channel_id, message_id):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Fixture claim did not resume")
            return original(channel_id, message_id)
        with patch.object(self.service, "_claim", side_effect=claim):
            request = asyncio.create_task(self.service.handle(CHANNEL, message(60)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.settings.notification_channels[0]["callback_users"] = "other-member"
                release.set()
                await asyncio.gather(request, return_exceptions=True)
            finally:
                release.set()
        await self.idle()
        self.action.assert_not_awaited()
        self.notifier.send_wecom_text.assert_not_awaited()
        self.settings.notification_channels[0]["callback_users"] = MEMBER
        restored = WeComCommandService(self.settings, self.runtime, self.routes, store_path=self.store_path)
        self.addAsyncCleanup(restored.close)
        self.assertIn("已提交", await restored.handle(CHANNEL, message(60)))
        await asyncio.wait_for(restored._queue.join(), 1)
        self.assertIn("已接收", await restored.handle(CHANNEL, message(60)))
        self.action.assert_awaited_once()

    async def test_member_case_is_ignored_without_changing_the_result_recipient(self):
        original_member = MEMBER.upper()
        request = {**message(70), "FromUserName": original_member}
        self.assertIn("已提交", await self.service.handle(CHANNEL, request))
        await self.idle()
        self.action.assert_awaited_once()
        self.assertEqual(self.notifier.send_wecom_text.await_args.args[:2], (CHANNEL, original_member))

    async def test_waiting_admission_does_not_start_after_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._claim
        def claim(channel_id, message_id):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Fixture claim did not resume")
            return original(channel_id, message_id)
        with patch.object(self.service, "_claim", side_effect=claim), patch(
                "awbotnest.wecom_commands.WECOM_SHUTDOWN_SECONDS", 0.03, create=True):
            first = asyncio.create_task(self.service.handle(CHANNEL, message(80)))
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                second = asyncio.create_task(self.service.handle(CHANNEL, message(81)))
                await asyncio.sleep(0)
                await self.service.close()
            finally:
                release.set()
                results = await asyncio.gather(first, *(tuple([second]) if second else ()),
                                               return_exceptions=True)
        self.assertTrue(all(isinstance(result, HTTPException) and result.status_code == 503
                            for result in results))
        self.assertFalse(self.service._received(CHANNEL, "80"))
        self.assertFalse(self.service._received(CHANNEL, "81"))
        self.assertEqual(len(self.service._admissions), 0)
        self.assertIsNone(self.service._worker)
        self.action.assert_not_awaited()
        restarted = WeComCommandService(self.settings, self.runtime, self.routes, store_path=self.store_path)
        self.addAsyncCleanup(restarted.close)
        self.assertIn("已提交", await restarted.handle(CHANNEL, message(80)))
        await asyncio.wait_for(restarted._queue.join(), 1)
        self.assertIn("已接收", await restarted.handle(CHANNEL, message(80)))
        self.action.assert_awaited_once()

    async def test_rejected_duplicate_admission_keeps_the_original_claim(self):
        self.assertIn("已提交", await self.service.handle(CHANNEL, message(90)))
        await self.idle()
        entered, release = threading.Event(), threading.Event()
        original = self.service._claim
        def claim(channel_id, message_id):
            result = original(channel_id, message_id)
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Fixture claim did not resume")
            return result
        with patch.object(self.service, "_claim", side_effect=claim):
            request = asyncio.create_task(self.service.handle(CHANNEL, message(90)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.settings.notification_channels[0]["callback_users"] = "other-member"
                release.set()
                with self.assertRaises(HTTPException) as rejected:
                    await request
                self.assertEqual(rejected.exception.status_code, 403)
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
        self.settings.notification_channels[0]["callback_users"] = MEMBER
        restarted = WeComCommandService(self.settings, self.runtime, self.routes, store_path=self.store_path)
        self.addAsyncCleanup(restarted.close)
        self.assertIn("已接收", await restarted.handle(CHANNEL, message(90)))
        self.action.assert_awaited_once()
