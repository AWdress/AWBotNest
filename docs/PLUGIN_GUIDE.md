# AWBotNest 2 插件开发指南

AWBotNest 插件是可以独立安装、启用、停用和重载的功能单元。平台提供 Telegram、HTTP、Cookie、浏览器、AI、KV、通知、Webhook 和调度能力。

AWBotNest 2 使用 Telethon，仅支持按 V2 规范开发的插件。

## 快速开始

1. 复制 `plugins/_TEMPLATE.py`。
2. 将文件改名为插件 ID，例如 `my_feature.py`。
3. 把 `__plugin__["id"]` 改成同一个 ID。
4. 在 `setup(ctx)` 注册事件或任务。
5. 在管理页面上传，或把文件放入 `plugins/`。
6. 启用插件；修改后点击“重载”即可生效。

```python
__plugin__ = {
    "id": "hello_world",
    "name": "Hello World",
    "version": "1.0.0",
    "scope": "bot",
    "description": "回复 /hello",
}

async def setup(ctx):
    @ctx.on_message(pattern=r"^/hello$")
    async def hello(event):
        await event.reply("Hello World")

async def teardown(ctx):
    ctx.log.info("插件已停止")
```

## 插件形态

- 单文件：`plugins/<id>.py`
- 目录包：`plugins/<id>/__init__.py`

目录包适合拆分模块和携带资源。`id` 必须与文件名或目录名一致；以下划线开头的文件和目录不会被识别为插件。

## 元数据

`__plugin__` 必须是入口文件顶层的字面量字典，平台通过 AST 静态读取。

```python
__plugin__ = {
    "id": "my_feature",
    "name": "我的功能",
    "version": "1.0.0",
    "author": "作者",
    "description": "一句话说明功能",
    "tags": ["自动化", "消息处理", "数据统计"],
    "render_mode": "schema",
    "scope": "standalone",
    "bot": "",
    "requirements": ["httpx>=0.28,<1"],
    "config_schema": {},
    "resources": {
        "timeout_seconds": 120,
        "max_concurrency": 8,
        "max_background_tasks": 32,
        "failure_threshold": 5,
    },
}
```

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `id` | 是 | 唯一 ID，与入口名称一致 |
| `name` | 是 | 用户看到的名称 |
| `version` | 是 | 推荐语义化版本 |
| `scope` | 是 | `standalone`、`bot`、`user`、`both` |
| `description` | 否 | 面向用户的功能说明 |
| `author` | 否 | 插件卡片中 GitHub 图标旁显示的作者名 |
| `tags` | 否 | 功能标签字符串列表；建议 1–4 个，每项不超过 24 个字符 |
| `bot` | 否 | 指定 Bot ID；留空使用默认 Bot |
| `requirements` | 否 | 启用前安装的 Python 依赖 |
| `config_schema` | 否 | 自动配置表单 |
| `render_mode` | 否 | `schema`（默认）或 `vue`；Vue 模式通过模块联邦加载插件配置组件 |
| `resources` | 否 | 超时、并发、任务数和熔断限制 |

作用域：`standalone` 不监听 Telegram；`bot` 监听 Bot；`user` 监听用户账号；`both` 同时挂载两者。

### Vue / 模块联邦配置界面（可选）

V2 默认使用 `config_schema` 自动生成配置界面。需要自定义 Vue 配置界面的目录插件可将 `render_mode` 设为 `vue`，并在 `frontend/dist` 发布模块联邦产物：

- 入口文件：`remoteEntry.js`（可位于 `dist/` 或 `dist/assets/`）
- 必须暴露：`./Config`
- `Config` 接收 `pluginId` 与 `host` props；通过 `host.getConfig`、`host.saveConfig` 读写配置，通过 `host.callApi` 调用插件 API
- 宿主会按插件 ID 动态注册远程模块，并在每次打开配置时刷新入口缓存

该接口是 V2 的正式插件扩展点，不依赖或修改 V1 前端；插件仍可选择使用 `config_schema`。

## Python 依赖声明与安装

