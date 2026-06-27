# 标准库
import asyncio
from typing import Optional, Tuple
from datetime import datetime, timedelta, date

# 第三方库
import aiohttp

# 自定义模块
from core import logger
from libs.state import state_manager
from infra.container import get_container
from models.redpocket_db_modle import Redpocket  # 仅用于读取最后触发时间
from schedulers import scheduler
# PT_GROUP_ID 属业务数据（已移出平台 config）；缺失时回退空字典，
# 由内置包/插件在运行时注入。此调度任务仅在被启用时实际执行。
try:
    from config.config import PT_GROUP_ID
except ImportError:
    PT_GROUP_ID = {}



SITE_NAME = "zhuque"
BONUS_NAME = "灵石"

url = "https://zhuque.in/api/gaming/fireGenshinCharacterMagic"


async def fireGenshinCharacterMagic() -> Optional[Tuple[str, float]]:
    """
    调用朱雀 API 释放原神角色技能，返回 code 指令和奖励值 bonus。
    """
    cookie = state_manager.get_item(SITE_NAME.upper(),"cookie","")
    xcsrf = state_manager.get_item(SITE_NAME.upper(),"xcsrf","")
    headers = {
        "Cookie": cookie,
        "X-Csrf-Token": xcsrf,
    }
    async with aiohttp.ClientSession() as session:
        try:
            logger.info("开始自动朱雀释放")
            async with session.post(
                url, headers=headers, json={"all": 1}
            ) as response:
                json_response = await response.json()
                if response.status == 200 and json_response:
                    code_command = json_response.get("data", {}).get("code", "")
                    bonus = json_response.get("data", {}).get("bonus", 0)
                    logger.info(f"释放成功，指令: {code_command}，奖励: {bonus}")
                    return code_command, bonus
                else:
                    logger.warning(
                        f"释放失败，状态码: {response.status}，返回: {json_response}"
                    )
        except aiohttp.ClientError as e:
            logger.error(f"请求异常: {e}")
        except Exception as e:
            logger.exception(f"未知异常: {e}")
    return None


################朱雀释放##################################
async def zhuque_autofire_firsttimeget():
    try:
        last_time = await Redpocket.get_today_latest_fire_createtime(
            SITE_NAME, "firegenshin"
        )
    except Exception as e:
        logger.exception(f"提交失败: 用户消息, 错误：{e}")
    if last_time:
        if date.today() - last_time.date() > timedelta(days=1):
            next_time = datetime.now() + timedelta(seconds=10)
        else:
            next_time = last_time + timedelta(days=1) + timedelta(seconds=10)
    else:
        next_time = datetime.now() + timedelta(seconds=30)
    scheduler.add_job(
        zhuque_autofire,
        "date",
        run_date=next_time,
        id="firegenshin",
        replace_existing=True,
    )


async def zhuque_autofire():
    from app import get_bot_app
    bot_app = get_bot_app()
    now = datetime.now()
    try:
        result1 = await fireGenshinCharacterMagic()
        await asyncio.sleep(15)
        result2 = await fireGenshinCharacterMagic()

        code1, bonus1 = result1 if result1 else ("", 0)
        code2, bonus2 = result2 if result2 else ("", 0)
        total_bonus = bonus1 + bonus2

        success = any("SUCCESS" in code for code in (code1, code2))

        if success and total_bonus > 0:
            next_time = now + timedelta(days=1) + timedelta(seconds=15)
            logger.info(
                f"释放成功：共得 {total_bonus} 灵石，下次时间：{next_time.isoformat()}"
            )
            await get_container().redpocket_record_service().record(SITE_NAME, "firegenshin", total_bonus)
            await bot_app.send_message(PT_GROUP_ID["BOT_MESSAGE_CHAT"], f"{now.replace(microsecond=0)} {SITE_NAME} 释放获得 {total_bonus} {BONUS_NAME} " )            
            
        else:
            next_time = now + timedelta(minutes=20)  # 适度增加重试间隔
            logger.warning(f"释放失败或无奖励，20分钟后重试：{next_time.isoformat()}")                     
        scheduler.add_job(
            zhuque_autofire,
            "date",
            run_date=next_time,
            id="firegenshin",
            replace_existing=True,
        )

    except Exception as e:
        logger.exception(f"朱雀释放任务异常：{e}")
