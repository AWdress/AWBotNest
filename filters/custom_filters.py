# 系统库
import json
import unicodedata

# 第三方库
from core import CallbackQuery, Message, create, Filter, filters


async def reply_to_me_filter(_, __, m: Message):
    return bool(
        m.reply_to_message
        and m.reply_to_message.from_user
        and m.reply_to_message.from_user.is_self
    )


reply_to_me = create(reply_to_me_filter)


def _strip_math_bold(text: str) -> str:
    """将Unicode数学粗体字母(U+1D400–U+1D433)还原为普通ASCII"""
    out = []
    for c in text:
        cp = ord(c)
        if 0x1D400 <= cp <= 0x1D419:
            out.append(chr(cp - 0x1D400 + 0x41))   # 粗体A-Z → A-Z
        elif 0x1D41A <= cp <= 0x1D433:
            out.append(chr(cp - 0x1D41A + 0x61))   # 粗体a-z → a-z
        else:
            out.append(c)
    return "".join(out)


async def self_mentioned_filter(_, client, m: Message):
    """消息文本（粗体还原后）以自己的名字开头 → 自己是转出方"""
    if any(e.user and e.user.is_self for e in (m.entities or [])):
        return True
    me = client.me
    if me and m.text:
        full_name = " ".join(filter(None, [me.first_name, me.last_name]))
        return bool(full_name and _strip_math_bold(m.text).startswith(full_name))
    return False


self_mentioned = create(self_mentioned_filter)


async def self_received_filter(_, client, m: Message):
    """消息文本中包含"已向 {me} 转赠" → 自己是转入方"""
    me = client.me
    if not me or not m.text:
        return False
    full_name = " ".join(filter(None, [me.first_name, me.last_name]))
    return bool(full_name and f"已向 {full_name} 转赠" in m.text)


self_received = create(self_received_filter)


self_mentioned = create(self_mentioned_filter)


async def command_to_me_filter(_, __, m: Message):
    return bool(
        m.reply_to_message
        and m.reply_to_message.reply_to_message
        and m.reply_to_message.reply_to_message.from_user
        and m.reply_to_message.reply_to_message.from_user.is_self
    )


command_to_me = create(command_to_me_filter)


async def auth_filter(_, __, m: Message):
    return bool(m.from_user and m.from_user.id == 5848633300)


auth = create(auth_filter)


async def test_filter(_, __, m: Message):
    return bool(m.from_user and m.from_user.id == 6138413603)


test = create(test_filter)


async def cmct_pay_keyword_filter(_, __, m: Message):
    exclude_keywords = ["转账金额过大", "余额不足", "转账失败"]
    if m.reply_to_message and "+" in m.reply_to_message.text:
        for keyword in exclude_keywords:
            if keyword in m.text:
                return False
        return True
    return False


cmct_pay_keyword = create(cmct_pay_keyword_filter)


def create_bot_filter(bot_id):
    async def bot_filter(_, __, m: Message):
        return bool(m.from_user and m.from_user.is_bot and m.from_user.id == bot_id)

    return create(bot_filter)


cms_bot = create_bot_filter(6091424371)
choujiang_bot = create_bot_filter(6461022460)
zhuque_bot = create_bot_filter(5697370563)
hddobly_bot = create_bot_filter(6474948384)
yyz_bot = create_bot_filter(6296776523)
audiences_bot = create_bot_filter(2053736484)
cmct_bot = create_bot_filter(752250569)
azusa_bot = create_bot_filter(6696869468)
common_lottery_bot = create_bot_filter(6420220651)  # 通用抽奖机器人 @Lottery8Bot
hdsky_bot = create_bot_filter(8907007783)  # 天空小秘 HDSKY（拼手气红包/转赠）
hdhive_bot = create_bot_filter(5831593155)  # HDHive 抽奖机器人（影巢群专用）
dianyingpai_bot = create_bot_filter(8704462066)  # 癫影小助手（积分红包）


class CallbackDataFromFilter(Filter):
    def __init__(self, from_value):
        self.from_value = from_value

    async def __call__(self, client, callback_query: CallbackQuery):
        try:
            data = json.loads(callback_query.data)
        except Exception:
            return False
        return data.get("a") == self.from_value


def dot_command(commands):
    """
    创建一个支持 . 前缀的命令过滤器
    用法: filters.command("start") | custom_filters.dot_command("start")
    这样既支持 /start 也支持 .start
    
    参数:
        commands: 字符串或字符串列表，表示命令名称
    """
    if isinstance(commands, str):
        commands = [commands]
    
    async def func(flt, client, message: Message):
        if not message.text:
            return False
        
        text = message.text
        # 检查是否以 . 开头
        if not text.startswith("."):
            return False
        
        # 提取命令部分（去掉 . 前缀）
        parts = text[1:].split(maxsplit=1)
        if not parts:
            return False
        
        command = parts[0].lower()
        
        # 检查命令是否匹配
        if command in [cmd.lower() for cmd in flt.commands]:
            # 设置 message.command 属性，与 filters.command 保持一致
            message.command = [command] + (parts[1:] if len(parts) > 1 else [])
            if len(parts) > 1:
                # 如果有参数，按空格分割
                message.command = [command] + parts[1].split()
            else:
                message.command = [command]
            return True
        
        return False
    
    return create(func, commands=commands)


def command_with_dot(commands):
    """
    创建一个同时支持 / 和 . 前缀的命令过滤器
    用法: custom_filters.command_with_dot("start")
    这样既支持 /start 也支持 .start
    
    参数:
        commands: 字符串或字符串列表，表示命令名称
    """
    if isinstance(commands, str):
        commands = [commands]
    
    async def func(flt, client, message: Message):
        if not message.text:
            return False
        
        text = message.text.strip()
        
        # 检查是否以 / 或 . 开头
        if not (text.startswith("/") or text.startswith(".")):
            return False
        
        # 提取命令部分（去掉前缀）
        parts = text[1:].split(maxsplit=1)
        if not parts:
            return False
        
        command = parts[0].lower()
        
        # 检查命令是否匹配
        if command in [cmd.lower() for cmd in flt.commands]:
            # 设置 message.command 属性，与 filters.command 保持一致
            if len(parts) > 1:
                # 如果有参数，按空格分割
                message.command = [command] + parts[1].split()
            else:
                message.command = [command]
            return True
        
        return False
    
    return create(func, commands=commands)
