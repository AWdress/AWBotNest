"""自动报时昵称：定时把账号昵称改成当前时间。

配置存放在 state section "AUTO_CHANGENAME"：
  - name_format : 报时格式模板（默认 "{emoji}{H}:{M}"）
  - name_field  : 改哪个名字 last_name/first_name/both（默认 last_name）
开关沿用 SCHEDULER.autochangename。

模板占位符：
  {emoji} 随机表情  {H} 时  {M} 分  {S} 秒
  {date} 年-月-日   {md} 月-日   {week} 星期几
"""
# 标准库
import traceback
import random
from datetime import datetime, timedelta, timezone

# 自定义模块
from core import logger
from libs.state import state_manager
from schedulers import scheduler

SECTION = "AUTO_CHANGENAME"
DEFAULT_FORMAT = "{emoji}{H}:{M}"

auto_change_name_init = False

emojis = [chr(i) for i in range(0x1F600, 0x1F637 + 1)]  # 56个表情符号

_WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _render_name(fmt: str, now: datetime) -> str:
    """按模板渲染昵称。每次调用 {emoji} 随机取一个。"""
    return (
        fmt.replace("{emoji}", random.choice(emojis))
        .replace("{H}", now.strftime("%H"))
        .replace("{M}", now.strftime("%M"))
        .replace("{S}", now.strftime("%S"))
        .replace("{date}", now.strftime("%Y-%m-%d"))
        .replace("{md}", now.strftime("%m-%d"))
        .replace("{week}", _WEEK_CN[now.weekday()])
    )


async def auto_changename_action():
    from app import get_user_apps
    user_apps = get_user_apps()
    if not user_apps:
        logger.debug("自动报时昵称：没有已连接的用户账号，跳过")
        return

    fmt = state_manager.get_item(SECTION, "name_format", DEFAULT_FORMAT) or DEFAULT_FORMAT
    field = state_manager.get_item(SECTION, "name_field", "last_name") or "last_name"
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

    # 遍历所有已连接账号，逐个改名报时（每个账号各自随机 emoji）
    for user_app in user_apps:
        acct = getattr(user_app, "name", "未知账号")
        try:
            rendered = _render_name(fmt, now)
            # 根据配置决定改 last_name / first_name / both
            kwargs = {}
            if field in ("last_name", "both"):
                kwargs["last_name"] = rendered
            if field in ("first_name", "both"):
                kwargs["first_name"] = rendered
            if not kwargs:  # 配置异常兜底
                kwargs["last_name"] = rendered

            await user_app.update_profile(**kwargs)

            # 验证更新结果
            me = await user_app.get_me()
            check_field = "first_name" if field == "first_name" else "last_name"
            if getattr(me, check_field, None) != rendered:
                raise Exception(f"修改 {check_field} 失败")
        except Exception as e:
            trac = "\n".join(traceback.format_exception(e))
            logger.info(f"[自动报时] 账号 {acct} 更新失败! \n{trac}")


async def auto_changename_temp():
    changename_switch = state_manager.get_item("SCHEDULER", "autochangename", "off")
    if changename_switch == 'on':
        # 已存在则先移除，确保间隔配置变更后能即时生效
        if scheduler.get_job("autochangename"):
            scheduler.remove_job("autochangename")
        try:
            interval = int(state_manager.get_item(SECTION, "interval_min", "5"))
            if interval < 1:
                interval = 1
        except (ValueError, TypeError):
            interval = 5
        scheduler.add_job(
            auto_changename_action, "cron", minute=f"*/{interval}", id="autochangename"
        )
        logger.info(f"自动报时昵称已启用（每 {interval} 分钟）")
    else:
        if scheduler.get_job("autochangename"):
            scheduler.remove_job("autochangename")
        logger.info(f"自动报时昵称已关闭")
