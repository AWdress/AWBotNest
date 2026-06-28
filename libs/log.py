# 标准库
import io
import sys
import logging
from pathlib import Path
from datetime import datetime
from logging.handlers import RotatingFileHandler

# 第三方库
import pytz


# 强制标准输出使用 UTF-8，避免 Windows/容器环境日志乱码
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # 日志系统不应因编码设置失败而中断
    pass


# 可选：东八区时间格式
class CSTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        time_utc8 = datetime.fromtimestamp(
            record.created, pytz.timezone("Asia/Shanghai")
        )
        return time_utc8.strftime(datefmt or "%Y-%m-%d %H:%M:%S(%Z)")


formatter = CSTFormatter("[%(levelname)s] %(asctime)s - %(message)s")
logger = logging.getLogger("main")
# INFO 起步：DEBUG 是开发噪音，不进文件/控制台/前端日志页
logger.setLevel(logging.INFO)

# 创建错误日志记录器（已弃用，保留兼容性）
error_logger = logging.getLogger("error")
error_logger.setLevel(logging.ERROR)

# 自定义日志过滤器：抑制 Pyrogram 的常见错误堆栈
class PyrogramErrorFilter(logging.Filter):
    """过滤 Pyrogram 框架的常见错误，避免日志噪音"""
    def filter(self, record):
        msg_str = str(record.msg)
        exc_text = str(record.exc_text) if record.exc_text else ""

        # 过滤掉 PeerIdInvalid 相关的错误堆栈
        if "PEER_ID_INVALID" in msg_str or "PEER_ID_INVALID" in exc_text:
            return False
        if "ID not found:" in msg_str or "ID not found:" in exc_text:
            return False
        if "PeerIdInvalid" in msg_str or "PeerIdInvalid" in exc_text:
            return False
        # 过滤掉其他常见的 Telegram API 错误堆栈
        if "CHANNEL_INVALID" in msg_str or "CHANNEL_INVALID" in exc_text:
            return False
        if "CHANNEL_PRIVATE" in msg_str or "CHANNEL_PRIVATE" in exc_text:
            return False
        return True


class InfoAndAboveFilter(logging.Filter):
    # 仅保留 INFO 及以上级别日志
    def filter(self, record):
        return record.levelno >= logging.INFO

# 抑制 Pyrogram 框架的详细错误日志（特别是 PeerIdInvalid 相关的堆栈跟踪）
# 只保留 CRITICAL 级别的 Pyrogram 日志
pyrogram_logger = logging.getLogger("pyrogram")
pyrogram_logger.setLevel(logging.CRITICAL)
pyrogram_logger.addFilter(PyrogramErrorFilter())

logging.getLogger("pyrogram.session").setLevel(logging.CRITICAL)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.CRITICAL)

# 防止重复添加 handler
# 创建日志目录
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "Mytgbot.log"

# 控制台处理器
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(formatter)
ch.addFilter(PyrogramErrorFilter())

# 文件处理器
# 使用 RotatingFileHandler，并强制 UTF-8 编码
# 必须设 maxBytes 才会轮转（默认 0 = 永不轮转，backupCount 失效）
fh = RotatingFileHandler(
    log_file,
    maxBytes=5 * 1024 * 1024,  # 单文件上限 5MB，超过滚动
    backupCount=5,             # 最多保留 5 个历史文件
    encoding="utf-8",          # 强制 UTF-8
)
fh.setFormatter(formatter)
fh.addFilter(PyrogramErrorFilter())
fh.addFilter(InfoAndAboveFilter())

# 检查是否已有处理器，避免重复添加
if not logger.handlers:
    logger.addHandler(ch)
    logger.addHandler(fh)

if not error_logger.handlers:
    error_logger.addHandler(ch)
    error_logger.addHandler(fh)


def log_group_error(group_id, error_msg, extra_info=""):
    """
    记录群组相关错误的便捷函数

    Args:
        group_id: 群组ID
        error_msg: 错误信息
        extra_info: 额外信息
    """
    # 将群组ID转换为群组名称（如果可能）
    group_name = get_group_name(group_id)
    full_msg = f"群组错误 - ID: {group_id} ({group_name}) - {error_msg}"
    if extra_info:
        full_msg += f" - 额外信息: {extra_info}"

    # 所有日志统一到主日志
    logger.error(full_msg)


def get_group_name(group_id):
    """
    根据群组ID获取群组名称
    优先从自定义群组名称 → PT_GROUP_ID 配置 → 返回群组ID
    """
    # 1. 尝试从自定义群组名称中获取
    try:
        from libs.state import state_manager
        group_names_str = state_manager.get_item("AUTO_LOTTERY", "custom_group_names", "{}")
        import json
        group_names = json.loads(group_names_str)
        if str(group_id) in group_names:
            return group_names[str(group_id)]
    except Exception:
        pass

    # 2. 尝试从配置文件中获取群组名称
    try:
        from config.config import PT_GROUP_ID
        # 遍历 PT_GROUP_ID 字典，查找匹配的群组ID
        for key, value in PT_GROUP_ID.items():
            if value == group_id:
                # 智能转换配置键为友好名称
                # 例如: "ZHUQUE_ID" -> "朱雀", "SSD_ID" -> "SSD", "BOT_MESSAGE_CHAT" -> "Bot消息"
                name = key.replace("_ID", "").replace("_CHAT", "")

                # 特殊处理一些常见名称
                name_mapping = {
                    "BOT_MESSAGE": "Bot消息",
                    "ZHUQUE": "朱雀",
                    "HONGYE": "红叶",
                    "AUDIENCES": "观众",
                    "AZUSA": "Azusa",
                    "DOLBY": "杜比",
                    "PTVICOMO": "PTVicomo",
                    "OPENCD": "OpenCD",
                    "HDSKY": "HDSky",
                    "MTEAM": "MTeam",
                    "FRDS": "FRDS",
                    "HDKYIN": "HDKyin",
                }

                # 如果有映射就用映射，否则直接用原名
                return name_mapping.get(name, name)
    except Exception:
        pass

    # 3. 如果都找不到，返回群组ID
    return f"群组 {group_id}"
