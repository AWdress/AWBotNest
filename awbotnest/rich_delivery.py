"""原生 Rich Message 投递；明确拒绝才降级，网络结果不明时不重复发送。"""
import inspect
import json
import logging
import httpx

logger = logging.getLogger('awbotnest.notifier')


class DeliveryUncertain(RuntimeError):
    """The request may have been delivered; automatic fallback could duplicate it."""


async def send_rich(client, target, rich, plain, *, token='', proxy='', rich_format='html',
                    is_rtl=None, skip_entity_detection=None, **kwargs):
    if token:
        # 直接使用 transport，避免 HTTP 客户端日志记录 URL 中的 Bot Token。
        try:
            async with httpx.AsyncHTTPTransport(proxy=proxy or None) as transport:
                request = httpx.Request('POST', f'https://api.telegram.org/bot{token}/sendRichMessage',
                                        json={'chat_id': target, 'rich_message': {'html': rich}},
                                        extensions={'timeout': dict(connect=15, read=30, write=30, pool=15)})
                response = await transport.handle_async_request(request)
                try:
                    data = json.loads(await response.aread())
                finally:
                    await response.aclose()
        except Exception:
            raise DeliveryUncertain('原生富文本投递结果未确认，请检查网络；未重复发送') from None
        if data.get('ok'):
            return data.get('result')
        if data.get('error_code') not in {400, 404}:
            raise RuntimeError(f'原生富文本投递失败（状态码 {data.get("error_code", "未知")}）')
        logger.warning('Bot 原生富文本被拒绝，改用兼容文本')
    else:
        me = await client.get_me()
        if getattr(me, 'bot', False) or getattr(me, 'premium', False):
            # HTML 和 blocks 是不同的 TL 构造器，不能用 InputRichMessage 检查 html。
            from telethon import functions, types
            rich_type = getattr(types, 'InputRichMessageHTML' if rich_format == 'html' else 'InputRichMessageMarkdown', None)
            sender = functions.messages.SendMessageRequest
            if rich_type and 'rich_message' in inspect.signature(sender).parameters and rich_format in inspect.signature(rich_type).parameters:
                try:
                    return await client(sender(peer=await client.get_input_entity(target), message='',
                                               rich_message=rich_type(**{rich_format: rich}, rtl=is_rtl,
                                                                       noautolink=skip_entity_detection), **kwargs))
                except Exception as exc:
                    if type(exc).__name__ not in {'PremiumAccountRequiredError', 'RichMessageInvalidError', 'MessageEmptyError'}:
                        raise
                    logger.warning('用户账号原生富文本不可用，改用兼容文本')
            else:
                logger.warning('当前 Telethon 未提供原生富文本接口，用户账号改用兼容文本')
    fallback = dict(kwargs)
    reply = fallback.pop('reply_to', None)
    if reply is not None:
        fallback['reply_to'] = getattr(reply, 'reply_to_msg_id', reply)
    return await client.send_message(target, plain, parse_mode='md' if rich_format == 'markdown' else None, **fallback)


def install_rich_methods(client):
    """Add V1 plugin-facing helpers without replacing Telethon's native send_message."""
    if getattr(client, '_aw_rich_methods', False):
        return client

    async def supports_native_rich():
        from telethon import functions, types
        if not client.is_connected():
            return False
        if not hasattr(types, 'InputRichMessageHTML') or 'rich_message' not in inspect.signature(functions.messages.SendMessageRequest).parameters:
            return False
        try:
            me = await client.get_me()
            return bool(getattr(me, 'bot', False) or getattr(me, 'premium', False))
        except Exception:
            return False

    async def send(chat_id, content, *, format='html', is_rtl=None, skip_entity_detection=None, **kwargs):
        from telethon import types
        from .rich_text import sanitize_rich_html, rich_html_to_plain
        if not client.is_connected():
            raise RuntimeError('目标账号未连接，无法发送消息')
        fmt = str(format or 'html').strip().lower()
        if fmt not in {'html', 'markdown'}:
            raise ValueError('Rich Message 格式只支持 html 或 markdown')
        rich = str(content or '')
        plain = rich
        if fmt == 'html':
            rich = sanitize_rich_html(rich)
            plain = rich_html_to_plain(rich)
        if not plain.strip():
            raise ValueError('Rich Message 内容不能为空')
        if 'disable_notification' in kwargs:
            kwargs['silent'] = kwargs.pop('disable_notification')
        if 'reply_to_message_id' in kwargs:
            kwargs['reply_to'] = types.InputReplyToMessage(kwargs.pop('reply_to_message_id'))
        return await send_rich(client, chat_id, rich, plain, rich_format=fmt, is_rtl=is_rtl,
                               skip_entity_detection=skip_entity_detection, **kwargs)

    client.supports_native_rich = supports_native_rich
    client.send_rich = send
    client._aw_rich_methods = True
    return client
