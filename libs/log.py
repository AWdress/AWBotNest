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
# 容错：文件不可写（如挂载目录权限问题）时退回纯控制台，不让日志拖垮整个应用
fh = None
try:
    fh = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 单文件上限 5MB，超过滚动
        backupCount=5,             # 最多保留 5 个历史文件
        encoding="utf-8",          # 强制 UTF-8
    )
    fh.setFormatter(formatter)
    fh.addFilter(PyrogramErrorFilter())
    fh.addFilter(InfoAndAboveFilter())
except OSError as e:
    # 常见于 /app/logs 由 root 挂载、降权用户无写权限。
    # 打到 stderr 提示，但保证程序继续以控制台日志运行。
    print(f"[log] 文件日志不可用，退回控制台输出: {e}", file=sys.stderr)
    fh = None

# 检查是否已有处理器，避免重复添加
if not logger.handlers:
    logger.addHandler(ch)
    if fh is not None:
        logger.addHandler(fh)

if not error_logger.handlers:
    error_logger.addHandler(ch)
    if fh is not None:
        error_logger.addHandler(fh)


def log_group_error(group_id, error_msg, extra_info=""):
    """
    记录群组相关错误的便捷函数

    Args:
        group_id: 群组ID
        error_msg: 错误信息
        extra_info: 额外信息
    """
    # 直接使用群组 ID，不再尝试转换为名称
    full_msg = f"群组错误 - ID: {group_id} - {error_msg}"
    if extra_info:
        full_msg += f" - 额外信息: {extra_info}"

    # 所有日志统一到主日志
    logger.error(full_msg)


# AWBotHub 残留函数已删除：get_group_name() 无人调用
