# AWBotNest 2 平台开发规范

本文定义 AWBotNest 2 平台的运行、扩展、安全与发布边界。插件开发教程见 `PLUGIN_GUIDE.md`；
本文中的插件相关条目仅用于说明平台对插件的兼容约束，不是插件开发教程。

## 入口与元数据

1. 单文件入口为 `plugins/<id>.py`，目录包入口为 `plugins/<id>/__init__.py`。
2. `<id>` 不得以下划线开头，必须与 `__plugin__["id"]` 完全一致。
3. `__plugin__` 必须是可由 `ast.literal_eval` 读取的顶层字面量字典。
4. 插件必须提供可调用的 `setup(ctx)`；可选提供 `teardown(ctx)`。
5. 单个入口文件不得超过 2 MB。
6. 必需元数据为 `id`、`name`、`version`、`scope`；作用域仅允许 `standalone`、`bot`、`user`、`both`。
7. 可选 `author` 用于显示作者名；可选 `tags` 为功能标签字符串列表，平台最多展示前 4 个。

## 运行模型

1. Telegram 回调使用 Telethon Event 单参数形式。
2. 事件通过 `ctx.on_message`、`ctx.on_edited_message` 或 `ctx.on_callback` 注册。
3. 调度通过 `ctx.schedule_interval` 或 `ctx.schedule_cron` 注册。
4. 长期协程通过 `ctx.create_task` 创建。
5. 日志使用 `ctx.log`，不得用 `print` 输出密钥或业务数据。
6. 插件不得导入和修改 `awbotnest` 内部模块，不得直接修改平台 Settings。

## 数据与安全

1. 持久数据只能写入 `ctx.kv` 或 `ctx.data_dir`。
2. 不得读取其他插件数据、平台配置、Telegram Session 或管理员凭据。
3. 密钥、Cookie、密码、验证码和 Session 不得进入日志、通知、异常文本或公开响应。
4. 公开 Webhook 必须验证调用方，并限制请求体和处理时间。
5. 网络请求必须设置合理超时，禁止无限重试。
6. 插件不得绕过平台代理、安全校验、资源限制或停用清理。

## 平台扩展兼容性边界

1. 不得导入 V1 的 `schedulers`、Pyrogram 或 Kurigram 模块；统一使用 `ctx.schedule_interval`、`ctx.schedule_cron` 和 Telethon 事件接口。
2. `schedule_cron` 至少要提供一个有效的 APScheduler 时间字段；无效表达式不得静默忽略。
3. Webhook 调用路径必须与 `ctx.on_webhook(path, callback)` 注册的路径逐字一致，且不得依赖停用插件留下的旧路由。
4. 插件安装后必须能被扫描器识别；语法错误、元数据错误或依赖缺失应通过 `ctx.log`/异常日志说明具体原因。

## Windows 与 Linux

1. 两个平台均为一等运行环境。
2. 路径使用 `pathlib.Path`，不得硬编码盘符、反斜杠或 Unix 用户目录。
3. 不得依赖固定 shell、systemd、注册表或仅单一系统存在的命令；确需使用时必须检测平台并安全降级。
4. 文本文件使用 UTF-8。

## 配置与生命周期

1. 用户配置必须在 `config_schema` 声明，未声明字段不得保存。
2. 敏感字段使用 `password` 类型，不得回显到公开接口。
3. 配置变化后插件必须能够安全重载。
4. `setup` 失败不得终止平台或影响其他插件。
5. `teardown` 应可重复调用，并释放插件自行申请的资源。
6. 停用或重载后不得继续处理事件、执行调度或保留后台任务。
7. 插件不得依赖加载顺序。

## 市场发布

1. 仓库根目录必须提供 `manifest_v2.json`。
2. 清单 ID、路径、版本和作用域必须与插件元数据一致。
3. 发布包不得包含 Token、Session、Cookie、`.env`、真实配置或用户数据。
4. 更新必须提升版本号，并说明不兼容变化。

## V2 扩展能力

### Python 依赖管理边界

平台读取入口元数据 `requirements`，在导入插件前检查发行包版本并安装缺失或版本不符的 Python 依赖。声明最多 50 项，不接受 URL 和环境条件；安装统一使用平台配置的代理、pip 镜像源，并串行执行，单次 pip 超时为 300 秒。安装结果及错误原因必须写入运行日志。

依赖持久化在共享的 `data/plugin_deps`，并非每插件独立环境。当前不提供跨插件版本冲突求解、升级事务、依赖回滚或系统软件安装；不能保证依赖变更不影响其他插件。具体开发约束与排查步骤见 [插件开发指南](PLUGIN_GUIDE.md#python-依赖声明与安装)。

当前加载器未实现 `instance_mode`、`dependencies` 或 `provides_capabilities` 元数据协议，不承诺按这些字段创建隔离账号实例、解析前置插件、自动启用依赖或选择/回退能力提供者。不得将尚未实现的协议描述为当前平台能力。

### 统一治理

`setup`、事件、Webhook、插件 API、动作、定时任务、自检和 `teardown` 均经过平台统一的超时、并发、异常记录、熔断和清理管道。插件自行创建的连接、文件句柄和后台任务必须通过 `ctx.add_cleanup` 或 `teardown` 释放。

### 平台能力

业务配置只能放在插件自己的 `config_schema`、`ctx.kv` 和 `ctx.data_dir`。Cookie 必须声明 `cookie_domains`；浏览器、AI、通知和 HTTP 使用平台托管的 `ctx.browser`、`ctx.ai`、`ctx.notify`、`ctx.http`，不得自行保存平台密钥或 Telegram Session。

### Vue 模块联邦

V2 同时支持 `config_schema` 原生表单和 `render_mode: "vue"`。Vue 插件必须暴露 `./Config`，并随插件发布 `frontend/dist/remoteEntry.js`；组件通过宿主注入的 `host.getConfig`、`host.saveConfig`、`host.callApi` 访问平台能力。详见 `PLUGIN_GUIDE.md` 的 Vue 章节。

### 发布清单

`manifest_v2.json` 中的 ID、版本、作用域、作者、图标、标签必须与 `__plugin__` 一致；升级必须递增版本并附变更说明。发布包不得包含 Token、Session、Cookie、`.env`、真实配置或用户数据。

违反安全边界、无法停用、污染其他插件数据或依赖 V1 Pyrogram/Kurigram API 的插件，不属于 AWBotNest 2 兼容插件。
