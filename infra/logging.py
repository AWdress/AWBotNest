"""
infra/logging.py
结构化日志配置 - structlog 24.x

替换 libs/log.py，提供：
- JSON 格式（Docker 环境）
- 彩色控制台（本地开发）
- 自动注入 timestamp / level / logger 字段
- 与现有 logging 标准库兼容（不破坏已有 logger 调用）
"""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(
    json_output: bool = False,
    log_level: str = "INFO",
) -> None:
    """
    配置 structlog 和标准 logging

    Args:
        json_output: True = JSON 格式（生产/Docker），False = 彩色控制台（开发）
        log_level: 日志级别（"DEBUG" / "INFO" / "WARNING" / "ERROR"）
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # 标准 logging 基础配置
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # 共用处理器链
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        # 生产环境：JSON 输出（Docker logs 友好）
        processors = [
            *shared_processors,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # 开发环境：彩色控制台
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    """获取 structlog BoundLogger（替代 logging.getLogger）"""
    return structlog.get_logger(name)


# 向后兼容：现有代码 `from libs.log import logger` 可迁移到
# `from infra.logging import get_logger; logger = get_logger(__name__)`
logger = get_logger(__name__)
