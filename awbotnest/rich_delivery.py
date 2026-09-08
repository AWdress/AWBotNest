"""原生 Rich Message 投递；明确拒绝才降级，网络结果不明时不重复发送。"""
import inspect
import json
import logging
import httpx

logger = logging.getLogger('awbotnest.notifier')


class DeliveryUncertain(RuntimeError):
    """The request may have been delivered; automatic fallback could duplicate it."""


async def send_rich(client, target, rich, plain, *, token='', proxy=''):
    if token:
        # 直接使用 transport，避免 HTTP 客户端日志记录 URL 中的 Bot Token。
        try:
            async with httpx.AsyncHTTPTransport(proxy=proxy or None) as transport:
                request = httpx.Request('POST', f'https://api.telegram.org/bot{token}/sendRichMessage',
                                        json={'chat_id': target, 'rich_message': {'html': rich.replace('\n', '<br>')}},
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
            rich_type = getattr(types, 'InputRichMessageHTML', None)
            sender = functions.messages.SendMessageRequest
            if rich_type and 'rich_message' in inspect.signature(sender).parameters and 'html' in inspect.signature(rich_type).parameters:
                try:
                    return await client(sender(peer=await client.get_input_entity(target), message='',
                                               rich_message=rich_type(html=rich.replace('\n', '<br>'))))
                except Exception as exc:
                    if type(exc).__name__ not in {'PremiumAccountRequiredError', 'RichMessageInvalidError', 'MessageEmptyError'}:
                        raise
                    logger.warning('用户账号原生富文本不可用，改用兼容文本')
            else:
                logger.warning('当前 Telethon 未提供原生富文本接口，用户账号改用兼容文本')
    return await client.send_message(target, plain, parse_mode=None)
