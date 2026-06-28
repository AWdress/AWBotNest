# AWBotNest 插件开发指南

> 给想自己写功能的用户看。读完你就能写一个插件，丢进平台，勾选启用，立刻用。

---

## 一分钟上手

1. 复制 `plugins/_TEMPLATE.py`，改名成你的功能名，比如 `my_feature.py`。
2. 改文件顶部的 `__plugin__` 字典（`id` 要等于文件名 `my_feature`）。
3. 在 `setup(ctx)` 里写你的逻辑。
4. 前端「上传插件」选这个文件（或直接放进 `plugins/` 目录）。
5. 在插件列表里打开开关 → 立即生效。不想要就关掉开关，立即卸载。

就这么简单。**不用重启，不用改其它任何文件。**

## 两种形态：单文件 / 文件夹

- **单文件**：`plugins/my_feature.py` —— 简单插件，一个文件搞定。
- **文件夹**：`plugins/my_feature/__init__.py` —— 复杂插件（要拆多个模块、带资源文件）。
  `__plugin__` 和 `setup` 写在 `__init__.py`，目录内可以正常 `from .helper import xxx`（按包导入）。
  目录名就是插件 ID。

两种平台都自动识别。同名时单文件优先。

## 发布到 GitHub 仓库（可选）

想让你的插件出现在别人平台的「插件市场」里，把它放进一个 GitHub 仓库即可。**推荐在仓库根放 `manifest.json`**（带版本号，平台才能识别"有更新"）：

```json
{
  "my_feature": {"name":"我的功能","version":"1.0.0","author":"我","description":"...","icon":"https://.../i.png","path":"my_feature.py"},
  "big_plugin": {"name":"大插件","version":"2.0.0","path":"big_plugin/"}
}
```

- key = 插件 id；`path` 单文件以 `.py` 结尾，文件夹以 `/` 结尾。
- 没有 `manifest.json` 也行：把插件 `.py` 或 `<id>/__init__.py` 放仓库根或 `plugins/` 目录，平台会自动扫描。

---

## 插件长什么样

一个插件就是一个 `.py` 文件，三部分：

```python
# ① 元数据：平台靠它在前端显示你的插件
__plugin__ = {
    "name": "我的功能",        # 显示名
    "id": "my_feature",        # 必须 = 文件名（去掉 .py）
    "version": "1.0.0",
    "author": "我",
    "description": "这个功能干啥的",
    "scope": "user",           # user=用户账号 / bot=机器人 / both=都挂
    "default_enabled": False,
    "config_schema": {         # 可选：前端会自动生成配置表单
        "keyword": {"type": "string", "default": "hello", "label": "触发词"},
    },
}

# ② 启用时跑：在这里注册你的处理器
async def setup(ctx):
    @ctx.on_message(ctx.filters.text)
    async def handler(client, message):
        if ctx.config["keyword"] in (message.text or ""):
            await message.reply("被我抓到了！")

# ③ 停用时跑（可选）：清理你自己开的资源
async def teardown(ctx):
    pass
```

---

## ctx 能干什么

`ctx` 是平台递给你的「工具箱」。你要的一切都从它拿，**不要自己 import pyrogram 或 config**。

### 注册消息处理

```python
@ctx.on_message(ctx.filters.text)               # 收到文字消息
async def h(client, message):
    await message.reply("收到")

@ctx.on_message(ctx.filters.outgoing & ctx.filters.text, group=-10)  # 自己发出的消息，优先级高
async def h2(client, message):
    ...
```

常用过滤器：`ctx.filters.text`、`ctx.filters.photo`、`ctx.filters.command("xxx")`、`ctx.filters.outgoing`、`ctx.filters.incoming`，可以用 `&`（与）`|`（或）`~`（非）组合。

### 注册按钮回调

```python
@ctx.on_callback(ctx.filters.regex("^my_btn$"))
async def on_click(client, callback_query):
    await callback_query.answer("点到了")
```

### 发消息

```python
await ctx.bot.send(chat_id, "机器人发的话")
await ctx.user.send(chat_id, "用户账号发的话")
await ctx.bot.send_photo(chat_id, "图片URL或路径")
```

### 给平台主人发通知

监控、定时任务、报警类插件常要「给我（平台所有者）推一条」。用 `ctx.notify` 提交给平台，**平台会自动分类、加上你的插件名和级别标签、统一发给主人**，你不用管发给谁、什么格式：

```python
await ctx.notify("有新订单啦")                          # 普通通知
await ctx.notify("磁盘快满了", level="warning")          # 警告
await ctx.notify("任务失败", level="error", category="备份")  # 带级别 + 分类

# 多账号场景：在 handler 里把 client 传进来，平台会标明是哪个账号的消息
@ctx.on_message(ctx.filters.text)
async def h(client, message):
    await ctx.notify("抢到红包", account=client)         # 通知里会带「👤 账号：xxx」
```

- `level`：`info` / `success` / `warning` / `error`，平台按级别打图标（ℹ️✅⚠️❌）。
- `category`：可选业务分类（如「订单」「签到」），显示在标签里。
- `account`：多账号时传 handler 收到的 `client`，平台自动显示该账号名——你不用自己查账号是谁。
- 平台优先用 Bot 私聊主人（需主人事先 /start 过 Bot），Bot 不可用就发到主账号收藏夹；每条通知也会进运行日志。
- **不要为了发通知自己去 `ctx.bot.send`**，统一走 `ctx.notify`，格式和投递由平台负责。

### 读配置

`config_schema` 里定义的字段，用户在前端改完，你这样读：

```python
kw = ctx.config["keyword"]          # 拿到当前值（用户改过就是新值）
on = ctx.config.get("enabled", True)
```

### 存数据（每个插件独立，互不干扰）