第三方 Python 包统一写在入口文件的 `__plugin__["requirements"]` 字符串列表中；无依赖时省略或填写 `[]`。仅放置 `requirements.txt`、`pyproject.toml` 或在市场清单中填写依赖，不会让平台自动安装。

```python
__plugin__ = {
    "id": "dependency_demo",
    "name": "依赖示例",
    "version": "1.0.0",
    "scope": "standalone",
    "requirements": ["httpx>=0.28,<1"],
}

import httpx  # 启用流程先处理依赖，再导入入口模块

async def setup(ctx):
    ctx.log.info("依赖示例已就绪")
```

### 声明规则

- 最多声明 50 项，使用发行包名称及版本约束，例如 `httpx>=0.28,<1`；不是任意 shell 命令。
- 不接受 URL/Git/本地路径依赖、pip 参数或带环境条件的声明，例如 `some-package; python_version < '3.13'`。
- 标准库（如 `json`、`asyncio`、`pathlib`）不用声明；发行包名和导入名不一定相同，例如 `beautifulsoup4` 对应 `bs4`。
- 当前检查器只检查发行包版本，不完整检查 extras 的附加依赖；不要仅靠 `package[extra]` 保证附加能力可用，应显式声明所需发行包并验证导入。
- 声明插件实际直接使用的第三方包，不要依赖开发机偶然已安装的包。不要把 V1 的 `schedulers` 等内部模块当作可安装依赖；调度使用 `ctx.schedule_interval` / `ctx.schedule_cron`。

### 安装时机与日志

1. 上传或市场安装先保存并校验插件；安装成功不代表已经启用或依赖已经安装。
2. 启用时检查当前 Python 环境能找到的发行包版本；缺包或不满足约束时，通过当前解释器的 `python -m pip install --target` 安装到 `data/plugin_deps`。
3. 已满足约束时跳过安装；多个插件需要安装时串行处理，并在获得安装锁后重新检查。因此首次启用可能等待其他插件安装。
4. 安装使用平台的代理和系统设置中的 pip 镜像源。单次 pip 执行最长 300 秒（不含等待安装锁的时间）。
5. 运行日志记录插件标题、所需依赖、开始安装、安装成功或失败原因。pip 失败显示末尾最多 2000 字符的输出；不是逐行实时输出。依赖失败时不会继续导入插件或执行 `setup`。

开关即时响应不代表后台初始化已完成；应以启用结果为准。插件不要在 `setup` 中自行运行 pip、修改平台环境或反复安装依赖。

### 共享环境与兼容性限制

`data/plugin_deps` 是持久化的共享目录，并加入平台进程的 Python 搜索路径；不是每个插件独立的虚拟环境，也不是安全沙箱。平台目前没有跨插件版本冲突求解、依赖升级事务或自动回滚保障。

开发者应选择与平台及其他插件兼容的版本范围。不要通过强制降级平台核心库解决单个插件的问题。当前安装命令不带 `--upgrade`，已有目标目录遇到版本变更时不能假定文件会正确替换；已导入模块也不会因为磁盘上的包变化而自动重新加载。遇到冲突应由管理员在备份和维护窗口内处理共享环境，必要时重启平台，不要让插件删除共享依赖目录。

`requirements` 只处理 Python 包，不安装操作系统库、编译器、浏览器可执行文件或 Node/npm 依赖。需要这些资源时，应在插件说明中列明 Windows/Linux、Python 版本及部署要求，并在缺失时给出清晰错误；优先使用平台提供的浏览器、HTTP、AI 等服务。自定义 Vue 界面由开发者构建后发布 `frontend/dist`，平台启用时不运行 npm 构建。

### 失败排查与发布检查

- “依赖声明不合法”：检查是否混入 URL、环境条件、文件路径或 pip 参数。
- “No matching distribution”或构建失败：检查包版本、当前 Python 版本、系统架构以及是否需要系统库/编译工具。
- 下载失败或超时：检查系统设置的 pip 镜像源、代理及对应服务可达性；GitHub Token 不用于 pip 包下载。
- 安装后仍提示缺模块：核对发行包名与导入名、extras、共享目录冲突，以及是否必须由管理员重启以加载变更。
- 发布前在干净环境验证首次启用、再次启用跳过安装、安装失败提示，以及多个插件共享依赖时的兼容性；日志和错误文本不得包含账号密码、Token 或私有源凭据。

