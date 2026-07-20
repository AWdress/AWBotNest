# AWBotNest 插件开发指南

面向开发者的插件编写规范。一个插件即一个独立可热插拔的功能单元，平台在运行时动态加载与卸载，无需重启进程，亦无需改动平台任何文件。

---

## 快速开始

1. 复制 `plugins/_TEMPLATE.py`，重命名为目标插件名（如 `my_feature.py`）。
2. 修改文件顶部的 `__plugin__` 元数据字典，其中 `id` 必须与文件名（去扩展名）一致。
3. 在 `setup(ctx)` 中注册处理器与任务。
4. 通过前端「上传插件」选择该文件，或直接置于 `plugins/` 目录。
5. 在插件列表中启用：处理器即时挂载；停用即时卸载。

## 插件形态

平台支持两种形态，自动识别；同名时单文件优先。

- **单文件**：`plugins/<id>.py`。适用于逻辑集中的插件。
- **目录包**：`plugins/<id>/__init__.py`。适用于需拆分模块或携带资源文件的插件。`__plugin__` 与 `setup` 定义于 `__init__.py`，包内可使用相对导入（`from .helper import xxx`）。目录名即插件 `id`。

## 插件结构

插件由三部分构成：元数据、`setup`、可选的 `teardown`。

```python
__plugin__ = {
    "name": "示例功能",            # 显示名
    "id": "my_feature",           # 必须等于文件名/目录名
    "version": "1.0.0",
    "author": "",
    "description": "功能说明",
    "changelog": "v1.0.0 初始版本\n- 实现基础功能\n- 添加配置项",  # 可选，版本更新说明
    "icon": "",                   # 可选，图标 URL；留空回退平台 logo
    "scope": "user",              # user | bot | both
    "default_enabled": False,
    "config_schema": {            # 可选，前端据此生成配置表单
        "keyword": {"type": "string", "default": "hello", "label": "触发词"},
    },
    "requirements": [             # 可选，第三方依赖；启用时由平台代装
        "httpx>=0.27",
    ],
}

async def setup(ctx):
    """启用时调用，在此注册处理器与定时任务。"""
    @ctx.on_message(ctx.filters.text)
    async def handler(client, message):
        if ctx.config["keyword"] in (message.text or ""):
            await message.reply("matched")

async def teardown(ctx):
    """停用时调用（可选），释放插件自行申请的资源。"""
    pass
```

`__plugin__` 为顶层字面量字典，平台通过静态 AST 解析读取，不执行插件代码。必填字段：`name`、`id`、`version`、`scope`。

`icon` 为可选的图标 URL，用于前端插件卡片（「我的插件」与「插件市场」）。留空则回退平台 logo。从 GitHub 仓库发布时，仓库 `manifest.json` 中的 `icon` 用于市场展示（见下文），二者一致即可。

`changelog` 为可选的版本更新说明，用户可以从插件卡片右下角的三点菜单打开“版本历史”。建议用白话说明新增了什么、修复了什么，以及升级后是否需要重新设置。支持多行文本（用 `\n` 换行）。更新插件时请同步更新 `version` 和 `changelog`，帮助用户看懂升级内容。示例：

```python
"version": "1.2.0",
"changelog": "v1.2.0 更新内容：\n- 新增自动重试功能\n- 修复特殊字符导致的崩溃\n- 优化响应速度",
```

`requirements` 为可选的第三方依赖列表（PEP 508 字符串）。**不要在插件里自己调 pip**——只声明，平台在启用时统一代装。建议用宽松范围（`"httpx>=0.27"`）而非钉死版本，以减少与其它插件/平台依赖撞车。若与已安装版本冲突，平台会拒绝启用并提示原因（插件运行在单进程内，同一个包无法多版本共存）。

声明依赖前请注意：

