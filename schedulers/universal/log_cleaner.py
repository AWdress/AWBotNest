"""
日志清理定时任务
每天凌晨3点自动清理日志，只保留最后100行
"""

from pathlib import Path
from core import logger
from libs.state import state_manager


def _bounded_int(value, default, minimum, maximum):
    try:
        return min(max(int(value), minimum), maximum)
    except (ValueError, TypeError):
        return default


def _enabled(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"on", "true", "1", "yes"}
    return default


def get_log_cleaner_settings():
    """返回清理设置，并修正旧配置或异常值。"""
    return {
        "enabled": _enabled(state_manager.get_item("SYSTEM", "log_cleaner_enabled", "on")),
        "keep_lines": _bounded_int(
            state_manager.get_item("SYSTEM", "log_keep_lines", 100), 100, 1, 1000
        ),
        "hour": _bounded_int(
            state_manager.get_item("SYSTEM", "log_clean_hour", 3), 3, 0, 23
        ),
        "minute": _bounded_int(
            state_manager.get_item("SYSTEM", "log_clean_minute", 0), 0, 0, 59
        ),
    }


def save_log_cleaner_settings(value):
    """保存维护页提交的日志清理设置。"""
    value = value if isinstance(value, dict) else {}
    settings = {
        "enabled": _enabled(value.get("enabled", True)),
        "keep_lines": _bounded_int(value.get("keep_lines"), 100, 1, 1000),
        "hour": _bounded_int(value.get("hour"), 3, 0, 23),
        "minute": _bounded_int(value.get("minute"), 0, 0, 59),
    }
    state_manager.set_section("SYSTEM", {
        "log_cleaner_enabled": "on" if settings["enabled"] else "off",
        "log_keep_lines": settings["keep_lines"],
        "log_clean_hour": settings["hour"],
        "log_clean_minute": settings["minute"],
    })
    return settings


def clean_log_file(log_file_path, keep_lines=100):
    """
    清理日志文件，只保留最后N行

    Args:
        log_file_path: 日志文件路径
        keep_lines: 保留的行数，默认100
    """
    try:
        log_path = Path(log_file_path)

        if not log_path.exists():
            return False

        # 读取所有行
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total_lines = len(lines)

        if total_lines <= keep_lines:
            return False

        # 只保留最后N行
        lines_to_keep = lines[-keep_lines:]

        # 写回文件
        with open(log_path, 'w', encoding='utf-8') as f:
            f.writelines(lines_to_keep)

        deleted_lines = total_lines - keep_lines
        logger.info(f"清理日志 {log_file_path}: 删除 {deleted_lines} 行，保留 {keep_lines} 行")

        return True

    except Exception as e:
        logger.error(f"清理日志文件失败 {log_file_path}: {e}")
        return False


async def log_cleaner_action():
    """日志清理任务"""
    try:
        # 检查开关
        settings = get_log_cleaner_settings()
        if not settings["enabled"]:
            return

        keep_lines = settings["keep_lines"]

        logger.info(f"开始清理日志，保留最后 {keep_lines} 行")

        # 日志文件列表
        log_files = [
            'logs/Mytgbot.log',
        ]

        cleaned_count = 0
        for log_file in log_files:
            if clean_log_file(log_file, keep_lines):
                cleaned_count += 1

        # 插件历史同时存在内存和磁盘中，必须通过统一入口同步清理。
        from webui.log_stream import trim_history
        if trim_history(keep_lines):
            cleaned_count += 1

        if cleaned_count > 0:
            logger.info(f"日志清理完成，共清理 {cleaned_count} 个文件")

    except Exception as e:
        logger.error(f"日志清理任务失败: {e}")


async def start_log_cleaner():
    """启动日志清理定时任务"""
    from schedulers import scheduler

    # 检查开关
    settings = get_log_cleaner_settings()

    # 移除旧任务（如果存在）
    if scheduler.get_job("log_cleaner"):
        scheduler.remove_job("log_cleaner")

    if settings["enabled"]:
        clean_hour = settings["hour"]
        clean_minute = settings["minute"]

        # 添加新任务
        scheduler.add_job(
            log_cleaner_action,
            "cron",
            hour=clean_hour,
            minute=clean_minute,
            id="log_cleaner"
        )
        logger.info(f"日志清理定时任务已启动（每天 {clean_hour}:{str(clean_minute).zfill(2)} 执行）")
    else:
        logger.info("日志清理定时任务已停止")
