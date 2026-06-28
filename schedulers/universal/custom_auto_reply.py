"""通用定时自动回复模块 - 支持多个自定义回复任务"""
# 标准库
import traceback
from datetime import datetime, timezone, timedelta

# 自定义模块
from core import logger
from libs.state import state_manager
from schedulers import scheduler


async def custom_auto_reply_action(task_id: str):
    """执行自定义定时回复

    Args:
        task_id: 任务ID，用于区分不同的定时回复任务
    """
    from app import get_user_apps, get_bot_app
    from config.config import PT_GROUP_ID

    user_apps = get_user_apps()
    bot_app = get_bot_app()

    try:
        # 获取当前时间（东八区）
        current_time = datetime.now(timezone(timedelta(hours=8)))

        # 获取任务配置
        task_config_key = f"CUSTOM_AUTO_REPLY_{task_id.upper()}"

        # 获取日期范围配置
        start_date_str = state_manager.get_item(
            task_config_key,
            "start_date",
            None
        )
        end_date_str = state_manager.get_item(
            task_config_key,
            "end_date",
            None
        )

        # 如果设置了日期范围，检查当前时间是否在范围内
        if start_date_str and end_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))

                if not (start_date <= current_time <= end_date):
                    logger.debug(f"任务 {task_id}: 当前时间不在活动时间范围内，跳过发送")
                    return
            except ValueError as e:
                logger.error(f"任务 {task_id}: 日期格式错误: {e}")
                return

        # 获取目标聊天ID
        target_chat_id_str = state_manager.get_item(task_config_key, "target_chat_id", None)
        if not target_chat_id_str:
            logger.error(f"任务 {task_id}: 未设置目标聊天ID")
            return

        # 支持数字ID或 @username 格式
        if target_chat_id_str.startswith("@"):
            target_chat_id = target_chat_id_str
        else:
            try:
                target_chat_id = int(target_chat_id_str)
            except ValueError:
                logger.error(f"任务 {task_id}: 目标聊天ID格式错误: {target_chat_id_str}")
                return

        # 获取回复消息内容
        reply_message = state_manager.get_item(task_config_key, "reply_message", None)
        if not reply_message:
            logger.error(f"任务 {task_id}: 未设置回复消息内容")
            return

        # 检查已连接账号
        if not user_apps:
            logger.error(f"任务 {task_id}: 没有已连接的用户账号，跳过发送")
            return

        # 格式化当前时间用于显示
        current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
        # 获取任务名称
        task_name = state_manager.get_item(task_config_key, "task_name", task_id)

        # 遍历所有已连接账号逐个发送
        logger.debug(f"任务 {task_id}: 准备用 {len(user_apps)} 个账号发送消息到 {target_chat_id}")
        for user_app in user_apps:
            # 账号标识（昵称 + 用户名/ID）
            _me = getattr(user_app, "me", None)
            if _me:
                acct = f"{_me.first_name}(@{_me.username})" if _me.username else f"{_me.first_name}(ID:{_me.id})"
            else:
                acct = getattr(user_app, "name", "未知账号")

            try:
                sent_message = await user_app.send_message(target_chat_id, reply_message)
                logger.info(f"任务 {task_id}: [{acct}] 定时回复成功，消息ID: {sent_message.id}")
            except Exception as send_error:  # noqa: BLE001
                logger.error(f"任务 {task_id}: [{acct}] 定时回复失败: {send_error}")
                try:
                    await bot_app.send_message(
                        PT_GROUP_ID.get("BOT_MESSAGE_CHAT"),
                        f"**定时回复失败**\n\n账号：{acct}\n任务：{task_name}\n目标：{target_chat_id}\n错误：{send_error}",
                    )
                except Exception:
                    pass
                continue

            # 构建消息链接
            if isinstance(target_chat_id, int) and target_chat_id < 0:  # 群组或频道
                group_id_str = str(target_chat_id).replace("-100", "")
                message_link = f"https://t.me/c/{group_id_str}/{sent_message.id}"
            elif isinstance(target_chat_id, str) and target_chat_id.startswith("@"):
                # 对于 @username 格式，检查是否是公开群组/频道
                # 机器人私聊无法生成有效链接，只显示消息ID
                username = target_chat_id[1:]  # 去掉 @
                if username.endswith("_bot") or username.endswith("Bot"):
                    # 机器人私聊，无法生成链接
                    message_link = f"目标: {target_chat_id}, 消息ID: {sent_message.id}"
                else:
                    # 公开群组/频道
                    message_link = f"https://t.me/{username}/{sent_message.id}"
            else:  # 私聊（数字ID）
                message_link = f"消息ID: {sent_message.id}"

            # 发送通知到Bot消息聊天（每个账号各一条，区分账号）
            notification = f"""**定时回复已发送**

账号：{acct}
任务名称：{task_name}
发送时间：{current_time_str}
目标聊天：{target_chat_id}
消息内容：
{reply_message[:100]}{'...' if len(reply_message) > 100 else ''}

{message_link}"""

            try:
                await bot_app.send_message(
                    PT_GROUP_ID.get("BOT_MESSAGE_CHAT"),
                    notification,
                    disable_web_page_preview=True
                )
            except Exception as notify_error:
                logger.error(f"任务 {task_id}: [{acct}] 发送通知消息失败: {notify_error}")

    except Exception as e:
        trac = "\n".join(traceback.format_exception(e))
        logger.error(f"任务 {task_id}: 定时回复失败! \n{trac}")

        # 发送失败通知
        try:
            from config.config import PT_GROUP_ID
            task_name = state_manager.get_item(f"CUSTOM_AUTO_REPLY_{task_id.upper()}", "task_name", task_id)
            error_msg = f"**定时回复失败**\n\n任务：{task_name}\n错误信息：\n{str(e)}"
            await bot_app.send_message(PT_GROUP_ID.get("BOT_MESSAGE_CHAT"), error_msg)
        except Exception:
            pass


