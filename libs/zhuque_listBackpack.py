# 标准库
import asyncio

# 第三方库
import aiohttp

# 自定义模块
from libs.state import state_manager
from core import logger


SITE_NAME = "zhuque"

prizes = {
    1: "改名卡",
    2: "神佑7天卡",
    3: "邀请卡",
    4: "自动释放7天卡"
}


card_counts = {k: 0 for k in prizes.keys()}

url = "https://zhuque.in/api/mall/listBackpack"


async def listBackpack():

    cookie = state_manager.get_item(SITE_NAME.upper(),"cookie","")
    xcsrf = state_manager.get_item(SITE_NAME.upper(),"xcsrf","")

    headers = {
        "Cookie": cookie,
        "X-Csrf-Token": xcsrf,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                json_response = await response.json()
                if json_response:
                    card_data = json_response.get("data", {})
                    for i in range(len(card_data)):
                        prize_index = card_data[i].get("card_id")
                        card_amount = card_data[i].get("amount")
                        if prize_index is not None:
                            prize_index = int(prize_index)
                            card_counts[prize_index] = card_amount
                return card_counts
            else:
                logger.error(f"Request failed with status {response.status}")
                return None