`requirements` 不是前置插件 ID 列表。目前平台不实现元数据 `dependencies` / `provides_capabilities` 的前置插件解析、自动启用、能力选择或回退；不要将这些字段当作已支持的扩展接口。

## Telethon 事件

回调只接收一个 Telethon Event：

```python
async def setup(ctx):
    @ctx.on_message(pattern=r"^/echo(?:\s+(.+))?$")
    async def echo(event):
        text = event.pattern_match.group(1) or "echo"
        await event.reply(text)

    @ctx.on_edited_message(pattern=r"^/status$")
    async def edited(event):
        await event.reply("消息已编辑")

    @ctx.on_callback(pattern=b"confirm")
    async def callback(event):
        await event.answer("已确认")
```

注册器：

- `ctx.on_message(pattern=None, chats=None, incoming=True, outgoing=False)`
- `ctx.on_edited_message(pattern=None, chats=None)`
- `ctx.on_callback(pattern=None)`

`ctx.bot` 是可用 Bot 或 `None`，`ctx.users` 是在线用户客户端列表。不要跨越停用、重载或重连缓存客户端。

## 配置表单

```python
"config_schema": {
    "enabled": {
        "type": "boolean", "default": True, "label": "启用功能",
        "section": "常规", "cols": 6, "order": 1,
    },
    "keyword": {
        "type": "string", "default": "hello", "label": "关键词",
        "help": "收到包含此词的消息时触发", "required": True,
        "section": "常规", "cols": 6, "order": 2,
    },
    "token": {"type": "password", "default": "", "label": "访问令牌"},
    "mode": {
        "type": "select", "default": "reply", "label": "处理方式",
        "options": [{"value": "reply", "label": "回复"}, {"value": "forward", "label": "转发"}],
    },
    "target": {
        "type": "chat", "default": 0, "label": "目标会话",
        "chat_types": ["group", "channel"], "multi": False,
    },
    "test": {"type": "action", "label": "测试连接", "action": "test"},
}
```

界面类型：`string`、`password`、`number`、`boolean`、`select`、`multiselect`、`slider`、`text`、`list`、`chat`、`info`、`action`。

常用属性包括 `default`、`label/title`、`help`、`required`、`options`、`min/max/step`、`section`、`order`、`cols`、`show_if`。`list` 使用 `fields`；`chat` 使用 `multi/chat_types/session`；`action` 使用 `action/danger`。

```python
keyword = ctx.config.get("keyword", "hello")
ctx.update_config({"last_run": "2026-08-31 12:00:00"})
```

动作按钮：

```python
async def setup(ctx):
    async def test(payload):
        return {"ok": True, "message": "连接正常"}
    ctx.action("test", test)
```

## 调度与后台任务

```python
async def setup(ctx):
    async def refresh():
        ctx.log.info("刷新完成")

    ctx.schedule_interval("refresh", refresh, seconds=300)
    ctx.schedule_cron("daily", refresh, hour=8, minute=0)
    ctx.create_task(worker(), name="worker")
```

注意：`schedule_cron` 的时间字段使用 APScheduler 的字段名（如
`hour=8, minute=0`），不要传入 V1 的 `schedulers` 模块或自行导入调度器。
回调必须是 `async def`；没有有效时间字段的平台会拒绝注册并写入错误日志。

停用时平台会移除事件、调度、Webhook 和动作，并取消通过 `ctx.create_task()` 创建的后台任务。不要直接创建平台无法追踪的永久任务。

## 平台服务

### HTTP

```python
response = await ctx.http.get("https://example.com/api", timeout=15)
response.raise_for_status()
data = response.json()
```

`ctx.http` 继承平台代理。下载使用 `ctx.http.download(url, destination)`。

### KV 与文件

```python
count = ctx.kv.get("count", 0)
ctx.kv.set("count", count + 1)
ctx.kv.delete("old_key")
all_values = ctx.kv.items()
cache_file = ctx.data_dir / "cache.json"
```

