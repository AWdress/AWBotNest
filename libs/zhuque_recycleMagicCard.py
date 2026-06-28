# 标准库
import asyncio

# 第三方库
import aiohttp

# 自定义模块
from libs.state import state_manager
from core import logger

SITE_NAME = "zhuque"
prizes2 = {
    1: "改名卡",
    2: "神佑7天卡",
    3: "邀请卡",
    4: "自动释放7天卡"
}
BONUS_VALUES = {1: 300000, 2: 100000, 3: 80000, 4: 30000}

url = "https://zhuque.in/api/mall/recycleMagicCard"
lock = asyncio.Lock()
cost = 0
card_counts = {k: 0 for k in prizes2.keys()}

re_number = 0
re_card_id = 4

async def recycleMagicCard(card_id,number):
    async with aiohttp.ClientSession() as session:
        tasks = []
        share = 4
        base_value = number // share
        remainder = number % share
        numbers = [base_value] * share
        for i in range(remainder):
            numbers[i] += 1
        for i in range(share):
            tasks.append(fetch_prize(card_id,numbers[i],session))
        # 并发执行所有协程
        await asyncio.gather(*tasks)

async def fetch_prize(card_id,number,session):

    global cost
    cookie = state_manager.get_item(SITE_NAME.upper(),"cookie","")
    xcsrf = state_manager.get_item(SITE_NAME.upper(),"xcsrf","")

    headers = {
        "Cookie": cookie,
        "X-Csrf-Token": xcsrf,
    }

    for i in range(number):
        async with session.post(url, headers=headers,json={"id":card_id}) as response:
            if response.status == 200:
                json_response = await response.json()
                if json_response:
                    code_commade = json_response.get("code", {})
                    if code_commade:
                        async with lock:
                            card_counts[card_id] += 1
                else:
                    return None
            else:
                logger.error(f"Request failed with status {response.status}")
                return None

# 主函数
async def main(card_id,number):
    global cost
    global card_counts
    await recycleMagicCard(card_id,number)
    bonus_back = BONUS_VALUES[card_id] * int(number) * 0.8

    re_msg = f"已成功回收 ：\n  {prizes2[card_id]}: {card_counts[card_id]} 张 \n    获得 {bonus_back:,.2f} 灵石 "
    card_counts_re = card_counts
    card_counts = {k: 0 for k in prizes2.keys()}
    return re_msg


# 运行主函数
if __name__ == "__main__":
    asyncio.run(main(re_card_id,re_number))

