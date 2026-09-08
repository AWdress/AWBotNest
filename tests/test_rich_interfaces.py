import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import types
from awbotnest.notification_text import content
from awbotnest.rich_text import build_rich_table
from awbotnest.rich_delivery import install_rich_methods


class RichInterfaces(unittest.IsolatedAsyncioTestCase):
    async def test_account_helpers_html_markdown_and_fallback(self):
        class Client:
            is_connected = lambda self: True
            get_me = AsyncMock(return_value=SimpleNamespace(premium=True, bot=False))
            get_input_entity = AsyncMock(return_value=types.InputPeerSelf())
            send_message = AsyncMock(return_value='plain')
            invoke = AsyncMock(return_value='native')
            async def __call__(self, request):
                return await self.invoke(request)
        client = install_rich_methods(Client())
        self.assertIs(install_rich_methods(client), client)
        self.assertTrue(await client.supports_native_rich())
        html = build_rich_table(['项目'], [['签到']], caption='结果', align='right')
        await client.send_rich('me', html, disable_notification=True, reply_to_message_id=42)
        request = client.invoke.call_args.args[0]
        self.assertIn('align="right"', request.rich_message.html)
        self.assertTrue(request.silent)
        self.assertEqual(request.reply_to.reply_to_msg_id, 42)
        await client.send_rich('me', '| A | B |\n|---|---|\n|1|2|', format='markdown')
        self.assertIsInstance(client.invoke.call_args.args[0].rich_message, types.InputRichMessageMarkdown)
        self.assertTrue(bytes(client.invoke.call_args.args[0]))
        client.get_me.return_value = SimpleNamespace(premium=False, bot=False)
        self.assertFalse(await client.supports_native_rich())
        await client.send_rich('me', html)
        self.assertIn('项目：签到', client.send_message.call_args.args[1])
        self.assertNotIn('<table', client.send_message.call_args.args[1])
        with self.assertRaises(ValueError):
            await client.send_rich('me', 'hello', format='invalid')

    def test_v1_structured_and_automatic_conversion(self):
        for data in ({'状态': '成功'}, [{'账号': 'A', '结果': '成功'}],
                     [[1, 2], [3, 4]], ['甲', '乙'], '账号：A\n结果：成功',
                     '任务：成功2/失败0\n账号 A：成功'):
            plain, rich = content(data)
            self.assertIn('<table', rich)
            self.assertTrue(plain)
        plain, rich = content('这是一条普通通知')
        self.assertEqual(plain, '这是一条普通通知')
        self.assertNotIn('<table', rich)

    def test_table_validation_and_safe_content(self):
        with self.assertRaises(ValueError):
            build_rich_table(['A'], [[1, 2]])
        with self.assertRaises(ValueError):
            build_rich_table(['A'], [[1]], align='invalid')
        table = build_rich_table(['项目'], [['<script>bad</script>']], caption='标题')
        plain, rich = content(table, 'rich')
        self.assertIn('&lt;script&gt;', rich)
        self.assertIn('标题', plain)
        plain, rich = content('<script>bad</script><table bordered><tr><td align="right" onclick="bad()">安全</td></tr></table>', 'rich')
        self.assertNotIn('bad', rich)
        self.assertIn('align="right"', rich)
        self.assertIn('安全', plain)