```python
ctx.kv.set("count", 10)
n = ctx.kv.get("count", 0)
ctx.kv.delete("count")
ctx.kv.keys()
```

### 存文件（图片、素材等实际文件）

`ctx.kv` 只存键值。要存实际文件（如自动换头像的图片池），用 `ctx.data_dir`——一个**你独享**的可写目录：

```python
p = ctx.data_dir / "avatars" / "a.jpg"   # data/plugin_data/<你的id>/avatars/a.jpg
p.parent.mkdir(parents=True, exist_ok=True)
p.write_bytes(img_bytes)
```

`ctx.data_dir` 是 `Path`，首次访问自动建好目录，每个插件一份，互不干扰。

### 写日志（别用 print）

```python
ctx.log.info("处理了一条消息")
ctx.log.warning("有点不对劲: %s", err)
ctx.log.error("出错了: %s", err)
```

> 你的日志会自动带上 `[插件id]` 前缀，在「运行日志」页能按插件名搜索、过滤，和平台日志区分开。

### 定时任务（停用插件时自动取消）

```python
async def tick():
    ctx.log.info("每分钟跑一次")

ctx.schedule(tick, "interval", seconds=60)
ctx.schedule(tick, "cron", hour=3, minute=0)   # 每天 3:00

# 可选 id=... 给任务起个可读名字，会显示在「系统状态」页
ctx.schedule(daily_report, "cron", hour=9, id="每日早报")
```

> 注册的任务会出现在「系统状态」页的定时任务卡片里，显示**任务名 · 所属插件 · 触发规则 · 下次运行时间**。
> 不传 `id` 时默认用函数名；id 自动加 `<插件id>::` 前缀以归属到本插件，停用插件时全部自动移除。

---

## config_schema 字段类型

前端会根据类型自动生成不同的输入控件，**打开插件的「配置」按钮就是你定义的设置界面**。

```python
"config_schema": {
    # 功能开关分到一组
    "enable_x":   {"type": "boolean", "default": True, "label": "启用X功能", "section": "功能开关"},
    "enable_y":   {"type": "boolean", "default": False, "label": "启用Y功能", "section": "功能开关"},
    # 参数分到另一组（仅在 enable_x 打开时显示 —— show_if 联动）
    "text_field": {"type": "string",  "default": "",  "label": "文本", "section": "参数", "help": "字段下方的说明", "show_if": {"enable_x": True}},
    "secret":     {"type": "password","default": "",  "label": "密钥", "section": "参数"},
    "number_field":{"type": "number", "default": 0,   "label": "数字", "section": "参数", "min": 0, "max": 100},
    "volume":     {"type": "slider",  "default": 5,   "label": "滑块", "section": "参数", "min": 0, "max": 10, "step": 1},
    "choice":     {"type": "select",  "default": "a", "label": "单选", "options": ["a","b","c"], "section": "参数"},
    "tags":       {"type": "multiselect", "default": [], "label": "多选", "options": ["x","y","z"], "section": "参数"},
    "long_text":  {"type": "text",    "default": "",  "label": "多行文本", "section": "参数"},
}
```

字段属性：
- `type`：`string` / `password`(密码) / `number` / `boolean`(开关) / `select`(下拉) / `multiselect`(多选标签) / `slider`(滑块) / `text`(多行)
- `default`：默认值（必填；multiselect 用列表，slider/number 用数字）
- `label`：显示名
- `help`：字段下方的灰色说明文字（可选）
- `options`：`select`/`multiselect` 的可选值，可写 `["a","b"]` 或 `[{"value":"a","label":"甲"}]`
- `min`/`max`/`step`：`number`/`slider` 用（可选）
- `section`：分区标题（可选）。同一 `section` 的字段在界面里归为一组。
- `show_if`：条件联动显示，如 `{"enable_x": True}` —— 仅当 `enable_x` 当前值是 `True` 才显示本字段。用它做「开关打开才出现相关参数」。

> 重要：**插件的所有配置都写在这里**，不要去改平台配置。用户在前端改的值，你用 `ctx.config["字段名"]` 读取，每次读取都是最新值。

---

## scope 怎么选

| scope | 处理器挂到哪 | 适合 |
|-------|------------|------|
| `user` | 你的用户账号 | 监听群里消息、自动抢红包、自动抽奖等 |
| `bot` | 机器人账号 | 菜单、命令、给用户回话 |
| `both` | 两者都挂 | 两边都要响应的功能 |

---

## 必须遵守的规矩

1. **一个文件一个插件**，文件名 = `id`，全局唯一。
2. **不要 import pyrogram / config / 内核模块**，一切走 `ctx`。
3. **不要用 `@Client.on_message`**，用 `@ctx.on_message`（否则关不掉）。
4. **不要 `print`**，用 `ctx.log`。
5. **插件之间不要互相 import**。要共用逻辑，写成工具函数放 `_` 开头的文件，或下沉到平台。
6. 以 `_` 开头的文件不会被当成插件（用作模板/辅助）。

---

## 常见问题

**Q：上传后插件标红了？**
看红字提示。多半是：缺 `__plugin__`、`id` 和文件名不一致、`scope` 写错、或者代码有语法错误。

**Q：改了插件代码怎么生效？**
前端点「重载」，或者关一次开关再打开。

**Q：插件报错会不会拖垮整个平台？**
不会。单个插件加载失败只标红它自己，其它插件和平台照常运行。

**Q：我的数据存哪了？**
`ctx.kv` 的数据在 `data/kv/<你的插件id>.sqlite`，文件在 `data/plugin_data/<你的插件id>/`，每个插件一份，互不干扰。

---

完整的硬性规范见 `SPEC.md`。照着 `plugins/_TEMPLATE.py` 改最快。