async def init_custom_auto_reply_task(task_id: str):
    """初始化单个自定义定时回复任务

    Args:
        task_id: 任务ID
    """
    task_config_key = f"CUSTOM_AUTO_REPLY_{task_id.upper()}"
    scheduler_key = f"custom_auto_reply_{task_id}"

    # 检查任务开关
    task_switch = state_manager.get_item("SCHEDULER", scheduler_key, "off")

    logger.debug(f"初始化任务 {task_id}: 开关状态={task_switch}, 配置键={task_config_key}")

    if task_switch == 'on':
        # 获取定时配置
        cron_config = state_manager.get_item(task_config_key, "cron_config", "0,3,6,9,12,15,18,21")
        cron_minute = state_manager.get_item(task_config_key, "cron_minute", "0")

        logger.debug(f"任务 {task_id}: 定时配置 hour={cron_config}, minute={cron_minute}")

        # 如果任务已存在，先移除再重新添加（以更新配置）
        if scheduler.get_job(scheduler_key):
            scheduler.remove_job(scheduler_key)
            logger.debug(f"任务 {task_id}: 移除旧任务，准备更新")

        # 添加定时任务
        scheduler.add_job(
            custom_auto_reply_action,
            "cron",
            args=[task_id],
            hour=cron_config,
            minute=cron_minute,
            id=scheduler_key
        )
        logger.debug(f"任务 {task_id}: 已添加到调度器")

        task_name = state_manager.get_item(task_config_key, "task_name", task_id)
        logger.info(f"定时回复任务 [{task_name}] 已启用")
    else:
        if scheduler.get_job(scheduler_key):
            scheduler.remove_job(scheduler_key)

        task_name = state_manager.get_item(task_config_key, "task_name", task_id)
        logger.info(f"定时回复任务 [{task_name}] 已关闭")


async def custom_auto_reply_init():
    """初始化所有自定义定时回复任务"""
    # 获取所有任务ID列表
    task_ids_str = state_manager.get_item("CUSTOM_AUTO_REPLY", "task_ids", "")

    if not task_ids_str:
        logger.info("未配置任何自定义定时回复任务")
        return

    task_ids = [tid.strip() for tid in task_ids_str.split(",") if tid.strip()]

    for task_id in task_ids:
        await init_custom_auto_reply_task(task_id)
