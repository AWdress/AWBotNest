# AWBotNest 2 插件规范

本文定义插件与平台之间的硬性兼容边界。教程见 `PLUGIN_GUIDE.md`。

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

违反安全边界、无法停用、污染其他插件数据或依赖 V1 Pyrogram/Kurigram API 的插件，不属于 AWBotNest 2 兼容插件。