- **必须有支持平台 Python 版本（当前 3.13）的发行版**。不少包用 `Requires-Python` 卡了上限，pypi 上虽有版本号，但 pip 在 3.13 上会判「无匹配版本」装失败。声明前先在 3.13 环境跑 `pip install "你的依赖" --dry-run` 验证能解出版本。（典型反例：`rapidocr_onnxruntime>=1.3` 全系列标 `<3.13`，装不上；应换支持 3.13 的 `rapidocr>=2`。）
- **优先复用平台已装的库**（见仓库 `requirements.txt`）：OCR 用 `ddddocr`、HTTP 用 `httpx`/`aiohttp`、图像用 `Pillow`、解析用 `beautifulsoup4`/`lxml` 等。能复用就不声明，既免装又零冲突。
- **出站请求自动走平台代理**：系统设置里启用代理后，平台会导出 `HTTP(S)_PROXY`/`ALL_PROXY` 环境变量，`httpx`/`requests`/`aiohttp`（默认 `trust_env=True`）会自动走代理，插件无需手动配置。`localhost`/`127.0.0.1` 已排除。如手动关闭了 `trust_env`，请自行读取这些环境变量。

---

## ctx 接口

`ctx` 是平台注入的能力上下文。插件的全部平台交互均通过 `ctx` 完成，不得直接 `import pyrogram` 或 `config`。

### 注册消息处理器

```python
@ctx.on_message(ctx.filters.text)
async def h(client, message):
    await message.reply("ok")

@ctx.on_message(ctx.filters.outgoing & ctx.filters.text, group=-10)
async def h2(client, message):
    ...
```

常用过滤器：`ctx.filters.text`、`ctx.filters.photo`、`ctx.filters.command("xxx")`、`ctx.filters.outgoing`、`ctx.filters.incoming`，支持 `&`、`|`、`~` 组合。

`group` 为本插件内部多个处理器之间的相对执行优先级，数值越小越先执行。在处理器中 `raise ctx.StopPropagation` 可阻止该消息被后续处理器继续处理。

`on_message` / `on_edited_message` / `on_callback` 还接受 `target` 参数，决定处理器挂载到哪类账号：`"auto"`（默认，按插件 `scope` 选择）、`"user"`、`"bot"`、`"both"`。`scope` 为 `both` 时可借此将不同处理器分别挂到用户账号或机器人账号。

### 注册编辑消息处理器

用法、参数（`filter` / `group` / `target`）与 `on_message` 完全一致，但**只在消息被编辑时触发**，`on_message` 收不到。适用于「先发消息再编辑来送达最终结果」的 bot（如某些大额转账确认）。

```python
@ctx.on_edited_message(ctx.filters.text)
async def on_edit(client, message):
    ...
```

### 注册回调处理器

```python
@ctx.on_callback(ctx.filters.regex("^my_btn$"))
async def on_click(client, callback_query):
    await callback_query.answer("ok")
```

### 发送消息

```python
await ctx.bot.send(chat_id, "text")
await ctx.user.send(chat_id, "text")
await ctx.bot.send_photo(chat_id, "url_or_path")
```

- `ctx.bot`：机器人账号发送代理。
- `ctx.user`：用户账号发送代理（取首个已连接）。
- `ctx.user_apps`：已连接用户账号的列表，多账号插件需逐个操作时使用。
- 目标账号未连接时，对应代理的发送方法抛 `RuntimeError`；可先判 `ctx.bot.connected` / `ctx.user.connected`。

> **多 Bot**：平台可配置多个 Bot，并在「系统设置 → 通知」为每个插件指定用哪个 Bot（默认=默认 Bot）。这对插件是**透明**的——`ctx.bot`、`ctx.notify`、`scope=bot` 的 handler 会自动走平台为本插件分配的 Bot，插件代码无需改动、也不要自己选 Bot。

### 通知平台管理员

监控、定时、告警类插件需向平台管理员推送时，调用 `ctx.notify` 提交给平台。平台负责分类、附加插件名与级别标签、统一格式与投递，插件无需关心收件人与格式。

```python
await ctx.notify("有新订单")
await ctx.notify("磁盘空间不足", level="warning")
await ctx.notify("任务失败", level="error", category="备份")

@ctx.on_message(ctx.filters.text)
async def h(client, message):
    await ctx.notify("已抢到红包", account=client)
```

- `level`：`info` / `success` / `warning` / `error`，平台按级别加标签。
- `category`：可选业务分类（如「订单」「签到」），显示于标签中。
- `account`：多账号场景下传入处理器收到的 `client`，平台自动标注来源账号名。
- 平台优先经 Bot 私聊管理员（需管理员已 `/start` 过 Bot），不可用时回退至主账号收藏夹；每条通知同时写入运行日志。
- 具体走哪个 Bot 由平台按插件分配（见上「多 Bot」），插件无需关心。
- 推送通知一律走 `ctx.notify`，不要自行调用 `ctx.bot.send` 实现。

