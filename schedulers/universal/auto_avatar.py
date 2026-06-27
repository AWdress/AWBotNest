"""自动换头像：定时把用户账号头像换成图片池里随机一张。

换的是用户账号（user_app）头像，不是 bot。图片由管理员通过 bot 私聊上传，
每个账号有独立图片池，存放在 assets/avatars/<账号session名>/ 下。

配置存放在 state section "AUTO_AVATAR"：
  - interval_min : 换头像间隔分钟（默认 60，最小 10，防 Telegram 限流）
  - delete_old   : 是否删除本功能上次设置的旧头像 on/off（默认 on）
开关沿用 SCHEDULER.autoavatar。

每账号上次设置的头像 file_id 记录在 state section "AUTO_AVATAR_<账号名大写>" 的 last_file_id，
仅用于换新头像后删除上一张（绝不删除用户原有的真实头像）。
"""
# 标准库
import random
import traceback
from pathlib import Path

# 自定义模块
from core import logger
from libs.state import state_manager
from schedulers import scheduler

SECTION = "AUTO_AVATAR"
MIN_INTERVAL = 10  # 最小间隔分钟，防止 FloodWait

# 项目根 / 头像存放目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
AVATAR_ROOT = _PROJECT_ROOT / "assets" / "avatars"

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def get_account_dir(account_name: str) -> Path:
    """返回某账号的图片池目录（不存在则创建）。"""
    d = AVATAR_ROOT / account_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_pool(account_name: str) -> list[Path]:
    """列出某账号图片池里的所有图片文件。"""
    d = AVATAR_ROOT / account_name
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS)


async def _change_one(user_app, delete_old: bool):
    """给单个账号换一张随机头像。"""
    account_name = getattr(user_app, "name", "未知账号")
    pool = list_pool(account_name)
    if not pool:
        logger.debug(f"[自动换头像] 账号 {account_name} 图片池为空，跳过")
        return

    img_path = random.choice(pool)
    acct_section = f"{SECTION}_{account_name.upper()}"
    old_file_id = state_manager.get_item(acct_section, "last_file_id", "")

    # 设置新头像（用 InputChatPhotoStatic 避免传字符串的弃用警告）
    from pyrogram.types import InputChatPhotoStatic
    await user_app.set_profile_photo(photo=InputChatPhotoStatic(str(img_path)))

    # 取刚设置的头像 file_id（相册第一张即最新）并记录
    new_file_id = ""
    try:
        async for photo in user_app.get_chat_photos("me", limit=1):
            new_file_id = photo.file_id
            break
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[自动换头像] 账号 {account_name} 获取新头像ID失败: {e}")

    if new_file_id:
        state_manager.set_section(acct_section, {"last_file_id": new_file_id})

    # 删除本功能上次设置的旧头像（仅删记录过的那张，不动用户其他头像）
    if delete_old and old_file_id and old_file_id != new_file_id:
        try:
            await user_app.delete_profile_photos(old_file_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[自动换头像] 账号 {account_name} 删除旧头像失败: {e}")

    logger.info(f"[自动换头像] 账号 {account_name} 已换头像: {img_path.name}")


async def auto_avatar_action():
    from app import get_user_apps
    user_apps = get_user_apps()
    if not user_apps:
        logger.debug("自动换头像：没有已连接的用户账号，跳过")
        return

    delete_old = state_manager.get_item(SECTION, "delete_old", "on") == "on"

    for user_app in user_apps:
        try:
            await _change_one(user_app, delete_old)
        except Exception as e:
            trac = "\n".join(traceback.format_exception(e))
            acct = getattr(user_app, "name", "未知账号")
            logger.info(f"[自动换头像] 账号 {acct} 换头像失败! \n{trac}")


async def auto_avatar_temp():
    switch = state_manager.get_item("SCHEDULER", "autoavatar", "off")
    if switch == 'on':
        # 已存在则先移除，确保间隔配置变更后能即时生效
        if scheduler.get_job("autoavatar"):
            scheduler.remove_job("autoavatar")
        try:
            interval = int(state_manager.get_item(SECTION, "interval_min", "60"))
            if interval < MIN_INTERVAL:
                interval = MIN_INTERVAL
        except (ValueError, TypeError):
            interval = 60
        scheduler.add_job(
            auto_avatar_action, "cron", minute=f"*/{interval}", id="autoavatar"
        )
        logger.info(f"自动换头像已启用（每 {interval} 分钟）")
    else:
        if scheduler.get_job("autoavatar"):
            scheduler.remove_job("autoavatar")
        logger.info(f"自动换头像已关闭")
