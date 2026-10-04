"""AWBotNest Telethon 插件模板；复制后将文件名与 __plugin__.id 改为同一值。"""

__plugin__ = {
    "id": "example",
    "name": "示例插件",
    "version": "2.0.0",
    "scope": "standalone",  # standalone | bot | user | both | wecom
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

    # 企业微信专用插件可把 scope 改为 wecom，无需 Telegram 账号。
    # 先在系统通知设置中开启自建应用的消息回调并关联本插件。
    # @ctx.on_wecom_message(message_types=("image", "file", "event"), events=("click",))
    # async def receive_wecom(message):
    #     if message.message_type in {"image", "file"}:
    #         media = await message.download_media(max_bytes=10 * 1024 * 1024)
    #         # 校验 media.content 并交给本插件业务处理，不信任文件扩展名。
    #         await message.reply("文件已接收。")
    #     elif message.event_key == "run":
    #         # 按 message.user_id 隔离用户任务，并防止重复执行。
    #         await message.reply("按钮已收到。")


async def teardown(ctx):
    ctx.log.info("插件已停止")