若需管理员的 Telegram 数字 ID（如直接发送至特定会话），用 `ctx.owner_id`（无主账号时为 `0`）。

### 读取配置

`config_schema` 中定义的字段，读取方式如下，每次读取均为用户保存的最新值：

```python
kw = ctx.config["keyword"]
on = ctx.config.get("enabled", True)
```

### 写回自己的配置

`ctx.update_config(patch)` 把结果写回本插件配置（局部合并，只改传入的键）。**不触发重载**，下次读 `ctx.config` 即最新值。常用于持久化运行状态，或把状态回填到 `info` 字段供前端展示：

```python
ctx.update_config({"last_run": "2026-07-15 10:00", "count": 5})
```

与用户在前端手动保存互不冲突，谁后写谁生效。存运行内部状态优先用 `ctx.kv`；只有想让状态在配置表单里显示时才用它。

### 动作按钮

在 `config_schema` 放一个 `action` 字段，用 `ctx.action(name)` 注册同名处理器，管理员在配置弹窗点按钮即可触发（如「测试连接」「立即运行一次」）：

```python
"config_schema": {"test": {"type": "action", "label": "测试连接", "action": "test"}}

async def setup(ctx):
    @ctx.action("test")
    async def _test():
        ok = await do_check()
        return {"ok": ok, "message": "连接正常" if ok else "连不上"}   # 返回值弹给管理员
```

处理函数无参；返回 `dict`（含 `ok`/`message`）、`str`（当作提示文字）或 `None`（视为成功）。由已登录管理员触发，插件须已启用。`danger: True` 的按钮点击前会弹确认框。

### 下载消息媒体

`ctx.download(message, subdir=None)` 把消息里的图片/文件/视频下载到本插件目录，返回落盘 `Path`：

```python
@ctx.on_message(ctx.filters.photo)
async def h(client, message):
    path = await ctx.download(message, subdir="imgs")   # data/plugin_data/<id>/imgs/xxx
    text = await ocr(path)
```

消息无可下载媒体时抛 `ValueError`。

### 浏览器自动化

需要渲染 JS 页面、过反爬/指纹检测、抓动态内容时，用 `ctx.browser`——平台已托管浏览器，插件无需自己装。引擎优先 **CloakBrowser**（停用 Chromium，过 Cloudflare/指纹检测），不可用时自动回退 **Playwright Chromium**，插件无感。

```python
# 取渲染后的 HTML 源码
html = await ctx.browser.page_source("https://example.com", timeout=60)

# 需要交互时，传一个同步 action(page)，在浏览器线程里执行、返回结果
def grab(page):
    page.click("#more")
    return page.inner_text("#list")
data = await ctx.browser.run("https://example.com", grab, headless=True)
```

- 两个方法都是 `async`（内部在线程里跑同步浏览器 API，不阻塞事件循环）。
- 参数：`cookies`（`"k=v; k2=v2"` 请求头串）、`ua`（User-Agent）、`headless`（默认 `True`）、`timeout`（秒）、`proxy`。
- `ctx.browser.run` 的 `action` 是**同步函数**，收到同步 `page` 对象（`goto`/`click`/`fill`/`content`/`inner_text`/`screenshot` 等），页面用完平台自动关闭。
- `ctx.browser.engine` 返回当前引擎名（`"cloakbrowser"` / `"playwright"` / `None`）。
- 为减小镜像体积，浏览器内核不随镜像发布，也不在启动时下载：**插件首次调用 `ctx.browser` 时**才下载到 `data/browser_cache`（随卷持久化，容器重建不必重下）。所以首次调用会多花一次下载时间，之后就快了；不用浏览器的部署零开销。出站默认走平台代理。

### 键值存储

每个插件拥有独立命名空间，互不干扰。

```python
ctx.kv.set("count", 10)
n = ctx.kv.get("count", 0)
ctx.kv.delete("count")
ctx.kv.keys()
```

### 文件存储

`ctx.kv` 仅存键值。存储实际文件使用 `ctx.data_dir`，为本插件独享的可写目录（`Path`，首次访问自动创建）：

