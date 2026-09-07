"""安全的通知内容转换；Telegram HTML 与其他渠道纯文本共用。"""
import html
import json
from html.parser import HTMLParser


class RichText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rich, self.plain, self.stack = [], [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'object', 'svg'}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {'b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre', 'blockquote', 'table', 'tr', 'td', 'th', 'mark'}:
            if tag in {'tr', 'td', 'th'}:
                self.plain.append('\n' if tag == 'tr' else ' | ')
            tag = {'strong': 'b', 'em': 'i'}.get(tag, tag)
            self.rich.append(f'<{tag}>')
            self.stack.append(tag)
        elif tag in {'br', 'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3'}:
            self.handle_data('\n')
        elif tag in {'td', 'th'}:
            self.handle_data(' | ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'iframe', 'object', 'svg'}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        tag = {'strong': 'b', 'em': 'i'}.get(tag, tag)
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.rich.append(f'</{current}>')
                if current == tag:
                    break
        elif tag in {'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3'}:
            self.handle_data('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.plain.append(data)
            self.rich.append(html.escape(data))


def content(text, format='text'):
    if format not in {'text', 'rich'}:
        raise ValueError('通知格式只支持 text 或 rich')
    if isinstance(text, (dict, list)):
        rows = text if isinstance(text, list) else [text]
        def value(v):
            return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        plain = '\n\n'.join('\n'.join(f'{k}：{value(v)}' for k, v in row.items())
                            if isinstance(row, dict) else value(row) for row in rows)
        if rows and all(isinstance(row, dict) for row in rows):
            keys = list(dict.fromkeys(k for row in rows for k in row))
            rich = '<table><tr>' + ''.join(f'<th>{html.escape(str(k))}</th>' for k in keys) + '</tr>'
            rich += ''.join('<tr>' + ''.join(f'<td>{html.escape(value(row.get(k, "")))}</td>' for k in keys) + '</tr>' for row in rows)
            return plain, rich + '</table>'
        return plain, html.escape(plain)
    if format == 'rich':
        parser = RichText()
        parser.feed(str(text or ''))
        parser.close()
        rich = ''.join(parser.rich) + ''.join(f'</{tag}>' for tag in reversed(parser.stack))
        return ''.join(parser.plain).strip(), rich.strip()
    plain = str(text or '').strip()
    return plain, html.escape(plain)


def notification(plugin_name, plain, rich, level, category='', account=''):
    label, icon = {'info': ('通知', '🔔'), 'success': ('成功', '✅'),
                   'warning': ('警告', '⚠️'), 'error': ('错误', '❌')}[level]
    details = [label] + ([category] if category else []) + ([f'账号：{account}'] if account else [])
    title = f'{icon} {plugin_name or "系统"}'
    return (f'{title}\n{" · ".join(details)}\n────────────\n{plain}',
            f'<b>{html.escape(title)}</b>\n{html.escape(" · ".join(details))}\n\n{rich}')
