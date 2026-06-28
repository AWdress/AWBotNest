"""
日志清理定时任务
每天凌晨3点自动清理日志，只保留最后100行
"""

from pathlib import Path
from core import logger
from libs.state import state_manager


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
        log_cleaner_enabled = state_manager.get_item("SYSTEM", "log_cleaner_enabled", "on")
        if log_cleaner_enabled != "on":
            return

        # 获取保留行数配置
        keep_lines_str = state_manager.get_item("SYSTEM", "log_keep_lines", "100")
        try:
            keep_lines = int(keep_lines_str)
        except (ValueError, TypeError):
            keep_lines = 100

        logger.info(f"开始清理日志，保留最后 {keep_lines} 行")

        # 日志文件列表
        log_files = [
            'logs/Mytgbot.log',
        ]

        cleaned_count = 0
        for log_file in log_files:
            if clean_log_file(log_file, keep_lines):
                cleaned_count += 1

        if cleaned_count > 0:
            logger.info(f"日志清理完成，共清理 {cleaned_count} 个文件")

    except Exception as e:
        logger.error(f"日志清理任务失败: {e}")


async def start_log_cleaner():
    """启动日志清理定时任务"""
    from schedulers import scheduler

    # 检查开关
    log_cleaner_enabled = state_manager.get_item("SYSTEM", "log_cleaner_enabled", "on")

    # 移除旧任务（如果存在）
    if scheduler.get_job("log_cleaner"):
        scheduler.remove_job("log_cleaner")

    if log_cleaner_enabled == "on":
        # 获取清理时间配置
        try:
            clean_hour = int(state_manager.get_item("SYSTEM", "log_clean_hour", "3"))
            clean_minute = int(state_manager.get_item("SYSTEM", "log_clean_minute", "0"))
        except (ValueError, TypeError):
            clean_hour = 3
            clean_minute = 0

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
