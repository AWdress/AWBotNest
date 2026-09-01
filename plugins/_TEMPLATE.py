"""AWBotNest Telethon 插件模板；复制后将文件名与 __plugin__.id 改为同一值。"""

__plugin__ = {
    "id": "example",
    "name": "示例插件",
    "version": "2.0.0",
    "scope": "standalone",  # standalone | bot | user | both
    "description": "插件说明",
    "requirements": [],
    "config_schema": {
        "message": {"type": "string", "title": "消息内容", "required": True},
    },
    "resources": {
        "timeout_seconds": 120,
        "max_concurrency": 8,
        "max_background_tasks": 32,
        "failure_threshold": 5,
    },
}


async def setup(ctx):
    ctx.log.info("插件已加载")

    # Bot/用户插件可以注册 Telethon 事件：
    # @ctx.on_message(pattern=r"^/hello$")
    # async def hello(event):
    #     await event.reply(ctx.config.get("message", "Hello"))

    # 独立任务、Webhook 与控制台动作：
    # ctx.schedule_interval("refresh", refresh, seconds=300)
    # ctx.on_webhook("receive", receive_webhook)
    # ctx.action("run", run_action)


async def teardown(ctx):
    ctx.log.info("插件已停止")