```python
p = ctx.data_dir / "avatars" / "a.jpg"   # data/plugin_data/<id>/avatars/a.jpg
p.parent.mkdir(parents=True, exist_ok=True)
p.write_bytes(img_bytes)
```

### 日志

使用 `ctx.log`，不要使用 `print`。日志自动附加 `[<id>]` 前缀，可在「运行日志」页按插件名检索与过滤。

```python
ctx.log.info("processed one message")
ctx.log.warning("unexpected: %s", err)
ctx.log.error("failed: %s", err)
```

### 定时任务

任务在插件停用时自动取消。

```python
ctx.schedule(tick, "interval", seconds=60)
ctx.schedule(tick, "cron", hour=3, minute=0)
ctx.schedule(daily_report, "cron", hour=9, id="每日早报")
```

任务 `id` 自动附加 `<id>::` 前缀以归属到本插件；不传 `id` 时默认取函数名。已注册任务展示于「系统状态」页（任务名、所属插件、触发规则、下次运行时间）。

### Webhook（接收外部回调）

需要接收外部服务回调（如媒体服务器事件推送）的插件，可在 `__plugin__` 声明 `"webhook": True`，并用 `ctx.on_webhook` 注册处理器：

```python
__plugin__ = { ..., "webhook": True }

async def setup(ctx):
    @ctx.on_webhook
    async def on_hook(req):
        data = req.json or {}          # 解析出的 JSON（非 JSON 为 None）
        await ctx.notify(f"收到事件：{data}", category="Webhook")
        return {"ok": True}            # dict→JSON / str→文本 / None→{"ok": true}
```

声明后，在插件「配置」弹窗的 Webhook 区即可看到本插件的入站地址（每个插件路径不同）：

```
http(s)://<平台地址>/api/v1/plugin/<插件id>/webhook?apikey=<密钥>
```

其中 `apikey` 是**平台统一的 Webhook 密钥**，在「系统设置 → 通知 → 平台 Webhook」点「随机」生成一次即可，所有插件与平台 webhook 共用，不为每个插件单独生成。处理器收到的 `req` 是 `WebhookRequest`：`req.method` / `req.query`（已剔除 apikey）/ `req.headers`（键小写）/ `req.json` / `req.text` / `req.body`。一个插件一个处理器，停用/重载时自动失效。仅当插件已启用、已注册处理器且平台已生成密钥时，webhook 才会响应。

> 若只是想把外部内容推给管理员而不写插件，用**平台级 webhook**：在「系统设置 → 通知」生成密钥，POST 到 `…/api/v1/webhook?apikey=<密钥>` 即可。

### 资源清理

通过 `ctx.on_message`、`ctx.on_edited_message`、`ctx.on_callback`、`ctx.schedule` 注册的处理器与任务由平台在停用时自动清理，无需手动处理。若插件自行申请了其它资源（连接、文件句柄、外部客户端等），用 `ctx.add_cleanup(fn)` 注册清理回调（停用时调用），或在 `teardown(ctx)` 中释放。

```python
async def setup(ctx):
    conn = open_something()
    ctx.add_cleanup(conn.close)
```

---

## config_schema

平台依据字段类型在前端自动生成配置表单，对应插件卡片的「配置」入口。

