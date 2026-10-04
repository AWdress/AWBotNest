"""Authenticated WeCom callbacks never widen a member's plugin capabilities."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import struct
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from awbotnest.api import create_app
from awbotnest.backup import BackupManager
from awbotnest.config import Settings, validate_config_format
from awbotnest.logs import redact_secrets
from awbotnest.notifier import NotificationService
from awbotnest.routing import PluginRoutes
from awbotnest.scheduler import PluginScheduler
from awbotnest.wecom_commands import WeComCommandService
from awbotnest.wecom_crypto import WeComCrypto, WeComCryptoError, message_signature, parse_xml_fields


TOKEN = "wecom-callback-fixture-token"
KEY_BYTES = bytes(range(32))
AES_KEY = base64.b64encode(KEY_BYTES).decode().rstrip("=")
CORP_ID = "wwfixturecorporation"
AGENT_ID = "1000002"
MEMBER = "authorized.user"
CHANNEL_ID = "wecom-fixture"


def channel(**changes):
    return {"id": CHANNEL_ID, "name": "Fixture app", "type": "wecom", "enabled": True,
            "corpid": CORP_ID, "agentid": AGENT_ID, "secret": "fixture-app-secret",
            "callback_enabled": True, "callback_token": TOKEN, "callback_aes_key": AES_KEY,
            "callback_users": MEMBER, **changes}


def xml(fields):
    root = ET.Element("xml")
    for key, value in fields.items():
        ET.SubElement(root, key).text = str(value)
    return ET.tostring(root, encoding="utf-8")


def incoming(content="/帮助", **changes):
    return {"ToUserName": CORP_ID, "FromUserName": MEMBER, "CreateTime": str(int(time.time())),
            "MsgType": "text", "Content": content, "MsgId": "100001", "AgentID": AGENT_ID, **changes}


def encrypt_raw(raw, *, key=KEY_BYTES):
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()


def encrypt_independent(payload, *, corp_id=CORP_ID, key=KEY_BYTES, declared_length=None, invalid_padding=False):
    """Construct the official frame independently from the implementation."""
    payload = payload.encode() if isinstance(payload, str) else payload
    length = len(payload) if declared_length is None else declared_length
    frame = b"0123456789ABCDEF" + struct.pack("!I", length) + payload + corp_id.encode()
    padding = 32 - len(frame) % 32
    raw = frame + bytes([padding]) * padding
    if invalid_padding:
        raw = raw[:-1] + b"\x00"
    return encrypt_raw(raw, key=key)


def decrypt_independent(ciphertext, *, corp_id=CORP_ID):
    decryptor = Cipher(algorithms.AES(KEY_BYTES), modes.CBC(KEY_BYTES[:16])).decryptor()
    padded = decryptor.update(base64.b64decode(ciphertext, validate=True)) + decryptor.finalize()
    padding = padded[-1]
    if not 1 <= padding <= 32 or padded[-padding:] != bytes([padding]) * padding:
        raise AssertionError("Reply has invalid official 32-byte PKCS#7 padding")
    raw = padded[:-padding]
    length = struct.unpack("!I", raw[16:20])[0]
    payload, recipient = raw[20:20 + length], raw[20 + length:]
    if recipient != corp_id.encode():
        raise AssertionError("Reply has the wrong corp recipient")
    return payload


def signature(ciphertext, *, timestamp=None, nonce="fixture-nonce", token=TOKEN):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hashlib.sha1("".join(sorted((token, timestamp, nonce, ciphertext))).encode()).hexdigest()
    return {"msg_signature": digest, "timestamp": timestamp, "nonce": nonce}


class WeComCryptoTests(unittest.TestCase):
    def setUp(self):
        self.crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)

    def test_decrypts_independently_built_official_frames(self):
        for payload in (b"hello", "中文回调 & <消息>", b"x" * 12, b"x" * 48):
            with self.subTest(payload=payload):
                expected = payload.encode() if isinstance(payload, str) else payload
                self.assertEqual(self.crypto.decrypt(encrypt_independent(payload)), expected)

    def test_encrypts_frames_readable_by_independent_implementation(self):
        for payload in (b"reply", "中文回复 & <测试>", b"x" * 100):
            with self.subTest(payload=payload):
                expected = payload.encode() if isinstance(payload, str) else payload
                self.assertEqual(decrypt_independent(self.crypto.encrypt(payload)), expected)

    def test_signature_matches_sha1_of_sorted_values_and_rejects_tampering(self):
        ciphertext = encrypt_independent("challenge")
        signed = signature(ciphertext)
        self.assertEqual(message_signature(TOKEN, signed["timestamp"], signed["nonce"], ciphertext),
                         signed["msg_signature"])
        self.crypto.verify_signature(signed["msg_signature"], signed["timestamp"], signed["nonce"], ciphertext)
        for value in ("0" * 40, signed["msg_signature"][:-1], "", "not-a-sha1"):
            with self.subTest(signature=value), self.assertRaises(WeComCryptoError):
                self.crypto.verify_signature(value, signed["timestamp"], signed["nonce"], ciphertext)

    def test_wrong_corporation_is_not_treated_as_a_valid_message(self):
        with self.assertRaises(WeComCryptoError):
            self.crypto.decrypt(encrypt_independent("message", corp_id="wwanothercorporation"))

    def test_corrupted_padding_and_invalid_frame_lengths_are_rejected(self):
        for ciphertext in (encrypt_independent("message", invalid_padding=True),
                           encrypt_independent("message", declared_length=2**31),
                           encrypt_raw(b"x" * 16), encrypt_raw(b"x" * 32)):
            with self.subTest(ciphertext=ciphertext), self.assertRaises(WeComCryptoError):
                self.crypto.decrypt(ciphertext)

    def test_malformed_keys_and_ciphertext_are_rejected(self):
        for key in ("", "x" * 42, "x" * 44, "!" * 43, "é" * 43):
            with self.subTest(key=key), self.assertRaises((ValueError, UnicodeError)):
                WeComCrypto(TOKEN, key, CORP_ID)
        for ciphertext in ("", "not-base64!", base64.b64encode(b"x" * 17).decode()):
            with self.subTest(ciphertext=ciphertext), self.assertRaises(WeComCryptoError):
                self.crypto.decrypt(ciphertext)

    def test_xml_fields_preserve_text_and_reject_entities_duplicates_and_nested_fields(self):
        self.assertEqual(parse_xml_fields(xml({"Content": "中文 & <消息>", "MsgId": "001"})),
                         {"Content": "中文 & <消息>", "MsgId": "001"})
        invalid = [b'<!DOCTYPE xml [<!ENTITY x "secret">]><xml><Content>&x;</Content></xml>',
                   b'<!DOCTYPE xml SYSTEM "file:///etc/passwd"><xml/>',
                   b"<xml><Content>first</Content><Content>second</Content></xml>",
                   b"<xml><Content><nested>value</nested></Content></xml>",
                   b"<not-xml><Content>value</Content></not-xml>", b"<xml><Content>"]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(WeComCryptoError):
                parse_xml_fields(content)


class WeComConfigTests(unittest.TestCase):
    def test_valid_flat_and_nested_app_callbacks_are_allowed_in_backups(self):
        flat = channel()
        nested = {"id": CHANNEL_ID, "type": "wechat", "enabled": True,
                  "config": {key: value for key, value in flat.items()
                             if key not in {"id", "name", "type", "enabled"}}}
        for item in (flat, nested, channel(callback_enabled=False, callback_token="", callback_aes_key="",
                                         callback_users=""), {"id": "legacy", "type": "wecom"}):
            with self.subTest(channel=item):
                validate_config_format({"notification_channels": [item]})

    def test_invalid_enabled_callbacks_are_rejected_before_backup_restore(self):
        changes = ({"callback_enabled": "true"}, {"callback_token": ""}, {"callback_token": 123},
                   {"callback_aes_key": "x" * 42}, {"callback_aes_key": "!" * 43},
                   {"callback_users": ""}, {"callback_users": "|  |"}, {"callback_users": "@all"},
                   {"callback_users": [MEMBER]}, {"corpid": ""}, {"agentid": "not-numeric"},
                   {"secret": ""}, {"url": "https://example.test/robot"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config_format({"notification_channels": [channel(**change)]})

    def test_callback_secrets_are_redacted_from_logs_and_bad_config_backups_are_rejected(self):
        redacted = redact_secrets(f"callback_token={TOKEN} callback_aes_key={AES_KEY}")
        self.assertNotIn(TOKEN, redacted)
        self.assertNotIn(AES_KEY, redacted)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format": "awbotnest-config", "version": 1}))
            archive.writestr("config/system.json", json.dumps({"notification_channels": [
                channel(callback_enabled=True, callback_aes_key="invalid")]}))
            archive.writestr("config/plugins.json", "{}")
        with self.assertRaises(ValueError):
            BackupManager._read_content(buffer.getvalue())


class WeComCallbackHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(admin_token="wecom-admin-fixture", notification_channels=[channel()])
        self.handler = AsyncMock(return_value="已处理 & <测试>")
        commands = SimpleNamespace(handle=self.handler, close=AsyncMock())
        scheduler = PluginScheduler()
        self.addCleanup(scheduler.stop)
        runtime = SimpleNamespace(loaded={}, scan=lambda: [])
        with patch("awbotnest.wecom_commands.WeComCommandService", return_value=commands):
            self.app = create_app(self.settings, SimpleNamespace(), runtime, scheduler, PluginRoutes(),
                                  market=SimpleNamespace(clear_cache=lambda: None))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.path = f"/api/wecom/callback/{CHANNEL_ID}"

    async def post(self, message=None, *, ciphertext=None, params=None, body=None, outer=None, headers=None):
        ciphertext = ciphertext or encrypt_independent(xml(message if message is not None else incoming()))
        params = params if params is not None else signature(ciphertext)
        body = body if body is not None else xml({"ToUserName": CORP_ID, "Encrypt": ciphertext,
                                                  "AgentID": AGENT_ID, **(outer or {})})
        return await self.client.post(self.path, params=params, content=body,
                                      headers={"Content-Type": "application/xml", **(headers or {})})

    def reply(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        fields = {item.tag: item.text for item in ET.fromstring(response.content)}
        self.assertEqual(signature(fields["Encrypt"], timestamp=fields["TimeStamp"], nonce=fields["Nonce"])
                         ["msg_signature"], fields["MsgSignature"])
        message = {item.tag: item.text for item in ET.fromstring(decrypt_independent(fields["Encrypt"]))}
        self.assertEqual(message["ToUserName"], MEMBER)
        self.assertEqual(message["FromUserName"], CORP_ID)
        self.assertEqual(message["MsgType"], "text")
        return message

    async def test_get_verification_returns_independently_encrypted_challenge(self):
        ciphertext = encrypt_independent("verification challenge 中文")
        response = await self.client.get(self.path, params={**signature(ciphertext), "echostr": ciphertext})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, "verification challenge 中文")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.handler.assert_not_awaited()

    async def test_get_verification_rejects_wrong_signature_receiver_or_clock(self):
        for cipher, params in ((encrypt_independent("test"), {"msg_signature": "0" * 40}),
                               (encrypt_independent("test", corp_id="wwother"), {}),
                               (encrypt_independent("test"), {"timestamp": str(int(time.time()) - 601)}),
                               (encrypt_independent("test"), {"timestamp": str(int(time.time()) + 601)})):
            with self.subTest(params=params):
                signed = {**signature(cipher), **params, "echostr": cipher}
                if "timestamp" in params:
                    signed.update(signature(cipher, timestamp=params["timestamp"]))
                response = await self.client.get(self.path, params=signed)
                self.assertIn(response.status_code, {400, 403})
                self.assertNotIn(TOKEN, response.text)
                self.assertNotIn(AES_KEY, response.text)
        self.handler.assert_not_awaited()

    async def test_valid_text_is_authenticated_before_handler_and_reply_is_encrypted(self):
        message = incoming()
        response = await self.post(message)
        self.assertEqual(self.reply(response)["Content"], "已处理 & <测试>")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.handler.assert_awaited_once_with(CHANNEL_ID, message)

    async def test_nested_wechat_alias_and_flat_channel_have_same_authentication(self):
        config = channel()
        self.settings.notification_channels = [{"id": CHANNEL_ID, "type": "wechat", "enabled": True,
                                                "config": {key: value for key, value in config.items()
                                                           if key not in {"id", "name", "type", "enabled"}}}]
        response = await self.post()
        self.assertEqual(self.reply(response)["Content"], "已处理 & <测试>")
        self.handler.assert_awaited_once()

    async def test_disabled_non_app_and_missing_channels_are_not_open_endpoints(self):
        for config in (channel(enabled=False), channel(callback_enabled=False),
                       channel(callback_enabled="true"), channel(type="webhook"),
                       channel(url="https://example.test/robot"), channel(callback_token="")):
            with self.subTest(config=config):
                self.settings.notification_channels = [config]
                self.assertEqual((await self.post()).status_code, 404)
        self.settings.notification_channels = []
        self.assertEqual((await self.post()).status_code, 404)
        self.handler.assert_not_awaited()

    async def test_unauthorized_member_wrong_app_and_wrong_corporation_never_reach_handler(self):
        for message in (incoming(FromUserName="unauthorized"), incoming(ToUserName="wwother"),
                        incoming(AgentID="1000003"), incoming(FromUserName="@all")):
            with self.subTest(message=message):
                self.assertIn((await self.post(message)).status_code, {400, 403})
        self.assertEqual((await self.post(outer={"AgentID": "1000003"})).status_code, 403)
        cipher = encrypt_independent(xml(incoming()), corp_id="wwother")
        self.assertEqual((await self.post(ciphertext=cipher)).status_code, 403)
        self.handler.assert_not_awaited()

    async def test_bad_signature_padding_and_malformed_message_are_rejected_without_dispatch(self):
        cipher = encrypt_independent(xml(incoming()))
        self.assertEqual((await self.post(ciphertext=cipher,
                                         params={**signature(cipher), "msg_signature": "0" * 40})).status_code, 403)
        self.assertEqual((await self.post(ciphertext=encrypt_independent(xml(incoming()),
                                                                        invalid_padding=True))).status_code, 403)
        for change in ({"MsgId": ""}, {"MsgId": "not-numeric"}, {"CreateTime": "later"},
                       {"ToUserName": ""}, {"AgentID": ""}):
            with self.subTest(change=change):
                self.assertIn((await self.post(incoming(**change))).status_code, {400, 403})
        self.handler.assert_not_awaited()

    async def test_outer_and_inner_xml_entities_or_duplicate_fields_are_rejected(self):
        cipher = encrypt_independent(xml(incoming()))
        outer = [b'<!DOCTYPE xml [<!ENTITY x "injected">]><xml><Encrypt>&x;</Encrypt></xml>',
                 b"<xml><Encrypt>first</Encrypt><Encrypt>second</Encrypt></xml>"]
        for body in outer:
            with self.subTest(body=body):
                self.assertEqual((await self.post(ciphertext=cipher, body=body)).status_code, 400)
        valid = xml(incoming())
        invalid = valid.replace(b"</xml>", b"<FromUserName>other</FromUserName></xml>")
        self.assertEqual((await self.post(ciphertext=encrypt_independent(invalid))).status_code, 403)
        self.handler.assert_not_awaited()

    async def test_oversized_stream_or_false_content_length_never_dispatches(self):
        response = await self.post(body=b"x" * (64 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        response = await self.post(headers={"Content-Length": "not-numeric"})
        self.assertEqual(response.status_code, 400)
        async def chunks():
            yield b"x" * 40000
            yield b"x" * 40000
        cipher = encrypt_independent(xml(incoming()))
        response = await self.client.post(self.path, params=signature(cipher), content=chunks(),
                                          headers={"Content-Type": "application/xml"})
        self.assertEqual(response.status_code, 413)
        self.handler.assert_not_awaited()

    async def test_duplicate_query_parameters_are_not_accepted_as_one_signature(self):
        cipher = encrypt_independent(xml(incoming()))
        params = list(signature(cipher).items()) + [("timestamp", "1")]
        response = await self.post(ciphertext=cipher, params=params)
        self.assertEqual(response.status_code, 400)
        self.handler.assert_not_awaited()

    async def test_non_text_events_are_acknowledged_without_running_commands(self):
        response = await self.post(incoming(Content="/运行 dangerous action", MsgType="event"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "success")
        self.handler.assert_not_awaited()

    async def test_long_passive_reply_is_utf8_safe_and_bounded(self):
        self.handler.return_value = "中" * 3000 + "\x00"
        message = self.reply(await self.post())
        self.assertLessEqual(len(message["Content"].encode()), 2048)
        self.assertNotIn("\x00", message["Content"])

    async def test_callback_settings_mask_roundtrip_and_reveal_use_the_effective_flat_values(self):
        initial = channel(config={"callback_token": "nested-unused-token", "callback_aes_key": AES_KEY[::-1]})
        self.settings.notification_channels = [initial]
        auth = {"Authorization": "Bearer wecom-admin-fixture"}
        response = await self.client.get("/api/settings", headers=auth)
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()["settings"]["NOTIFICATION_CHANNELS"][0]
        self.assertEqual(view["config"]["callback_token"], "********")
        self.assertEqual(view["config"]["callback_aes_key"], "********")
        for secret in (TOKEN, AES_KEY, "nested-unused-token"):
            self.assertNotIn(secret, response.text)
        for field, expected in (("callback_token", TOKEN), ("callback_aes_key", AES_KEY)):
            response = await self.client.post("/api/settings/reveal-secret", headers=auth,
                                              json={"kind": "channel", "field": field, "id": CHANNEL_ID})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["value"], expected)
            self.assertEqual(response.headers["cache-control"], "no-store")
        for url, body in (("/api/settings/notification-channels", {"channels": [view]}),
                          ("/api/settings", {"notification_channels": [view]})):
            with self.subTest(url=url), patch("awbotnest.api.settings.save_settings"):
                response = await self.client.put(url, headers=auth, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            saved = self.settings.notification_channels[0]
            self.assertEqual(saved["callback_token"], TOKEN)
            self.assertEqual(saved["callback_aes_key"], AES_KEY)
            self.assertEqual(saved["callback_users"], MEMBER)

    async def test_callback_settings_reject_invalid_save_without_mutating_existing_channels(self):
        auth = {"Authorization": "Bearer wecom-admin-fixture"}
        before = json.loads(json.dumps(self.settings.notification_channels))
        for url, body in (("/api/settings/notification-channels", {"channels": [channel(callback_users="@all")]}),
                          ("/api/settings", {"notification_channels": [channel(callback_aes_key="invalid")]})):
            with self.subTest(url=url), patch("awbotnest.api.settings.save_settings") as save:
                response = await self.client.put(url, headers=auth, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                save.assert_not_called()
                self.assertEqual(self.settings.notification_channels, before)

    async def test_callback_secrets_cannot_be_revealed_without_administrator_authentication(self):
        response = await self.client.post("/api/settings/reveal-secret", json={
            "kind": "channel", "field": "callback_aes_key", "id": CHANNEL_ID})
        self.assertIn(response.status_code, {401, 403})
        self.assertNotIn(AES_KEY, response.text)

    async def test_encrypted_http_command_ack_queue_private_result_and_retry_work_end_to_end(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        notifier = SimpleNamespace(send_wecom_text=AsyncMock(return_value={"errcode": 0}))
        runtime = SimpleNamespace(loaded={"allowed": object()}, display_name=lambda pid: "允许插件",
                                  notifier=notifier)
        self.settings.bot_routing = {"allowed": CHANNEL_ID}
        routes = PluginRoutes()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(payload):
            entered.set()
            await release.wait()
            return {"ok": True, "message": "后台完成"}
        action = AsyncMock(side_effect=slow)
        routes.action("allowed", "check", action)
        service = WeComCommandService(self.settings, runtime, routes,
                                      store_path=Path(temporary.name) / "received.sqlite")
        self.addAsyncCleanup(service.close)
        self.app.state.wecom_commands = service
        message = incoming('/run allowed check {"dry_run":true}', MsgId="9000")
        try:
            response = await asyncio.wait_for(self.post(message), 1.5)
            self.assertIn("已提交", self.reply(response)["Content"])
            await asyncio.wait_for(entered.wait(), 1)
            notifier.send_wecom_text.assert_not_awaited()
        finally:
            release.set()
        await asyncio.wait_for(service._queue.join(), 2)
        self.assertIn("已接收", self.reply(await self.post(message))["Content"])
        action.assert_awaited_once_with({"dry_run": True})
        self.assertEqual(notifier.send_wecom_text.await_count, 1)
        self.assertEqual(notifier.send_wecom_text.await_args.args[:2], (CHANNEL_ID, MEMBER))

    async def test_asgi_lifespan_closes_callback_background_service(self):
        commands = self.app.state.wecom_commands
        async with self.app.router.lifespan_context(self.app):
            commands.close.assert_not_awaited()
        commands.close.assert_awaited_once()


class WeComCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store_path = Path(self.temporary.name) / "received.sqlite"
        self.settings = Settings(notification_channels=[channel()], bot_routing={
            "allowed": CHANNEL_ID, "foreign": "another-channel", "disabled": CHANNEL_ID,
            "web_only": CHANNEL_ID})
        names = {"allowed": "允许插件", "foreign": "其他插件", "disabled": "停用插件", "web_only": "接口插件"}
        self.notifier = SimpleNamespace(send_wecom_text=AsyncMock(return_value={"errcode": 0}))
        self.runtime = SimpleNamespace(loaded={"allowed": object(), "foreign": object(), "web_only": object()},
                                       display_name=lambda pid: names.get(pid, pid),
                                       notifier=self.notifier, accounts=SimpleNamespace(users={}))
        self.routes = PluginRoutes()
        self.action = AsyncMock(return_value={"ok": True, "message": "执行成功"})
        self.foreign = AsyncMock(return_value=True)
        self.webhook = AsyncMock(return_value=True)
        self.routes.action("allowed", "check", self.action)
        self.routes.action("foreign", "check", self.foreign)
        self.routes.action("disabled", "check", self.foreign)
        self.routes.webhook("web_only", "hook", self.webhook)
        self.routes.api("web_only", "api", self.webhook)
        self.service = WeComCommandService(self.settings, self.runtime, self.routes, store_path=self.store_path)
        self.addAsyncCleanup(self.service.close)

    async def idle(self):
        await asyncio.wait_for(self.service._queue.join(), 2)

    async def test_queries_only_show_loaded_plugins_associated_with_this_channel(self):
        listing = await self.service.handle(CHANNEL_ID, incoming("/插件"))
        self.assertIn("允许插件", listing)
        self.assertNotIn("其他插件", listing)
        self.assertNotIn("停用插件", listing)
        self.assertIn("check", listing)
        self.assertIn("/运行", await self.service.handle(CHANNEL_ID, incoming("/help")))
        self.assertIn("AWBotNest", await self.service.handle(CHANNEL_ID, incoming("/状态")))
        self.assertIn("check", await self.service.handle(CHANNEL_ID, incoming("/动作 allowed")))
        self.action.assert_not_awaited()
        self.assertFalse(self.store_path.exists())

    async def test_action_ack_does_not_wait_for_execution_and_result_is_sent_only_to_original_member(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(payload):
            entered.set()
            await release.wait()
            return {"ok": True, "message": "后台执行完成"}
        self.routes.action("allowed", "check", slow)
        try:
            result = await asyncio.wait_for(self.service.handle(CHANNEL_ID,
                                           incoming('/运行 allowed check {"dry_run": true}')), 1.5)
            self.assertIn("已提交", result)
            await asyncio.wait_for(entered.wait(), 1)
            self.notifier.send_wecom_text.assert_not_awaited()
        finally:
            release.set()
        await self.idle()
        self.notifier.send_wecom_text.assert_awaited_once()
        recipient = self.notifier.send_wecom_text.await_args.args
        self.assertEqual(recipient[:2], (CHANNEL_ID, MEMBER))
        self.assertIn("后台执行完成", recipient[2])

    async def test_unauthorized_or_disabled_channel_members_cannot_execute_actions(self):
        from fastapi import HTTPException
        for message in (incoming("/run allowed check", FromUserName="other"),
                        incoming("/run allowed check", FromUserName="@all")):
            with self.subTest(message=message), self.assertRaises(HTTPException) as raised:
                await self.service.handle(CHANNEL_ID, message)
            self.assertEqual(raised.exception.status_code, 403)
        self.settings.notification_channels[0]["callback_enabled"] = False
        with self.assertRaises(HTTPException) as raised:
            await self.service.handle(CHANNEL_ID, incoming("/run allowed check"))
        self.assertEqual(raised.exception.status_code, 403)
        self.action.assert_not_awaited()
        self.assertFalse(self.store_path.exists())

    async def test_webhook_api_unloaded_unassociated_and_unknown_actions_are_not_command_capabilities(self):
        for command in ("/run foreign check", "/run disabled check", "/run web_only hook",
                        "/run web_only api", "/run allowed missing", "/run allowed ../check",
                        "/shell whoami", "/reload allowed"):
            with self.subTest(command=command):
                result = await self.service.handle(CHANNEL_ID, incoming(command))
                self.assertNotIn("已提交", result)
        self.action.assert_not_awaited()
        self.foreign.assert_not_awaited()
        self.webhook.assert_not_awaited()
        self.assertFalse(self.store_path.exists())

    async def test_action_payload_must_be_a_small_json_object(self):
        for payload in ("[]", "null", "1", "bad-json", '{"value":"' + "x" * 8200 + '"}',
                        "[" * 1200 + "]" * 1200):
            with self.subTest(payload=payload[:30]):
                result = await self.service.handle(CHANNEL_ID, incoming(f"/run allowed check {payload}"))
                self.assertNotIn("已提交", result)
        self.assertIn("已提交", await self.service.handle(CHANNEL_ID,
                      incoming('/run allowed check {"dry_run":true}')))
        await self.idle()
        self.action.assert_awaited_once_with({"dry_run": True})

    async def test_concurrent_and_restart_retries_execute_a_message_only_once(self):
        message = incoming("/run allowed check")
        responses = await asyncio.gather(self.service.handle(CHANNEL_ID, message),
                                         self.service.handle(CHANNEL_ID, message))
        self.assertEqual(sum("已提交" in text for text in responses), 1)
        self.assertEqual(sum("已接收" in text for text in responses), 1)
        await self.idle()
        await self.service.close()
        restarted = WeComCommandService(self.settings, self.runtime, self.routes, store_path=self.store_path)
        self.addAsyncCleanup(restarted.close)
        self.assertIn("已接收", await restarted.handle(CHANNEL_ID, message))
        await asyncio.wait_for(restarted._queue.join(), 1)
        self.action.assert_awaited_once()
        database = self.store_path.read_bytes()
        for private in (TOKEN, AES_KEY, MEMBER, message["Content"]):
            self.assertNotIn(private.encode(), database)

    async def test_same_message_id_from_different_channels_is_not_a_false_duplicate(self):
        second = "second-app"
        self.settings.notification_channels.append(channel(id=second))
        self.settings.bot_routing["allowed"] = f"{CHANNEL_ID},{second}"
        for selected in (CHANNEL_ID, second):
            self.assertIn("已提交", await self.service.handle(selected, incoming("/run allowed check")))
        await self.idle()
        self.assertEqual(self.action.await_count, 2)
        self.assertEqual({call.args[0] for call in self.notifier.send_wecom_text.await_args_list},
                         {CHANNEL_ID, second})

    async def test_queued_actions_and_late_results_recheck_revoked_permissions(self):
        for index, revoke in enumerate(("channel", "member", "routing", "loaded", "action", "application")):
            with self.subTest(revoke=revoke):
                self.settings.notification_channels = [channel()]
                self.settings.bot_routing["allowed"] = CHANNEL_ID
                self.runtime.loaded["allowed"] = object()
                self.routes.action("allowed", "check", self.action)
                entered, release = asyncio.Event(), asyncio.Event()
                async def block(payload):
                    entered.set()
                    await release.wait()
                self.routes.action("allowed", "block", block)
                await self.service.handle(CHANNEL_ID, incoming("/run allowed block", MsgId=str(2000 + index * 2)))
                await asyncio.wait_for(entered.wait(), 1)
                await self.service.handle(CHANNEL_ID, incoming("/run allowed check", MsgId=str(2001 + index * 2)))
                if revoke == "channel":
                    self.settings.notification_channels[0]["callback_enabled"] = False
                elif revoke == "member":
                    self.settings.notification_channels[0]["callback_users"] = "other-member"
                elif revoke == "routing":
                    self.settings.bot_routing["allowed"] = "other-channel"
                elif revoke == "loaded":
                    self.runtime.loaded.pop("allowed")
                elif revoke == "application":
                    self.settings.notification_channels[0].update(corpid="wwdifferentcorporation", agentid="1000003")
                else:
                    self.routes.remove_plugin("allowed")
                release.set()
                await self.idle()
                self.action.assert_not_awaited()
                self.notifier.send_wecom_text.assert_not_awaited()

    async def test_bounded_queue_and_idempotent_shutdown_cancel_inflight_and_pending_work(self):
        from fastapi import HTTPException
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def block(payload):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.routes.action("allowed", "check", block)
        await self.service.handle(CHANNEL_ID, incoming("/run allowed check", MsgId="3000"))
        await asyncio.wait_for(entered.wait(), 1)
        for index in range(16):
            await self.service.handle(CHANNEL_ID, incoming("/run allowed check", MsgId=str(3001 + index)))
        with self.assertRaises(HTTPException) as raised:
            await self.service.handle(CHANNEL_ID, incoming("/run allowed check", MsgId="4000"))
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.service._queue.qsize(), 16)
        await asyncio.wait_for(self.service.close(), 1)
        await self.service.close()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.service._worker.done())
        self.assertTrue(self.service._queue.empty())
        await self.idle()
        self.notifier.send_wecom_text.assert_not_awaited()
        with self.assertRaises(HTTPException) as raised:
            await self.service.handle(CHANNEL_ID, incoming("/run allowed check", MsgId="5000"))
        self.assertEqual(raised.exception.status_code, 503)

    async def test_unavailable_dedup_storage_never_starts_the_plugin(self):
        from fastapi import HTTPException
        with patch.object(self.service, "_claim", side_effect=OSError("fixture readonly storage")):
            with self.assertRaises(HTTPException) as raised:
                await self.service.handle(CHANNEL_ID, incoming("/run allowed check"))
        self.assertEqual(raised.exception.status_code, 503)
        self.action.assert_not_awaited()
        self.notifier.send_wecom_text.assert_not_awaited()

    async def test_request_cancelled_during_committed_admission_does_not_lose_or_duplicate_action(self):
        entered, release = threading.Event(), threading.Event()
        original_claim = self.service._claim
        def claim(channel_id, message_id):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Fixture admission did not resume")
            return original_claim(channel_id, message_id)
        message = incoming("/run allowed check", MsgId="6000")
        with patch.object(self.service, "_claim", side_effect=claim):
            task = asyncio.create_task(self.service.handle(CHANNEL_ID, message))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        await self.idle()
        self.assertIn("已接收", await self.service.handle(CHANNEL_ID, message))
        await self.idle()
        self.action.assert_awaited_once()
        self.notifier.send_wecom_text.assert_awaited_once()

    async def test_plugin_permission_revoked_during_real_access_token_request_blocks_result_delivery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def token_request(*args, **kwargs):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"access_token": "fixture-access-token"},
                                  request=httpx.Request("GET", "https://qyapi.weixin.qq.com"))
        http = SimpleNamespace(get=AsyncMock(side_effect=token_request), post=AsyncMock())
        self.runtime.notifier = NotificationService(self.settings, SimpleNamespace(), http)
        await self.service.handle(CHANNEL_ID, incoming("/run allowed check", MsgId="7000"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.settings.bot_routing["allowed"] = "another-channel"
        finally:
            release.set()
        await self.idle()
        self.action.assert_awaited_once()
        http.get.assert_awaited_once()
        http.post.assert_not_awaited()


class WeComDirectDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(notification_channels=[channel(touser="@all"),
                                 {"id": "backup", "type": "webhook", "url": "https://example.test/hook"}])
        self.http = SimpleNamespace(
            get=AsyncMock(return_value=httpx.Response(200, json={"access_token": "fixture-access-token"},
                                                     request=httpx.Request("GET", "https://qyapi.weixin.qq.com"))),
            post=AsyncMock(return_value=httpx.Response(200, json={"errcode": 0},
                                                      request=httpx.Request("POST", "https://qyapi.weixin.qq.com"))))
        self.notifier = NotificationService(self.settings, SimpleNamespace(), self.http)

    async def test_command_result_targets_original_member_not_configured_broadcast_group(self):
        with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock) as telegram:
            result = await self.notifier.send_wecom_text(CHANNEL_ID, MEMBER, "完成")
        self.assertEqual(result, {"errcode": 0})
        self.http.get.assert_awaited_once()
        self.http.post.assert_awaited_once()
        request = self.http.post.await_args
        self.assertEqual(request.args[0], "https://qyapi.weixin.qq.com/cgi-bin/message/send")
        self.assertEqual(request.kwargs["json"]["touser"], MEMBER)
        self.assertEqual(request.kwargs["json"]["agentid"], int(AGENT_ID))
        self.assertEqual(request.kwargs["json"]["text"]["content"], "完成")
        telegram.assert_not_awaited()

    async def test_delivery_failure_never_uses_telegram_other_channels_or_webhooks(self):
        for body in ({"errcode": 40013}, {"errcode": 0, "invaliduser": MEMBER}, {"unexpected": True}):
            with self.subTest(body=body):
                self.http.get.reset_mock()
                self.http.post.reset_mock()
                self.http.post.return_value = httpx.Response(200, json=body,
                                       request=httpx.Request("POST", "https://qyapi.weixin.qq.com"))
                with patch("awbotnest.notifier.send_rich", new_callable=AsyncMock) as telegram:
                    with self.assertRaises(RuntimeError):
                        await self.notifier.send_wecom_text(CHANNEL_ID, MEMBER, "完成")
                    telegram.assert_not_awaited()
                self.assertEqual(self.http.post.await_count, 1)
                self.assertNotIn("example.test", self.http.post.await_args.args[0])

    async def test_unauthorized_or_disabled_recipients_never_receive_an_http_request(self):
        for selected, member in ((CHANNEL_ID, "unauthorized"), (CHANNEL_ID, "@all"), ("missing", MEMBER)):
            with self.subTest(selected=selected, member=member), self.assertRaises((PermissionError, RuntimeError)):
                await self.notifier.send_wecom_text(selected, member, "完成")
        self.settings.notification_channels[0]["callback_enabled"] = False
        with self.assertRaises(RuntimeError):
            await self.notifier.send_wecom_text(CHANNEL_ID, MEMBER, "完成")
        self.http.get.assert_not_awaited()
        self.http.post.assert_not_awaited()

    async def test_access_token_wait_rechecks_callback_members_and_original_application_before_sending(self):
        for change in ({"callback_enabled": False}, {"callback_users": "another-member"},
                       {"corpid": "wwdifferentcorporation"}, {"agentid": "1000003"}):
            with self.subTest(change=change):
                self.settings.notification_channels[0] = channel(touser="@all")
                self.http.get.reset_mock()
                self.http.post.reset_mock()
                entered, release = asyncio.Event(), asyncio.Event()
                async def token_request(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return httpx.Response(200, json={"access_token": "fixture-access-token"},
                                          request=httpx.Request("GET", "https://qyapi.weixin.qq.com"))
                self.http.get.side_effect = token_request
                task = asyncio.create_task(self.notifier.send_wecom_text(CHANNEL_ID, MEMBER, "完成"))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.settings.notification_channels[0].update(change)
                    release.set()
                    with self.assertRaises((PermissionError, RuntimeError)):
                        await asyncio.wait_for(task, 1)
                    self.http.post.assert_not_awaited()
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
