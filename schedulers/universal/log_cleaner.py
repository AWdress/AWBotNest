"""
日志清理定时任务
每天凌晨3点自动清理日志，只保留最后100行
"""

from pathlib import Path
from core import logger
from libs.log_cleaner_settings import (
    get_log_cleaner_settings,
    save_log_cleaner_settings,
    state_manager,
)


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
