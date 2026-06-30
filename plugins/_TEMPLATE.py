# =============================================================================
# AWBotNest 插件模板 _TEMPLATE.py
#
# 复制此文件，改名为「你的插件id.py」（文件名即插件 ID，全局唯一），
# 然后修改 __plugin__ 元数据与 setup 逻辑即可。
#
# 两种形态（二选一）：
# - 单文件：plugins/<id>.py        ← 本模板，简单插件用这个
# - 文件夹：plugins/<id>/__init__.py  ← 复杂插件（带辅助模块/资源），
# __plugin__ 与 setup 写在 __init__.py，目录内可 from .xxx import ...
#
# 规则速记（完整规范见 SPEC.md / PLUGIN_GUIDE.md）：
# 1. 一个插件 = 一个文件或一个文件夹，文件名/目录名 = 插件 id。
# 2. 必须有 __plugin__ 字典，且 id 等于文件名/目录名。
# 3. 注册处理器一律用 ctx.on_message / ctx.on_callback，禁止 @Client.on_message。
# 4. 不要 import pyrogram / 全局 config / 内核内部模块；一切走 ctx。
# 5. 不要用 print，用 ctx.log。
# 6. 插件自带配置：所有参数写进 config_schema，前端「配置」按钮即生成 UI；
# 禁止往平台的 config 写业务配置。
# 7. 以 _ 开头的文件/目录不会被平台识别为插件（可用作私有辅助）。
# =============================================================================

# ① 元数据（平台静态解析此字典，必须是纯字面量）
__plugin__ = {
    "name": "示例插件",                 # 前端显示名
    "id": "_TEMPLATE",                  # 必须等于文件名/目录名（去掉 .py）。改名后记得同步！
    "version": "1.0.0",
    "author": "你的名字",
    "description": "这是一个插件模板，演示 setup/teardown、ctx 用法与配置 UI。",
    "icon": "",                         # 可选：图标 URL，前端卡片用；留空回退平台 logo
    "scope": "user",                    # user(用户账号) | bot(机器人账号) | both

    # 上传后是否默认启用
    "default_enabled": False,

    # ── 第三方依赖（可选）──
    # PEP 508 字符串列表，启用时由平台统一代装（不要在插件里自己调 pip）。
    # 建议用宽松范围（"httpx>=0.27"）而非钉死版本，减少与其它插件撞车；
    # 与已安装版本冲突时平台会拒绝启用并提示原因（单进程同一包不能多版本共存）。
    # 注意1：所声明的版本范围里必须有支持平台 Python（当前 3.13）的发行版，
    #        否则 pip 会判「无匹配版本」装失败。发布前用 `pip install "依赖" --dry-run` 验证。
    # 注意2：优先复用平台已装库（见 requirements.txt：ddddocr/httpx/aiohttp/Pillow 等），
    #        能复用就别声明——既免装又零冲突。
    # "requirements": ["httpx>=0.27"],

    # ── 配置项（前端「配置」按钮据此自动生成设置界面）──
    # 每个字段支持：
    # type:    string | password | number | boolean | select | multiselect | slider | text(多行)
    # default: 默认值（multiselect 用 list，slider/number 用数字）
    # label:   显示名
    # help:    字段下方说明文字（可选）
    # options: select/multiselect 的可选值，["a","b"] 或 [{"value":"a","label":"甲"}]
    # min/max/step: number / slider 用（可选）
    # section: 分区标题（同 section 归一组卡片）
    # show_if: 条件显示，如 {"enable_reply": True} —— 仅当该字段当前值匹配才显示本字段
    "config_schema": {
        # —— 功能开关区 ——
        "enable_reply": {
            "type": "boolean", "default": True, "label": "启用自动回复",
            "section": "功能开关", "help": "关闭后插件只监听不回复",
        },
        "enable_log": {
            "type": "boolean", "default": False, "label": "记录触发日志",
            "section": "功能开关",
        },
        # —— 参数区（仅在「启用自动回复」打开时显示，演示 show_if）——
        "keyword": {
            "type": "string", "default": ".ping", "label": "触发命令",
            "section": "参数", "help": "自己发出的、以此开头的消息会触发",
            "show_if": {"enable_reply": True},
        },
        "reply_text": {
            "type": "string", "default": "pong", "label": "回复内容",
            "section": "参数", "show_if": {"enable_reply": True},
        },
        "mode": {
            "type": "select", "default": "edit", "label": "回复方式",
            "options": [{"value": "edit", "label": "编辑原消息"}, {"value": "reply", "label": "回复"}],
            "section": "参数", "show_if": {"enable_reply": True},
        },
        "delay": {
            "type": "slider", "default": 0, "label": "回复延迟(秒)",
            "min": 0, "max": 10, "step": 1, "section": "参数",
        },
    },
}


# ② 启用时调用：在这里注册处理器
async def setup(ctx):
    cfg = ctx.config
    ctx.log.info("示例插件已启用，触发命令=%s", cfg.get("keyword"))

    @ctx.on_message(ctx.filters.outgoing & ctx.filters.text)
    async def on_text(client, message):
        c = ctx.config  # 每次读取，确保拿到前端最新配置
        kw = c.get("keyword", ".ping")
        if not message.text or not message.text.startswith(kw):
            return
        if c.get("enable_log"):
            ctx.log.info("触发：%s", message.text)
        if not c.get("enable_reply", True):
            return
        text = c.get("reply_text", "pong")
        if c.get("mode") == "reply":
            await message.reply(text)
        else:
            await message.edit(text)

    # 定时任务示例（可选）：注册后会出现在「系统状态」页的定时任务卡片，
    # 显示 任务名 · 所属插件 · 触发规则 · 下次运行时间。停用插件时自动取消。
    async def heartbeat():
        ctx.log.info("示例定时任务跑了一次")

    ctx.schedule(heartbeat, "interval", minutes=10, id="示例心跳")
    # 也可用 cron：ctx.schedule(daily, "cron", hour=9, minute=0, id="每日早报")


# ③ 停用时调用（可选）：释放自管理的资源。
# ctx.on_message / ctx.schedule 注册的东西由平台自动清理，无需手动处理。
async def teardown(ctx):
    ctx.log.info("示例插件已停用")
