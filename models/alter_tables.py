# 标准库
import asyncio
from urllib.parse import quote_plus

# 第三方库
import aiomysql

# 自定义模块
from config.config import DB_INFO
from core import logger



# 数据库连接配置
password = quote_plus(DB_INFO['password'])

DB_CONFIG = {
    "host": DB_INFO['address'],     
    "port": DB_INFO['port'],
    "user": DB_INFO['user'],
    "password": DB_INFO['password'],
    "db": DB_INFO['db_name'],
    "autocommit": True,
}

# 需要修改的表和字段配置（表名, 字段名, 新字段类型）
ALTER_TASKS = [
    ("redpocket", "website", "varchar(32)"),
    ("redpocket", "gamemode", "varchar(32)"),
    ("redpocket", "bonus", "decimal(16,2)"),
    ("raiding", "website", "varchar(32)"),
    ("raiding", "action", "varchar(32)"),
    ("raiding", "bonus", "decimal(16,2)"),
    ("transform", "website", "varchar(32)"),
    ("transform", "bonus", "decimal(16,2)"),
    ("user_name", "name", "VARCHAR(32)")
]

async def alter_columns():
    conn = await aiomysql.connect(**DB_CONFIG)
    async with conn.cursor() as cursor:
        for table, column, new_type in ALTER_TASKS:
            sql = f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {new_type};"
            try:
                await cursor.execute(sql)
                logger.info(f"成功修改表 {table} 的字段 {column} 类型为 {new_type}")
            except Exception as e:
                logger.error(f"修改表 {table} 字段 {column} 时出错：{e}")
    conn.close()
