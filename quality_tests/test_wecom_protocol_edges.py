"""Regression fixtures from WeCom's documented protocol, without live accounts.

Official references: /document/path/90968 (crypto), 90240 (events),
90196 (case-insensitive UserID).
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI

from awbotnest.api.settings import create_router as settings_router
from awbotnest.api.wecom import create_router as callback_router
from awbotnest.auth import admin_dependency
from awbotnest.config import Settings, validate_config_format
from awbotnest.logs import redact_secrets
from awbotnest.notifier import NotificationService
from awbotnest.wecom_crypto import WeComCrypto, WeComCryptoError, parse_xml_fields


KEY_BYTES = bytes(range(32))
AES_KEY = base64.b64encode(KEY_BYTES).decode().rstrip("=")
CORP = "wwprotocolfixture"
AGENT = "1000001"
CHANNEL = "protocol-fixture"
TOKEN = "protocol-fixture-token"
ADMIN = "protocol-fixture-admin"

# This EncodingAESKey and ciphertext are verbatim from the official 90968
# example. Its last Base64 character is intentionally not canonical padding.
OFFICIAL_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
OFFICIAL_CIPHERTEXT = (
    "RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo+rtK1I9qca6aM/wvqnLSV5zEPeusUiX5L5X/0lWfrf0QADHHhGd3QczcdCUpj911L3vg3W/"
    "sYYvuJTs3TUUkSUXxaccAS0qhxchrRYt66wiSpGLYL42aM6A8dTT+6k4aSknmPj48kzJs8qLjvd4Xgpue06DOdnLxAUHzM6+kDZ+HMZfJYuR+"
    "LtwGc2hgf5gsijff0ekUNXZiqATP7PF5mZxZ3Izoun1s4zG4LUMnvw2r+KqCKIw+3IQH03v+BCA9nMELNqbSf6tiWSrXJB3LAVGUcallcrw8V2t9EL4"
    "EhzJWrQUax5wLVMNS0+rUPA3k22Ncx4XXZS9o0MBH27Bo6BpNelZpS+/uh9KsNlY6bHCmJU9p8g7m3fVKn28H3KDYA5Pl/T8Z1ptDAVe0lXdQ2YoyyH2"
    "uyPIGHBZZIs2pDBS8R07+qN+E7Q=="
)


def channel(**changes):
    return {"id": CHANNEL, "type": "wecom", "enabled": True, "corpid": CORP,
            "agentid": AGENT, "secret": "fixture-app-secret", "callback_enabled": True,
            "callback_token": TOKEN, "callback_aes_key": AES_KEY,
            "callback_users": "Alice.Example", **changes}


def message_xml(*, msg_type="text", sender="Alice.Example", agent=AGENT, details=""):
    root = ET.Element("xml")
    fields = {"ToUserName": CORP, "FromUserName": sender, "CreateTime": str(int(time.time())),
              "MsgType": msg_type}
    if agent is not None:
        fields["AgentID"] = agent
    if msg_type == "text":
        fields.update(Content="/帮助", MsgId="80001")
    else:
        fields["Event"] = "scancode_push"
    for name, value in fields.items():
        ET.SubElement(root, name).text = value
    return ET.tostring(root, encoding="utf-8").replace(b"</xml>", details.encode() + b"</xml>")


def encrypted_request(payload: bytes, *, corp=CORP):
    frame = b"0123456789ABCDEF" + struct.pack("!I", len(payload)) + payload + corp.encode()
    padding = 32 - len(frame) % 32
    frame += bytes((padding,)) * padding
    worker = Cipher(algorithms.AES(KEY_BYTES), modes.CBC(KEY_BYTES[:16])).encryptor()
    ciphertext = base64.b64encode(worker.update(frame) + worker.finalize()).decode()
    timestamp, nonce = str(int(time.time())), "protocol-nonce"
    signature = hashlib.sha1("".join(sorted((TOKEN, timestamp, nonce, ciphertext))).encode()).hexdigest()
    envelope = ET.Element("xml")
    for name, value in {"ToUserName": CORP, "AgentID": AGENT, "Encrypt": ciphertext}.items():
        ET.SubElement(envelope, name).text = value
    return ET.tostring(envelope, encoding="utf-8"), {
        "timestamp": timestamp, "nonce": nonce, "msg_signature": signature,
    }


class OfficialCryptoFixtureTests(unittest.TestCase):
    def test_official_random_43_character_key_and_published_ciphertext(self):
        crypto = WeComCrypto("QDG6eK", OFFICIAL_KEY, "wx5823bf96d3bd56c7")
        crypto.verify_signature("477715d11cdb4164915debcba66cb864d751f3e6", "1409659813",
                                "1372623149", OFFICIAL_CIPHERTEXT)
        fields = parse_xml_fields(crypto.decrypt(OFFICIAL_CIPHERTEXT))
        self.assertEqual(fields["Content"], "hello")
        self.assertEqual(fields["FromUserName"], "mycreate")
        self.assertEqual(fields["AgentID"], "218")

    def test_official_key_is_valid_for_save_and_import(self):
        validate_config_format({"notification_channels": [channel(callback_aes_key=OFFICIAL_KEY)]})

    def test_plain_flat_parser_stays_strict(self):
        with self.assertRaises(WeComCryptoError):
            parse_xml_fields(b"<xml><Content><nested>/run plugin action</nested></Content></xml>")

    def test_wecom_sensitive_query_and_official_config_names_are_redacted(self):
        source = ("HTTP failure for https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=wwpublic"
                  "&corpsecret=secret%2Bfixture&access_token=token-fixture&limit=5 "
                  "CallbackToken=callback-fixture EncodingAESKey=key-fixture "
                  "callback_token=snake-token callback_aes_key=snake-key")
        redacted = redact_secrets(source)
        for private in ("secret%2Bfixture", "token-fixture", "callback-fixture", "key-fixture", "snake-token", "snake-key"):
            self.assertNotIn(private, redacted)
        for public in ("corpid=wwpublic", "limit=5"):
            self.assertIn(public, redacted)
        for field in ("corpsecret", "access_token", "CallbackToken", "EncodingAESKey", "callback_token", "callback_aes_key"):
            self.assertIn(f"{field}=***", redacted)

    def test_secret_name_detection_does_not_mask_similarly_named_status_fields(self):
        source = ("corpsecret_status=ready corpsecretary=member token_usage=12 secret_count=3 "
                  "callbacktoken_enabled=false encodingaeskey_valid=true")
        self.assertEqual(redact_secrets(source), source)


class CallbackProtocolEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(admin_token=ADMIN, notification_channels=[channel()])
        self.handler = AsyncMock(return_value="已处理")
        self.app = FastAPI()
        self.app.state.wecom_commands = SimpleNamespace(handle=self.handler)
        deps = SimpleNamespace(settings=self.settings, accounts=SimpleNamespace(), runtime=SimpleNamespace(),
                               scheduler=SimpleNamespace(), routes=SimpleNamespace(), restart_event=None,
                               market=SimpleNamespace(clear_cache=lambda: None),
                               require_admin=admin_dependency(self.settings), started_at=time.monotonic(),
                               resource_sampler=SimpleNamespace())
        self.app.include_router(callback_router(deps))
        self.app.include_router(settings_router(deps))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="http://fixture")
        self.addAsyncCleanup(self.client.aclose)
        self.path = f"/api/wecom/callback/{CHANNEL}"
        self.auth = {"Authorization": f"Bearer {ADMIN}"}

    async def post(self, payload, **changes):
        body, query = encrypted_request(payload, **changes)
        return await self.client.post(self.path, content=body, params=query)

    async def test_official_nested_menu_and_card_events_ack_without_commands(self):
        details = (
            "<ScanCodeInfo><ScanType>qrcode</ScanType><ScanResult>/运行 plugin action</ScanResult></ScanCodeInfo>",
            "<SendPicsInfo><Count>2</Count><PicList><item><PicMd5Sum>abc</PicMd5Sum></item>"
            "<item><PicMd5Sum>def</PicMd5Sum></item></PicList></SendPicsInfo>",
            "<SendLocationInfo><Location_X>23</Location_X><Location_Y>113</Location_Y>"
            "<Scale>15</Scale><Label>广州</Label><Poiname /></SendLocationInfo>",
            "<SelectedItems><SelectedItem><QuestionKey>one</QuestionKey><OptionIds>"
            "<OptionId>1</OptionId><OptionId>2</OptionId></OptionIds></SelectedItem>"
            "<SelectedItem><QuestionKey>two</QuestionKey><OptionIds><OptionId>3</OptionId>"
            "</OptionIds></SelectedItem></SelectedItems>",
            "<ApprovalInfo><ApprovalNodes><ApprovalNode><Items><Item><ItemName>张三</ItemName>"
            "</Item></Items></ApprovalNode></ApprovalNodes></ApprovalInfo>",
        )
        for detail in details:
            with self.subTest(detail=detail):
                response = await self.post(message_xml(msg_type="event", details=detail))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.text, "success")
        self.handler.assert_not_awaited()

    async def test_official_batch_job_event_without_agent_is_acknowledged(self):
        detail = "<BatchJob><JobId>job1</JobId><JobType>sync_user</JobType><ErrCode>0</ErrCode><ErrMsg>ok</ErrMsg></BatchJob>"
        response = await self.post(message_xml(msg_type="event", agent=None, sender="sys", details=detail))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, "success")
        self.handler.assert_not_awaited()

    async def test_non_text_events_still_reject_wrong_application_or_enterprise(self):
        for payload, changes in ((message_xml(msg_type="event", agent="1000002"), {}),
                                 (message_xml(msg_type="event", agent=None), {"corp": "wwother"}),
                                 (message_xml(msg_type="event").replace(CORP.encode(), b"wwother"), {})):
            with self.subTest(changes=changes):
                self.assertEqual((await self.post(payload, **changes)).status_code, 403)
        self.handler.assert_not_awaited()

    async def test_nested_text_and_nested_identity_never_reach_commands(self):
        payloads = (
            message_xml(details="<Extra><Value>nested</Value></Extra>"),
            message_xml(msg_type="event").replace(b"<AgentID>1000001</AgentID>", b"<AgentID><value>1000001</value></AgentID>"),
            message_xml(msg_type="event", details="<FromUserName>second</FromUserName>"),
            message_xml(msg_type="event", details="<MsgType>text</MsgType>"),
            b'<!DOCTYPE xml [<!ENTITY x "secret">]>' + message_xml(msg_type="event", details="<Extra>&x;</Extra>"),
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual((await self.post(payload)).status_code, 403)
        self.handler.assert_not_awaited()

    async def test_text_without_agent_cannot_execute(self):
        self.assertEqual((await self.post(message_xml(agent=None))).status_code, 403)
        self.handler.assert_not_awaited()

    async def test_case_insensitive_userid_matching_preserves_the_sender(self):
        self.settings.notification_channels[0]["callback_users"] = "alice.example"
        response = await self.post(message_xml(sender="ALICE.Example"))
        self.assertEqual(response.status_code, 200, response.text)
        self.handler.assert_awaited_once()
        self.assertEqual(self.handler.await_args.args[1]["FromUserName"], "ALICE.Example")

    async def test_other_case_insensitive_userid_is_not_authorized(self):
        self.assertEqual((await self.post(message_xml(sender="BOB.Example"))).status_code, 403)
        self.handler.assert_not_awaited()

    async def test_official_key_survives_masked_settings_roundtrip(self):
        self.settings.notification_channels = [channel(callback_aes_key=OFFICIAL_KEY)]
        response = await self.client.get("/api/settings", headers=self.auth)
        saved_view = response.json()["settings"]["NOTIFICATION_CHANNELS"]
        self.assertEqual(saved_view[0]["config"]["callback_aes_key"], "********")
        self.assertNotIn(OFFICIAL_KEY, response.text)
        for url, key in (("/api/settings", "notification_channels"),
                         ("/api/settings/notification-channels", "channels")):
            with self.subTest(url=url), patch("awbotnest.api.settings.save_settings"):
                response = await self.client.put(url, json={key: saved_view}, headers=self.auth)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.settings.notification_channels[0]["callback_aes_key"], OFFICIAL_KEY)

    async def test_token_http_json_and_network_failures_do_not_expose_credentials(self):
        private = "fixture-sensitive-app-secret"
        self.settings.notification_channels[0]["secret"] = private
        request = httpx.Request("GET", "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                                params={"corpid": CORP, "corpsecret": private})
        bad_json = httpx.Response(200, request=request)
        # Exercise a JSON parser/proxy error whose text includes private data,
        # rather than depending on the default parser's generic English error.
        bad_json.json = lambda: (_ for _ in ()).throw(json.JSONDecodeError(private, "", 0))
        failures = (httpx.Response(500, text="upstream failed", request=request), bad_json,
                    httpx.ConnectError(f"cannot reach URL with corpsecret={private}", request=request))
        for failure in failures:
            with self.subTest(kind=type(failure).__name__):
                http = SimpleNamespace(get=AsyncMock(), post=AsyncMock())
                if isinstance(failure, Exception):
                    http.get.side_effect = failure
                else:
                    http.get.return_value = failure
                service = NotificationService(self.settings, SimpleNamespace(), http)
                with self.assertRaises(RuntimeError) as caught:
                    await service.send_wecom_text(CHANNEL, "Alice.Example", "完成")
                self.assertEqual(str(caught.exception), "企业微信获取令牌失败")
                self.assertNotIn(private, str(caught.exception))
                http.post.assert_not_awaited()

    async def test_rejected_token_response_cannot_send_even_with_an_access_token(self):
        http = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={
            "errcode": 40013, "errmsg": "corpsecret=do-not-return", "access_token": "untrusted-token"},
            request=httpx.Request("GET", "https://qyapi.weixin.qq.com/cgi-bin/gettoken"))),
            post=AsyncMock())
        service = NotificationService(self.settings, SimpleNamespace(), http)
        with self.assertRaises(RuntimeError) as caught:
            await service.send_wecom_text(CHANNEL, "Alice.Example", "完成")
        self.assertEqual(str(caught.exception), "企业微信获取令牌失败")
        http.post.assert_not_awaited()

    async def test_missing_or_wrong_typed_token_acknowledgement_is_not_success(self):
        for ack in ({}, {"errcode": False}, {"errcode": True}, {"errcode": "0"}, {"errcode": None},
                    {"errcode": 0.0}, {"errcode": 40013}):
            with self.subTest(ack=ack):
                http = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200,
                    json={"access_token": "not-confirmed-token", **ack},
                    request=httpx.Request("GET", "https://qyapi.weixin.qq.com/cgi-bin/gettoken"))), post=AsyncMock())
                service = NotificationService(self.settings, SimpleNamespace(), http)
                with self.assertRaises(RuntimeError) as caught:
                    await service.send_wecom_text(CHANNEL, "Alice.Example", "完成")
                self.assertEqual(str(caught.exception), "企业微信获取令牌失败")
                http.post.assert_not_awaited()

    async def test_callback_channel_ids_and_enabled_types_cannot_save_unusable_callbacks(self):
        for change in ({"id": "channel with spaces"}, {"id": "中文渠道"}, {"id": "x" * 129},
                       {"id": ""}, {"enabled": "true"}, {"enabled": 1}):
            for url, key in (("/api/settings", "notification_channels"),
                             ("/api/settings/notification-channels", "channels")):
                with self.subTest(change=change, url=url), patch("awbotnest.api.settings.save_settings") as save:
                    response = await self.client.put(url, json={key: [channel(**change)]}, headers=self.auth)
                    self.assertEqual(response.status_code, 400, response.text)
                    save.assert_not_called()
                    self.assertEqual(self.settings.notification_channels, [channel()])

    async def test_duplicate_callback_id_cannot_shadow_an_enabled_application(self):
        for first in (channel(), {"id": CHANNEL, "type": "webhook", "enabled": True, "url": "https://example.test"}):
            for url, key in (("/api/settings", "notification_channels"),
                             ("/api/settings/notification-channels", "channels")):
                with self.subTest(url=url, first=first), patch("awbotnest.api.settings.save_settings") as save:
                    response = await self.client.put(url, json={key: [first, channel()]}, headers=self.auth)
                    self.assertEqual(response.status_code, 400, response.text)
                    save.assert_not_called()

    def test_unusable_callback_channels_are_rejected_in_backup_validation(self):
        invalid = ([channel(id="spaces are invalid")], [channel(enabled="true")], [channel(), channel()],
                   [{"type": "wecom", "enabled": True, "config": channel()}])
        for channels in invalid:
            with self.subTest(channels=channels), self.assertRaises(ValueError):
                validate_config_format({"notification_channels": channels})

    def test_callback_identity_collision_with_a_legacy_channel_is_rejected_in_backups(self):
        with self.assertRaises(ValueError):
            validate_config_format({"notification_channels": [
                {"id": CHANNEL, "type": "webhook"}, channel()]})

    def test_legacy_non_callback_notification_configuration_stays_compatible(self):
        validate_config_format({"notification_channels": [{"type": "wecom", "config": {"corpid": "oldcorp"}}]})

    def test_member_count_deduplicates_case_insensitive_userids(self):
        users = [f"member{index}" for index in range(64)]
        validate_config_format({"notification_channels": [channel(callback_users="|".join(users + ["MEMBER0"]))]})
        with self.assertRaises(ValueError):
            validate_config_format({"notification_channels": [channel(callback_users="|".join(users + ["member64"]))]})


if __name__ == "__main__":
    unittest.main()
