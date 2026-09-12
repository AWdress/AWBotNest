# AWBotNest 2 平台开发规范

> **强制性：本规范是 AWBotNest V2 后续所有平台改动的依据。任何修改必须遵守本规范；如需变更规范本身，必须遵循本文末尾的「变更协议」。**

本文定义 AWBotNest 2 平台的运行、扩展、安全与发布边界。插件开发教程见 `PLUGIN_GUIDE.md`；
本文中的插件相关条目仅用于说明平台对插件的兼容约束，不是插件开发教程。

## 稳定架构与非目标

AWBotNest V2 的稳定调用方向为：

```text
Telegram / Telethon
        │
        ▼
TelegramAccounts
        │
        ▼
PluginRuntime ────── API / FastAPI
        │
        ▼
PluginContext
  ┌─────┼──────────────┐
  ▼     ▼              ▼
Storage Sessions  TelegramDelivery
        │
        ▼
Platform Services / Scheduler / Governance
```

强制原则是：**插件共享平台能力，但默认不共享状态。** 每个插件及账号实例拥有自己的 handler、task、scheduler job、storage namespace、session namespace、delivery lifecycle、数据目录和错误状态。

以下方向已经确定，普通功能修改不得擅自替换：Telethon、FastAPI、SQLite、asyncio、`PluginContext` Facade、`setup(ctx)` / `teardown(ctx)` 生命周期以及插件 enable/disable/reload 模型。不得借普通需求重写前端、插件元数据格式、插件市场或整个 PluginContext，也不得引入 Redis、消息队列、微服务、分布式锁、跨插件 GameBus、共享经济系统或通用插件 RPC。

## 平台模块边界

1. `awbotnest/api/` 按领域维护 FastAPI routers；app composition 只负责创建应用、注册 middleware/router/lifecycle/OpenAPI 及挂载静态资源。
2. `awbotnest/app.py` 是 `create_app` 的 compatibility shim，不再承载 endpoint 实现。测试 patch 必须指向 symbol 的实际查找位置，不得继续 patch 已移出的 `awbotnest.app.*` 实现。
3. `awbotnest/plugin_runtime/` 负责扫描、元数据解析、依赖解析、模块加载和卸载；`PluginRuntime` 负责生命周期协调，不得重新聚合所有实现细节。
4. `awbotnest/services/` 分别实现 HTTP、AI、Browser 和 Cookie；`PlatformServices` 只作为容器，不得成为新的 God Object。
5. `PluginContext` 是正式 Plugin SDK Facade。插件通过 `ctx.http`、`ctx.ai`、`ctx.browser`、`ctx.cookies`、`ctx.storage`、`ctx.sessions`、`ctx.delivery`、通知、路由和调度接口使用平台能力，不直接依赖平台内部实现。
6. 内部拆分不得无意改变现有 REST URL、HTTP method、参数、响应 JSON、状态码、鉴权、auth cookie、WebSocket 协议、前端 contract、配置格式或数据库格式。

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

1. 持久数据只能写入异步 `ctx.storage`（`ctx.kv` 是同一对象的兼容别名）或 `ctx.data_dir`。
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

1. 原生 schema 表单的用户配置必须在 `config_schema` 声明；自定义 Vue 配置页允许保存额外业务字段，已声明字段仍执行类型及必填校验。敏感字段仍必须声明以便平台脱敏。
2. 敏感字段使用 `password` 类型，不得回显到公开接口。管理端插件配置默认只返回掩码；原生表单的显示按钮和自定义 Vue 配置的 `host.revealSecret(field)` 可以按字段受控读取真实值。
3. 配置变化后插件必须能够安全重载。
4. `setup` 失败不得终止平台或影响其他插件。
5. `teardown` 应可重复调用，并释放插件自行申请的资源。
6. 停用或重载后不得继续处理事件、执行调度或保留后台任务。
7. 插件不得依赖加载顺序。

平台必须保证：

1. 同一插件的 enable、disable 和 reload 由 per-plugin lifecycle lock 串行化；不同插件不得共用一把全局生命周期锁。
2. setup 失败后仍清理已经注册的 handler、task、scheduler job、route、capability、session、delivery、storage 和模块缓存。
3. teardown 异常或超时不能中止其余清理；外部取消应在清理完成后继续传播。
4. reload 必须先清理旧实例，不能同时保留旧 handler 与新 handler。
5. 应用关闭时单个插件失败不应阻止其他插件释放资源。
6. 每个插件的并发信号量、熔断和后台任务限额由唯一的 Governor 管理，不得在 PluginContext 内建立第二套相互矛盾的治理状态。

## Async 正确性