```python
"config_schema": {
    "enable_x":    {"type": "boolean", "default": True,  "label": "启用X功能", "section": "功能开关"},
    "enable_y":    {"type": "boolean", "default": False, "label": "启用Y功能", "section": "功能开关"},
    "text_field":  {"type": "string",  "default": "",    "label": "文本", "section": "参数", "help": "说明", "show_if": {"enable_x": True}},
    "secret":      {"type": "password","default": "",    "label": "密钥", "section": "参数"},
    "number_field":{"type": "number",  "default": 0,     "label": "数字", "section": "参数", "min": 0, "max": 100},
    "volume":      {"type": "slider",  "default": 5,     "label": "滑块", "section": "参数", "min": 0, "max": 10, "step": 1},
    "choice":      {"type": "select",  "default": "a",   "label": "单选", "options": ["a","b","c"], "section": "参数"},
    "tags":        {"type": "multiselect", "default": [], "label": "多选", "options": ["x","y","z"], "section": "参数"},
    "long_text":   {"type": "text",    "default": "",    "label": "多行文本", "section": "参数"},
    "required_key":{"type": "password","default": "",    "label": "密钥", "section": "参数", "required": True},
    "items":       {"type": "list",    "default": [],    "label": "列表", "section": "参数",
                    "item_label": "项",
                    "fields": {
                        "name":    {"type": "string",  "label": "名称"},
                        "value":   {"type": "string",  "label": "值"},
                        "tags":    {"type": "multiselect", "label": "标签", "options": ["a","b","c"], "default": []},
                        "enabled": {"type": "boolean", "label": "启用", "default": True},
                    }},
    # 会话选择器：从账号的群/频道/私聊里挑，存会话 id（multi=True 存 id 数组）
    "target":      {"type": "chat",    "default": 0,     "label": "转发到", "section": "会话",
                    "multi": False, "chat_types": ["group", "channel"]},
    # 只读展示：显示 text；不写 text 则显示该键当前值（可由 ctx.update_config 写入当状态看）
    "tip":         {"type": "info",    "label": "使用说明", "text": "先填密钥再启用", "section": "会话"},
    "last_run":    {"type": "info",    "default": "",    "label": "上次运行", "section": "会话"},
    # 动作按钮：点击触发插件用 ctx.action("test") 注册的函数
    "test":        {"type": "action",  "label": "测试连接", "action": "test", "section": "会话"},
}
```

字段属性：

| 属性 | 说明 |
|------|------|
| `type` | `string` / `password` / `number` / `boolean` / `select` / `multiselect` / `slider` / `text` / `list` / `chat` / `action` / `info` |
| `default` | 默认值（必填。`multiselect`/`list` 为列表，`slider`/`number` 为数字） |
| `label` | 显示名 |
| `help` | 字段下方说明文字（可选） |
| `options` | `select`/`multiselect` 候选项，`["a","b"]` 或 `[{"value":"a","label":"甲"}]` |
| `min`/`max`/`step` | `number`/`slider` 取值约束（可选）。`min`/`max` 同时用于保存前校验 |
| `required` | 保存前校验：为 `True` 时该项不能为空，否则前端拦下不保存（`info`/`action` 不校验） |
| `section` | 分区标题（可选）。同一 `section` 的字段在表单中归为一组 |
| `order` | 排序权重（可选，数字）。同一 `section` 内数字越小越靠前，未指定的排最后 |
| `cols` | 栅格列数（可选，1-12）。控制字段占用宽度，12=整行，6=半行，4=三分之一行 |
| `show_if` | 条件显示，如 `{"enable_x": True}`：仅当该字段当前值匹配时显示 |
| `fields` | `list` 专用：每行的子字段 `{子键: 子 spec}`，子字段可用上述基础类型 |
| `item_label` | `list` 专用：每行标题前缀（默认「项」），如显示为「规则 1」「规则 2」 |
| `multi` | `chat` 专用：`True` 为多选（存会话 id 数组），默认单选（存单个 id） |
| `chat_types` | `chat` 专用：过滤会话类型，取值 `private`/`bot`/`group`/`channel`，如 `["group","channel"]` |
| `session` | `chat` 专用：指定用哪个账号枚举会话；不填取插件应用范围内首个已连接用户账号 |
| `text` | `info` 专用：要展示的固定文字；不填则显示该键的当前值 |
| `action` | `action` 专用：动作名，须与插件里 `ctx.action("名字")` 注册的一致 |
| `danger` | `action` 专用：`True` 时点击前弹确认框（用于有风险的操作） |

- `list` 取值为 list-of-dict，`ctx.config["items"]` 直接拿到 `[{"name":..., ...}, ...]` 遍历即可。行内子字段暂不支持 `show_if`，也不建议 `list` 再嵌套 `list`。
- `chat` 取值为会话 id（`multi` 时为 id 数组），插件里 `ctx.config["target"]` 直接当 chat_id 用。管理员没连账号时可手填 id 兜底。
- `info` 只展示不收集输入。要显示动态状态（如「上次运行时间」），让插件用 `ctx.update_config` 写回同名键即可。

### 表单布局控制

配置弹窗采用 **12 列栅格系统**（桌面约 1000px 宽，窄屏自动全屏），插件开发者可以通过 `cols` 和 `order` 精确控制字段排版：

