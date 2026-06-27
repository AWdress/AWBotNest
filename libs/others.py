# 标准库
import asyncio
import html
from datetime import datetime, time, timedelta

# 第三方库
from core import filters, Client, Message, PeerIdInvalid

# 自定义模块
from libs.log import logger
from libs.state import state_manager


SEND_BLACKLIST_SECTION = "SEND_BLACKLIST"


def get_send_blacklist() -> list[int]:
    raw = state_manager.get_item(SEND_BLACKLIST_SECTION, "user_ids", [])
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def is_user_in_send_blacklist(user_id: int | str | None) -> bool:
    if user_id is None:
        return False
    try:
        target = int(user_id)
    except (TypeError, ValueError):
        return False
    return target in get_send_blacklist()


def build_user_markdown_link(user_id: int | str | None, user_name: str | None) -> str:
    safe_name = str(user_name or "未知用户")
    for ch in ("\\", "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
        safe_name = safe_name.replace(ch, f"\\{ch}")
    if user_id is None:
        return safe_name
    return f"[{safe_name}](tg://user?id={user_id})"


def build_user_html_link(user_id: int | str | None, user_name: str | None) -> str:
    safe_name = html.escape(str(user_name or "未知用户"))
    if user_id is None:
        return safe_name
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'



async def delete_message(message_del: Message, sleep_time: float = 35) -> asyncio.Task:
    """
    删除 Telegram 消息，非阻塞方式在指定时间后执行删除，返回任务对象。

    Args:
        message_del: 要删除的 Telegram 消息对象
        sleep_time: 延迟时间（秒），默认 35 秒

    Returns:
        asyncio.Task: 可用于取消或跟踪删除任务
    """

    async def delayed_delete():
        try:
            await asyncio.sleep(sleep_time)
            if message_del is not None:
                await message_del.delete()
        except Exception as e:
            logger.error(f"删除消息失败: {e}")

    return asyncio.create_task(delayed_delete())


async def get_user_info(client: Client, tgid):
    """
    根据 TGID 查询用户信息
    返回: (user_entity, result)
    result:
        1: 查询成功
        2: 多次查询失败（用户不存在）
        3: 发生其他异常
    """
    for attempt in (1, 2):
        try:
            user_entity = await client.get_users(tgid)
            logger.debug(
                f"{'二次' if attempt == 2 else '一次'}查询成功: ID: {tgid}, userinfo: {user_entity}"
            )
            return user_entity, 1
        except PeerIdInvalid:
            logger.debug(
                f"{'二次' if attempt == 2 else '一次'}查询账户不存在: ID: {tgid}"
            )
            continue
        except Exception as e:
            logger.error(f"查询出现异常: ID: {tgid}, error: {e}")
            return f"An unexpected error occurred: {e}", 3
    return PeerIdInvalid, 2


async def get_usertoarray(client: Client, sorted_array):
    """
    查询数组内所有 ID 的用户名
    """
    new_array = []
    logger.debug(f"需要查询用户名的 ID 列表: {sorted_array}")
    for row in sorted_array:
        raw_id = row[0]
        tgid = raw_id[4:]
        if tgid.isdigit():
            user_entity, code = await get_user_info(client, int(tgid))
            if code == 1:
                first = getattr(user_entity, "first_name", "")
                last = getattr(user_entity, "last_name", "")
                tgname = f"{first} {last}".strip() or " "
            else:
                tgname = str(user_entity)
        else:
            tgname = raw_id
        new_array.append([tgname] + row[1:])
    return new_array


#################将各个形式的日期转为date格式###################
def parse_date_input(value):
    if isinstance(value, datetime):
        return value
    elif isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d")
    elif hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return datetime.combine(value, time.min)
    raise ValueError(f"Unsupported date type: {type(value)}")
