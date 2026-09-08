"""安全的通知内容转换；Telegram HTML 与其他渠道纯文本共用。"""
import html


def content(text, format='text'):
    from .rich_text import (structured_to_rich_html, text_to_notification_rich_html,
                            sanitize_rich_html, rich_html_to_plain)
    if format not in {'text', 'rich'}:
        raise ValueError('通知格式只支持 text 或 rich')
    if isinstance(text, (dict, list)):
        rich = structured_to_rich_html(text)
    elif format == 'rich':
        rich = sanitize_rich_html(str(text or ''))
    else:
        rich = text_to_notification_rich_html(str(text or '').strip())
    return rich_html_to_plain(rich), rich


def notification(plugin_name, plain, rich, level, category='', account=''):
    label, icon = {'info': ('通知', '🔔'), 'success': ('成功', '✅'),
                   'warning': ('警告', '⚠️'), 'error': ('错误', '❌')}[level]
    details = [label] + ([category] if category else []) + ([f'账号：{account}'] if account else [])
    title = f'{icon} {plugin_name or "系统"}'
    return (f'{title}\n{" · ".join(details)}\n────────────\n{plain}',
            f'<b>{html.escape(title)}</b><br>{html.escape(" · ".join(details))}<br><br>{rich}')
