import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telethon import types
from telethon.errors import PremiumAccountRequiredError
from awbotnest.rich_delivery import send_rich, DeliveryUncertain


class RichDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *, premium=False, bot=False):
        client = AsyncMock(return_value='native')
        client.get_me.return_value = SimpleNamespace(premium=premium, bot=bot)
        client.get_input_entity.return_value = types.InputPeerSelf()
        client.send_message.return_value = 'plain'
        return client

    async def test_premium_user_sends_html_table_as_same_user(self):
        client = self.client(premium=True)
        html = '<table><tr><td>签到成功</td></tr></table>'
        self.assertEqual(await send_rich(client, 'me', html, '签到成功'), 'native')
        request = client.call_args.args[0]
        self.assertIsInstance(request.rich_message, types.InputRichMessageHTML)
        self.assertEqual(request.rich_message.html, html)
        self.assertIsInstance(request.peer, types.InputPeerSelf)
        self.assertTrue(bytes(request))  # Verify real Telethon serialization, not mocked TL types.
        client.send_message.assert_not_awaited()

    async def test_nonpremium_user_falls_back(self):
        client = self.client()
        self.assertEqual(await send_rich(client, 'me', '<table/>', 'plain'), 'plain')
        client.assert_not_awaited()
        client.send_message.assert_awaited_once_with('me', 'plain', parse_mode=None)

    async def test_bot_session_does_not_require_premium(self):
        client = self.client(bot=True)
        self.assertEqual(await send_rich(client, 123, '<table/>', 'plain'), 'native')
        self.assertIsInstance(client.call_args.args[0].rich_message, types.InputRichMessageHTML)

    async def test_expired_premium_rejection_falls_back_once(self):
        client = self.client(premium=True)
        client.side_effect = PremiumAccountRequiredError(request=None)
        await send_rich(client, 'me', '<table/>', 'plain')
        client.send_message.assert_awaited_once()

    async def test_unknown_delivery_does_not_send_duplicate(self):
        client = self.client(premium=True)
        client.side_effect = TimeoutError('uncertain')
        with self.assertRaises(TimeoutError):
            await send_rich(client, 'me', '<table/>', 'plain')
        client.send_message.assert_not_awaited()

    async def test_old_library_falls_back(self):
        client = self.client(premium=True)
        with patch.object(types, 'InputRichMessageHTML', None):
            await send_rich(client, 'me', '<table/>', 'plain')
        client.assert_not_awaited()
        client.send_message.assert_awaited_once()

    async def test_bot_token_uses_bot_api_without_user_account(self):
        client = self.client()
        requests = []
        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
        with patch('awbotnest.rich_delivery.httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(handler)):
            result = await send_rich(client, 123, '<table/>', 'plain', token='test-token')
        self.assertEqual(result, {'message_id': 1})
        self.assertEqual(requests, [{'chat_id': 123, 'rich_message': {'html': '<table/>'}}])
        client.get_me.assert_not_awaited()
        client.send_message.assert_not_awaited()
