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
    "icon": "",                   # 可选，图标 URL；留空回退平台 logo
    "scope": "user",              # user | bot | both
    "default_enabled": False,
    "config_schema": {            # 可选，前端据此生成配置表单
        "keyword": {"type": "string", "default": "hello", "label": "触发词"},
    },
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

`on_message` / `on_callback` 还接受 `target` 参数，决定处理器挂载到哪类账号：`"auto"`（默认，按插件 `scope` 选择）、`"user"`、`"bot"`、`"both"`。`scope` 为 `both` 时可借此将不同处理器分别挂到用户账号或机器人账号。

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
- `ctx.user`：本插件应用范围内首个已连接用户账号的发送代理。
- `ctx.user_apps`：本插件应用范围内、所有已连接用户账号的列表，多账号插件需逐个操作时使用。
- 目标账号未连接时，对应代理的发送方法抛 `RuntimeError`；可先判 `ctx.bot.connected` / `ctx.user.connected`。

`ctx.user` 与 `ctx.user_apps` 均遵循「应用账号范围」：在插件卡片「账号」中只勾选了部分账号时，二者只返回所勾选的账号；处理器也只挂到这些账号。因此无论插件是被动响应消息、还是主动遍历 `ctx.user_apps` 发消息，都只作用于所选账号，不会用到范围外的账号。

### 通知平台所有者

监控、定时、告警类插件需向平台所有者推送时，调用 `ctx.notify` 提交给平台。平台负责分类、附加插件名与级别标签、统一格式与投递，插件无需关心收件人与格式。

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
- 平台优先经 Bot 私聊所有者（需所有者已 `/start` 过 Bot），不可用时回退至主账号收藏夹；每条通知同时写入运行日志。
- 推送通知一律走 `ctx.notify`，不要自行调用 `ctx.bot.send` 实现。

若需所有者的 Telegram 数字 ID（如直接发送至特定会话），用 `ctx.owner_id`（无主账号时为 `0`）。

### 读取配置

`config_schema` 中定义的字段，读取方式如下，每次读取均为用户保存的最新值：

```python
kw = ctx.config["keyword"]
on = ctx.config.get("enabled", True)
```

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

### 资源清理

通过 `ctx.on_message`、`ctx.on_callback`、`ctx.schedule` 注册的处理器与任务由平台在停用时自动清理，无需手动处理。若插件自行申请了其它资源（连接、文件句柄、外部客户端等），用 `ctx.add_cleanup(fn)` 注册清理回调（停用时调用），或在 `teardown(ctx)` 中释放。

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
}
```

字段属性：

| 属性 | 说明 |
|------|------|
| `type` | `string` / `password` / `number` / `boolean` / `select` / `multiselect` / `slider` / `text` |
| `default` | 默认值（必填。`multiselect` 为列表，`slider`/`number` 为数字） |
| `label` | 显示名 |
| `help` | 字段下方说明文字（可选） |
| `options` | `select`/`multiselect` 候选项，`["a","b"]` 或 `[{"value":"a","label":"甲"}]` |
| `min`/`max`/`step` | `number`/`slider` 取值约束（可选） |
| `section` | 分区标题（可选）。同一 `section` 的字段在表单中归为一组 |
| `show_if` | 条件显示，如 `{"enable_x": True}`：仅当该字段当前值匹配时显示 |

插件的全部配置均通过 `config_schema` 声明，不得修改平台配置。

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