单个 KV 值最大 10 MB，数据库最大 256 MB。

### Cookie、浏览器与 AI

```python
cookies = await ctx.cookies.get("example.com")
await ctx.cookies.set("example.com", {"sid": "..."})
html = await ctx.browser.page_source("https://example.com")
reply = await ctx.ai.chat("你好", system="回答要简洁")
description = await ctx.ai.vision("screenshot.png", "识别图片中的文字")
generated = await ctx.ai.generate_image("一张蓝绿色的极简海报")
```

`ctx.ai.is_available("text" | "vision" | "image")` 可判断能力是否可用；
`ctx.ai.available_models(...)` 只返回管理员授权给当前插件的模型别名和能力。插件不得自行保存服务地址或密钥。

### 通知

```python
await ctx.notify("任务执行完成", category="定时任务", level="info")
```

不要在日志或通知中包含密码、Token、Cookie 或 Session。

## 配置页内部 API

配置页使用 `host.callApi('/status')` 或 `host.callApi('/run', {method: 'POST', body: {}})`，
访问管理员鉴权的 `/api/plugins/<id>/api/<path>`。插件在 `setup` 中注册：

```python
async def setup(ctx):
    @ctx.on_api("status")
    async def status(request):
        return {"ok": True}
```

也支持 `ctx.on_api("status", status)`。回调接收与下文 Webhook 相同的请求对象，
通过 `request.method`、`request.query`、`request.json` 读取请求；一个路径的不同 HTTP 方法由回调处理。
插件必须启用并完成初始化后才能调用，停用时接口自动移除。
内部 API 无需注册公开 Webhook，也不使用 Webhook 密钥；仅注册 `on_api` 的接口不会通过公开 Webhook 路径暴露。
管理员 API 兼容调用已有 `on_webhook` 同名接口，但新插件应使用 `on_api`；两者同名时内部 API 优先。

## Webhook

```python
async def setup(ctx):
    async def receive(request):
        signature = request.headers.get("x-signature", "")
        if not verify(signature, request.body):
            return {"ok": False, "error": "invalid signature"}
        return {"ok": True, "payload": request.json}

    ctx.on_webhook("receive", receive)
```

公开地址为 `/api/plugin/<插件ID>/receive`。请求对象提供 `method`、`path`、`query`、`headers`、`body`、`text` 和 `json`。插件必须自行验证调用方。

Webhook 路径必须与注册值完全一致（这里的 `receive` 不能改成 `/receive`，也不能省略）：

```text
注册：ctx.on_webhook("receive", receive)
调用：POST /api/plugin/hello_world/receive
```

调用未注册的路径会返回“Webhook 不存在”。插件停用或重载后旧路由会被清理，
调用方应在插件重新启用后再请求。签名校验失败时应返回错误结果，不要抛出包含密钥的异常。

## 资源保护

平台按 `resources` 限制回调时间、并发和后台任务数，连续失败达到阈值后熔断。插件仍应为外部请求设置超时并捕获可预期异常。

## 发布到插件市场

兼容仓库根目录必须有 `manifest_v2.json`：

```json
{
  "plugins": {
    "hello_world": {
      "name": "Hello World", "version": "1.0.0", "author": "作者",
      "description": "示例插件", "scope": "standalone", "path": "hello_world.py"
    }
  }
}
```

条目也可直接放在顶层。清单中的 ID、版本和作用域必须与 `__plugin__` 一致。安装后默认不启用。

## 发布前检查

发布前验证 Windows/Linux、停用与重载清理、`standalone` 无账号运行，以及依赖失败不影响其他插件。

1. ID 与文件/目录名一致，版本与清单同步。
2. 使用 Telethon Event 单参数回调，不导入 Pyrogram/Kurigram。
3. 启用、停用、重载后没有遗留处理器或任务。
4. 外部请求有超时，网络失败不会拖垮平台。
5. 密钥不进入源码、日志、通知或公开响应。
6. Windows、Linux 路径使用 `pathlib`，不要写死盘符或 `/home`。
7. 只写 `ctx.data_dir` 和 `ctx.kv`。

硬性规则见 [SPEC.md](SPEC.md)。
