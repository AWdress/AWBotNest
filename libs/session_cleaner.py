# 标准库
import os
import sqlite3
from pathlib import Path

# 自定义模块
from libs.log import logger


def clean_corrupted_sessions(sessions_dir: str = "sessions"):
    """
    清理损坏的会话文件

    Args:
        sessions_dir: 会话文件目录
    """
    sessions_path = Path(sessions_dir)
    if not sessions_path.exists():
        logger.info(f"会话目录不存在: {sessions_path}")
        return

    cleaned_count = 0
    for session_file in sessions_path.glob("*.session"):
        try:
            # 尝试打开SQLite文件检查是否损坏
            conn = sqlite3.connect(str(session_file))
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            conn.close()

            if not tables:
                logger.warning(f"发现空的会话文件: {session_file}")
                session_file.unlink()
                cleaned_count += 1

        except sqlite3.DatabaseError as e:
            err_msg = str(e).lower()
            if "locked" in err_msg:
                logger.debug(f"会话文件正被锁定，跳过检查: {session_file}")
                continue

            logger.warning(f"发现损坏的会话文件: {session_file}, 错误: {e}")
            # 备份损坏的文件
            backup_file = session_file.with_suffix('.session.backup')
            try:
                session_file.rename(backup_file)
                cleaned_count += 1
            except Exception as rename_err:
                logger.error(f"重命名损坏的会话文件失败: {rename_err}")

        except Exception as e:
            logger.error(f"检查会话文件时出错: {session_file}, 错误: {e}")

    if cleaned_count > 0:
        logger.info(f"清理了 {cleaned_count} 个损坏的会话文件")
    else:
        logger.info("未发现损坏的会话文件")


def repair_session_file(session_file_path: str):
    """
    尝试修复单个会话文件

    Args:
        session_file_path: 会话文件路径
    """
    try:
        # 尝试使用SQLite的修复功能
        conn = sqlite3.connect(str(session_file_path))
        cursor = conn.cursor()

        # 检查数据库完整性
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()

        if result[0] == "ok":
            logger.info(f"会话文件完整: {session_file_path}")
        else:
            logger.warning(f"会话文件损坏: {session_file_path}, 完整性检查: {result[0]}")

        conn.close()

    except sqlite3.DatabaseError as e:
        logger.error(f"无法修复会话文件: {session_file_path}, 错误: {e}")
        return False
    except Exception as e:
        logger.error(f"修复会话文件时出错: {session_file_path}, 错误: {e}")
        return False

    return True


if __name__ == "__main__":
    # 如果直接运行此脚本，清理当前目录下的sessions文件夹
    clean_corrupted_sessions()