1. Telethon handler、API endpoint 和 scheduler callback 的热路径不得直接执行同步 SQLite 或其他长时间阻塞 I/O；必要时使用真正异步实现或 `asyncio.to_thread`。
2. `PluginKV.get/set/delete/items` 是正式 async API，所有调用必须 `await`。不得为兼容旧插件重新加入同名同步 API。
3. SQLite 继续作为底层存储；应保持现有路径、schema、序列化和 WAL 数据兼容。涉及 schema 变化时必须提供明确 migration，不得静默重建数据。
4. `asyncio.CancelledError` 必须传播。取消不能计为插件故障、增加熔断计数或被普通 `except Exception` 路径转换为业务失败。
5. Telethon `StopPropagation` 是正常控制流，不计入失败或熔断。
6. operation timeout 后不得留下平台无法追踪的 orphan task；SQLite 等不可安全中断的操作必须等待事务完成或回滚后再释放锁。
7. 插件后台任务必须由 Governor 按插件统计、按实例归属和取消。取消超时的顽固任务必须继续保持追踪并记录警告。

## 市场发布

1. 仓库根目录必须提供 `manifest_v2.json`。
2. 清单 ID、路径、版本和作用域必须与插件元数据一致。
3. 发布包不得包含 Token、Session、Cookie、`.env`、真实配置或用户数据。
4. 更新必须提升版本号，并说明不兼容变化。

## V2 扩展能力

### Python 依赖管理边界

平台读取入口元数据 `requirements`，在导入插件前检查发行包版本并安装缺失或版本不符的 Python 依赖。声明最多 50 项，不接受 URL 和环境条件；安装统一使用平台配置的代理、pip 镜像源，并串行执行，单次 pip 超时为 300 秒。安装结果及错误原因必须写入运行日志。