**自动布局（默认）**：
- 未指定 `cols` 时，大字段（`text`/`list`/`multiselect`/`chat`）自动占 12 列（整行）
- 短字段（`string`/`password`/`number`/`boolean`/`select`/`slider`）自动占 6 列（半行，两两并排）

**自定义布局**：
```python
"config_schema": {
    # 三个字段并排成一行（每个占 4 列，即三分之一行）
    "enable":   {"type": "boolean", "label": "启用", "cols": 4, "order": 1},
    "interval": {"type": "number",  "label": "间隔", "cols": 4, "order": 2},
    "max":      {"type": "number",  "label": "最大", "cols": 4, "order": 3},
    
    # 8 + 4 列组合（三分之二 + 三分之一）
    "api_url":  {"type": "string", "label": "API地址", "cols": 8, "order": 4},
    "timeout":  {"type": "number", "label": "超时",     "cols": 4, "order": 5},
    
    # 半行并排
    "mode":     {"type": "select", "label": "模式",   "cols": 6, "options": ["a","b"]},
    "level":    {"type": "select", "label": "级别",   "cols": 6, "options": ["1","2"]},
    
    # order 控制排序（数字越小越靠前）
    "important": {"type": "string", "label": "重要配置", "order": 1},  # 排在最前
    "optional":  {"type": "string", "label": "可选配置", "order": 99}, # 排在最后
}
```

**移动端适配**：窄屏（≤768px）自动回退单列布局，`cols` 设置失效，所有字段占满宽。

参考示例插件 `plugins/_EXAMPLE_LAYOUT.py` 查看完整演示。

插件的全部配置均通过 `config_schema` 声明，不得修改平台配置。

---

## Vue 模式（自带配置界面）

`config_schema` 生成的是标准表单，覆盖绝大多数插件。若需要图表、可视化编辑器或非表单式的复杂交互，可改用 **Vue 模式**：插件自带一个用 Vue 写的配置界面，平台在运行时把它加载进配置弹窗（基于模块联邦 Module Federation）。

起步最快的方式是复制模板目录 `plugins/_TEMPLATE_VUE/`，去掉下划线改成你的插件 id。

### 开启

仅**目录包**插件可用（需自带前端工程）。在 `__plugin__` 声明：

```python
__plugin__ = {
    "name": "我的插件", "id": "my_plugin", "version": "1.0.0", "scope": "user",
    "render_mode": "vue",     # ← 配置界面改由插件自带的 Vue 组件渲染
    # vue 模式无需 config_schema
}
```

`render_mode` 缺省为 `"schema"`（走自动表单）。写 `"vue"` 时目录下须有一个前端工程 `frontend/`。

### 前端工程

目录结构（模板已备好）：

```
my_plugin/
├── __init__.py
└── frontend/
    ├── package.json
    ├── vite.config.js        # 模块联邦：暴露 ./Config，共享宿主 Vue
    ├── src/Config.vue        # ← 你的配置界面（必须暴露为 ./Config）
    └── dist/                 # 构建产物，发布时一并提交
```

关键约定：

- `vite.config.js` 用 `@originjs/vite-plugin-federation` 暴露 `./Config`，并把 `vue` 声明为 `shared`（`generate: false`）——复用平台那一份 Vue，不重复打包。
- 平台加载的是**构建产物**：发布前必须 `cd frontend && npm install && npm run build`，并把 `frontend/dist/` 一起提交/打包。产物入口为 `frontend/dist/assets/remoteEntry.js`；未构建时配置弹窗会提示「未随附前端构建产物」。

### 组件契约

平台加载 `Config.vue` 时注入两个 prop：

```js
const props = defineProps({ pluginId: String, host: Object })

// host 提供的能力：
await props.host.getConfig()                 // 读取已保存配置（对象）
await props.host.saveConfig(values)          // 保存配置（存平台统一存储，插件里 ctx.config 可读到）
await props.host.callApi('/ping')            // 调用插件后端接口（见下）
await props.host.callApi('/echo', { method: 'POST', body: {...} })
props.host.toast.success('已保存')            // 弹平台提示（success / error）
```

vue 模式下配置弹窗不再显示平台的「保存」按钮——由你的组件自己调用 `host.saveConfig`。配置值仍存平台统一存储，插件里照常用 `ctx.config` 读取。

