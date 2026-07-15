# =============================================================================
# AWBotNest 「Vue 模式」插件模板  _TEMPLATE_VUE/__init__.py
#
# 复制整个目录，改名为「你的插件id」（目录名即插件 ID，全局唯一），
# 修改 __plugin__ 与 setup，并在 frontend/ 里写你自己的配置界面。
#
# 与普通插件的唯一区别：配置界面不再由平台按 config_schema 自动生成，
# 而是由你用 Vue 自己写（frontend/src/Config.vue），平台在运行时把它加载进后台。
# 适合需要图表、复杂交互、非表单式界面的插件。普通表单请继续用 _TEMPLATE.py。
#
# 三件事：
#   1. __plugin__ 里声明 "render_mode": "vue"（本模板已声明）。
#   2. frontend/ 是一个独立的 Vite + 模块联邦工程，构建后产物在 frontend/dist/。
#      发布前必须先 `cd frontend && npm install && npm run build`，
#      并把 frontend/dist/ 一起提交/打包——平台加载的是构建产物，不是源码。
#   3. 前端要读写的数据，用 ctx.on_api 在这里注册后端接口，前端 host.callApi 调用。
#
# 完整说明见 docs/PLUGIN_GUIDE.md 的「Vue 模式（自带配置界面）」一节。
# =============================================================================

import time

__plugin__ = {
    "name": "Vue 模式示例",
    "id": "_TEMPLATE_VUE",           # 必须等于目录名
    "version": "1.0.0",
    "author": "你的名字",
    "description": "演示插件自带 Vue 配置界面（模块联邦）与 ctx.on_api 后端接口。",
    "icon": "",
    "scope": "user",
    "default_enabled": False,

    # ★ 关键：声明用 Vue 联邦组件渲染配置界面（仅目录包插件可用）。
    #   未声明或写 "schema" 时，走 config_schema 自动表单（见 _TEMPLATE.py）。
    "render_mode": "vue",

    # vue 模式下不需要 config_schema——界面全由 frontend/src/Config.vue 决定。
    # 配置值仍存平台统一存储，前端用 host.getConfig()/host.saveConfig() 读写，
    # 插件里用 ctx.config 读取，与普通插件完全一致。
}


async def setup(ctx):
    ctx.log.info("Vue 模式示例已启用")

    # ── 给前端用的后端接口（ctx.on_api）──
    # 前端 host.callApi('/ping') / host.callApi('/echo', {method:'POST', body}) 调用。
    # 实际地址 /api/plugins/<id>/api/<path>，经管理员登录态鉴权，外部访问不到。
    # req 是 WebhookRequest：req.method / req.query / req.json / req.text / req.path。
    # 返回 dict→JSON / str→文本 / None→{"ok": true}。

    @ctx.on_api("/ping", methods=["GET"])
    async def ping(req):
        return {"ok": True, "message": "pong", "server_time": int(time.time())}

    @ctx.on_api("/echo", methods=["POST"])
    async def echo(req):
        data = req.json or {}
        ctx.log.info("收到前端 echo：%s", data)
        # 演示：把前端传来的一个值累加进 kv 计数
        n = int(ctx.kv.get("echo_count", 0)) + 1
        ctx.kv.set("echo_count", n)
        return {"ok": True, "received": data, "echo_count": n}

    @ctx.on_api("/stats", methods=["GET"])
    async def stats(req):
        return {"echo_count": int(ctx.kv.get("echo_count", 0)), "config": ctx.config}


async def teardown(ctx):
    ctx.log.info("Vue 模式示例已停用")