依赖持久化在共享的 `data/plugin_deps`，并非每插件独立环境。当前不提供跨插件版本冲突求解、升级事务、依赖回滚或系统软件安装；不能保证依赖变更不影响其他插件。具体开发约束与排查步骤见 [插件开发指南](PLUGIN_GUIDE.md#python-依赖声明与安装)。

加载器支持 `instance_mode="shared"`（默认）及 `"account"`。账号实例分别隔离 storage、session、scheduler job、delivery lifecycle、后台任务和数据目录；HTTP 路由由首个实例注册。需要 shared storage 时必须设计明确的新平台能力，不得把账号实例数据库隐式合并。`requires_plugins` 声明前置插件，`requires_capabilities` 声明所需能力，`provides_capabilities` 声明提供能力。恢复启动按依赖顺序进行，单个失败不阻断其他无关插件；手动启用缺少依赖时明确报错，不擅自启用其他插件。插件默认必须彼此独立，`requires_plugins` 仅用于少数边界清晰、确有必要的扩展场景。能力通过 `ctx.provide_capability` 注册，调用失败时尝试下一提供者。Capability 仅用于少量平台扩展点，不得发展为通用 plugin-to-plugin RPC，不得用于取得其他插件实例、共享插件内部状态或建立链式依赖网络；`dependencies` 不作为这些字段的别名。

运行治理提供超时、并发限制、熔断冷却恢复、事件回放及停用时资源清理。Telegram `StopPropagation` 属于控制流程，不计为故障。Cookie 接口只读，按 `cookie_domains` 声明授权，保留路径、过期时间和子域匹配；`request_sync` 对缺少 Cookie 的提醒限频 30 分钟。

### 统一治理

`setup`、事件、Webhook、插件 API、动作和定时任务经过平台执行治理。`teardown` 有独立的 15 秒超时及失败清理，不因熔断跳过资源释放。自检接口报告配置、依赖和加载状态。插件自行创建的连接、文件句柄必须通过 `ctx.add_cleanup` 或 `teardown` 释放，后台任务通过 `ctx.create_task` 创建以纳入停用取消。

### Session Runtime

Session 是插件实例作用域内的短期内存状态，内部 namespace 至少包含 `plugin_id`、`instance_id` 和 session key。平台提供 get/open、per-session asyncio lock、TTL、touch、reset、cleanup、插件卸载及应用关闭清理。不同插件、不同账号实例和不同 chat 不得共享 Session 或全局锁。运行中的牌局和回合状态放入 Session；长期统计、用户配置和历史数据放入 Storage。第一版不引入 Redis、分布式 Session、跨插件 Session 或 GameBus。

### Telegram Delivery

Delivery 是可选治理能力，不封装或禁止 Telethon 原生 `event.reply`、`client.send_message` 和 `message.edit`。平台 Delivery 负责短 FloodWait 的有限重试、重复 edit 抑制、edit coalescing、同账号同 chat 顺序以及插件停用时取消 pending operation。

必须使用 per-chat lock；禁止使用串行化整个 Telegram account 或整个平台的 global send lock。不同 chat 必须能够并发。闲置 chat lock 应安全回收，但不得删除正在持有或仍有等待者的 lock。插件 unload 和平台 shutdown 后不得遗留 delivery worker。

### 平台能力

业务配置只能放在插件自己的 `config_schema`、异步 `ctx.storage`（或其兼容别名 `ctx.kv`）和 `ctx.data_dir`。Cookie 必须声明 `cookie_domains`；浏览器、AI、通知和 HTTP 使用平台托管的 `ctx.browser`、`ctx.ai`、`ctx.notify`、`ctx.http`，不得自行保存平台密钥或 Telegram Session。

平台浏览器默认使用内置 Chromium。管理员选择 CloakBrowser 时，平台把依赖安装到持久化的
`data/plugin_deps`，License Key 仅由系统设置保存，不通过 Compose 环境变量或插件配置传递。只有管理员开启
免费 Key 模式且 Key 非空时才可在调用时注入，并以进程级单会话队列治理 CloakBrowser `launch*` 调用，
以 Browser/Context 的正常关闭作为放行条件；异常关闭后等待服务端席位释放。关闭开关或删除 Key 时必须停止
注入并恢复旧版免费内核，不得选择遗留的最新版缓存或改变原有并发行为。组件更新必须等待队列空闲，
只更新持久化依赖目录中的兼容版本，并通过平台重启切换代码；内核在下次调用时更新到持久化缓存。
平台每天只读检查一次兼容组件版本，并可在启动后异步检查；只有 CloakBrowser 已选中、免费 Key 模式开启且
Key 非空时才允许发起检查请求，任一条件不满足都必须在本地静默跳过。定时检查不得自动安装或替换组件。

### Vue 模块联邦

V2 同时支持 `config_schema` 原生表单和 `render_mode: "vue"`。Vue 插件必须暴露 `./Config`，并随插件发布 `frontend/dist/remoteEntry.js`；组件通过宿主注入的 `host.getConfig`、`host.saveConfig`、`host.callApi` 访问平台能力。详见 `PLUGIN_GUIDE.md` 的 Vue 章节。

### 发布清单

`manifest_v2.json` 中的 ID、版本、作用域、作者、图标、标签必须与 `__plugin__` 一致；升级必须递增版本并附变更说明。发布包不得包含 Token、Session、Cookie、`.env`、真实配置或用户数据。

违反安全边界、无法停用、污染其他插件数据或依赖 V1 Pyrogram/Kurigram API 的插件，不属于 AWBotNest 2 兼容插件。

## 测试与合并门槛

1. 每次修改必须运行与改动直接相关的测试；合并和发布前必须运行 `tests/` 全部测试，不得只运行新增测试。
2. API 拆分需要验证 app 创建、router 注册、原 path、鉴权、WebSocket 和前端 contract。
3. 生命周期修改至少覆盖 enable、disable、reload、setup failure、teardown exception/timeout、handler 去重、task cancellation 和插件间隔离。
4. Storage 修改至少覆盖现有 SQLite 数据读取、并发读写、取消、关闭及账号实例隔离。
5. Session 修改至少覆盖 namespace 隔离、同 session lock、不同 session 并发、TTL、reset 和 unload cleanup。
6. Delivery 修改至少覆盖同 chat 顺序、不同 chat 并发、FloodWait 有限重试、重复抑制、edit 合并、取消、idle lock 回收及 shutdown。
7. 测试必须检查并消除 `coroutine was never awaited`、`Task was destroyed but it is pending`、未关闭 transport/client 等资源警告。
8. 不得通过删除、跳过或放宽有效回归测试使变更通过。测试 patch target 必须对应重构后的实际模块。
9. 延迟敏感的 Telegram handler 可显式启用 Interactive Fast Path。快路径必须绕过普通 Governor、plugin-wide semaphore、通用超时、执行日志和 per-operation circuit，但仍保留关闭检查、活动任务追踪、插件归属、异常隔离、`StopPropagation`、取消传播和生命周期清理。状态一致性由插件按真实业务 key 使用 `ctx.sessions` 负责，平台不得自动添加 per-chat lock。性能回归应比较普通与交互 handler，并分别测量 handler wrapper overhead、Session lock wait 和 Telegram API call start。
10. Interactive profiling 默认关闭，仅在显式诊断开关启用时使用单调纳秒时钟和有界内存记录。不得为 profiling 默认写磁盘、生成 UUID、记录消息正文或全局 monkey patch Telethon。Session lock 和 TelegramDelivery instrumentation 必须保持原有语义；直接 Telethon 调用无法安全自动覆盖时使用独立真实网络探针。

## 变更协议

本规范本身的变更必须遵循以下流程：

1. 说明变更动机、范围、替代方案以及对 REST、WebSocket、前端、Plugin API、Telegram、数据库和配置的兼容影响。
2. 如果改变公开 Plugin API，同一变更中必须更新 `PLUGIN_GUIDE.md`，给出最终接口、示例和迁移说明；必要时调整 `plugin_api_version`。
3. 如果改变平台内部边界，应保持现有 compatibility shim 或明确记录移除计划，不得让内部移动静默破坏外部 import。
4. 为新规则增加与风险相称的 regression tests，并完整运行项目测试。
5. 数据格式或 SQLite schema 变化必须附 migration 和回滚方案；不得以删除用户数据作为默认迁移方式。
6. 安全、鉴权、密钥、Cookie、Session、备份或公开网络接口变化必须单独说明风险。
7. 规范修改与实现必须在同一发布周期保持一致。实现尚未落地的设计应明确标为提案，不得写成已支持能力。
8. 未完成兼容性说明、文档同步和完整测试的规范变更不得合并或发布。