平台给 vue 界面的画布：桌面约 1000px 宽、窄屏（≤768px）自动全屏。请用响应式布局（百分比 / 栅格 / 媒体查询）适配，让界面能在这块画布里铺开，不要写死过窄或过宽的固定尺寸，否则窄屏会溢出。

### 后端接口 ctx.on_api

前端要读写的业务数据，用 `ctx.on_api` 在 `setup` 里注册接口，前端用 `host.callApi` 调：

```python
async def setup(ctx):
    @ctx.on_api("/ping", methods=["GET"])
    async def ping(req):
        return {"ok": True, "message": "pong"}

    @ctx.on_api("/save_rule", methods=["POST"])
    async def save_rule(req):
        data = req.json or {}          # 前端 body
        ctx.kv.set("rules", data.get("rules", []))
        return {"ok": True}
```

- 实际地址 `/api/plugins/<id>/api/<path>`，经**管理员登录态鉴权**（外部访问不到，与 webhook 的 apikey 公开端点不同）。
- `req` 是 `WebhookRequest`：`req.method` / `req.query` / `req.headers` / `req.json` / `req.text` / `req.path`。
- 返回 `dict`→JSON、`str`→文本、`None`→`{"ok": true}`。
- 同一 `(方法, 路径)` 重复注册后者覆盖前者；停用/重载时自动失效。

### 鉴权与安全

- **前端产物**（`/api/plugins/<id>/fe/...`）由登录时下发的**资源 Cookie** 鉴权（ESM 动态 import 带不了 Authorization 头，故用同源自动携带的 Cookie）。未登录会话无法下载插件前端，公网也拿不到。
- **`ctx.on_api` 接口**需管理员 **Bearer 令牌**（与其它控制台接口同级）。
- Vue 组件一旦加载，就运行在管理员后台的同一上下文里、能读到令牌——这是模块联邦（非沙箱隔离）的固有特性；加之插件的 Python 启用后即在服务端全权限运行。**因此务必只安装可信来源的插件。**

---

## scope

| scope | 处理器挂载目标 | 适用场景 |
|-------|---------------|---------|
| `user` | 用户账号 | 监听群消息、自动抢红包、自动抽奖等 |
| `bot` | 机器人账号 | 菜单、命令、面向用户应答 |
| `both` | 两者 | 需双端响应的功能 |

---

## 约束

1. 一个文件对应一个插件，文件名即 `id`，全局唯一。
2. 不得 `import pyrogram` / `config` / 内核模块，全部能力经 `ctx` 获取。
3. 不得使用 `@Client.on_message`，须使用 `@ctx.on_message`，否则无法热卸载。
4. 不得使用 `print`，须使用 `ctx.log`。
5. 插件之间不得相互 import。共用逻辑应抽为 `_` 前缀的辅助文件，或下沉至平台。
6. `_` 前缀的文件不被识别为插件（用作模板或辅助模块）。

---

## 发布到 GitHub 仓库

将插件置于 GitHub 仓库即可在他人平台的「插件市场」中出现。推荐在仓库根目录提供 `manifest.json`（含版本号，平台据此判定更新）：

```json
{
  "my_feature": {"name":"示例功能","version":"1.0.0","author":"","description":"...","icon":"https://.../i.png","path":"my_feature.py"},
  "big_plugin": {"name":"大型插件","version":"2.0.0","path":"big_plugin/"}
}
```

- key 为插件 `id`；`path` 单文件以 `.py` 结尾，目录包以 `/` 结尾。
- 无 `manifest.json` 时，将 `<id>.py` 或 `<id>/__init__.py` 置于仓库根或 `plugins/` 目录，平台自动扫描。

---

## 故障排查

- **插件标红**：依据错误提示排查，常见原因为缺少 `__plugin__`、`id` 与文件名不一致、`scope` 非法或语法错误。
- **代码改动生效**：前端点击「重载」，或停用后重新启用。
- **插件异常的影响范围**：单个插件加载失败仅标记该插件，不影响其它插件与平台运行。
- **数据存储位置**：`ctx.kv` 数据位于 `data/kv/<id>.sqlite`，文件位于 `data/plugin_data/<id>/`，按插件隔离。

---

完整硬性规范见 `SPEC.md`；可参照 `plugins/_TEMPLATE.py` 起步。
