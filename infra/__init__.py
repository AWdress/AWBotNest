"""
infra/__init__.py
基础设施包

包含：
- config.py: pydantic-settings 统一配置
- container.py: DI 容器（dependency-injector）
- logging.py: structlog 日志配置
- scheduler.py: APScheduler 定时任务注册表
- task_registry.py: anyio TaskGroup 封装
- health.py: 健康检查
"""
