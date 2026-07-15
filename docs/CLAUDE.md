# AWBotNest 项目说明

> 本文件是整个项目的结构与实现说明，供随时了解项目全貌。
> **每次改动代码后都会即时更新本文件**（见文末「修改记录」）。
> 更详细的强制开发规范见 [SPEC.md](SPEC.md)，插件编写教程见 [PLUGIN_GUIDE.md](PLUGIN_GUIDE.md)。

---

## 一、这是什么

AWBotNest 是一个 **Telegram 机器人平台**。核心理念：**平台内核 + 单文件插件**。

- 所有业务功能都是**插件**——一个功能 = 一个 `.py` 文件（或一个目录包），在网页控制台点几下即可上传、安装、开关，无需懂代码、无需重启进程。
- **内核**只提供通用能力（账号生命周期、插件热插拔、给插件的统一上下文 `ctx`），极少改动。
- **前端**（Vue3 深色控制台）是用户唯一操作台：插件管理、账号管理、运行日志、系统状态、系统设置。
- 内置**插件市场**：官方仓库 + 用户自定义 GitHub 仓库，浏览一键安装。
- 安全铁律：插件下载后**绝不自动运行**，必须用户手动开启才在服务器执行。

技术栈：Python 3.13 + Pyrogram(kurigram) + FastAPI + SQLAlchemy 2.0 async + APScheduler + Vue3/Vite。

---

## 二、整体架构

项目分三层，依赖方向单向：

```
┌─────────────────────────────────────────────────────┐
│  webui/  前端(Vue3) + FastAPI 后端 —— 用户操作台        │
├─────────────────────────────────────────────────────┤
│  kernel/  平台内核（稳定）：账号 / 插件热插拔 / ctx 能力面 │
│  plugins/ 单文件插件（所有业务在这，用户上传）             │
├─────────────────────────────────────────────────────┤
│  core/ infra/ adapters/ models/ libs/ schedulers/     │
│  复用自旧项目的六边形架构「底座」（内核可用，插件不可直接用）│
└─────────────────────────────────────────────────────┘
```

- **内核与插件分离铁律**：业务一律是插件，禁止往 `kernel/` 塞业务；插件只能通过 `ctx` 访问平台，禁止 `import pyrogram` / `from config` / `from core|kernel`。
- **底座是六边形架构**（端口与适配器）：`core`（领域模型+端口协议+业务服务，不依赖任何框架）← `adapters`（端口的具体技术实现）；`infra` 提供配置/DI 容器/日志/调度。DI 容器在启动时把 adapters 装配进 services。这是从旧项目渐进式重构保留的底座，新内核通过 `app.py` 兼容垫片复用它。

---

## 三、目录与文件职责

### 根目录 / 入口

| 文件 | 职责 |
|------|------|
| `main.py` | **平台入口**。启动顺序：配置文件自检(写 `data/config.json` 模板) → 挂 `data/plugin_deps` 到 sys.path → 导出代理环境变量 → 启动账号(AccountManager) → 初始化数据库 + DI 容器 → 启动调度器 → 恢复已启用插件(PluginRuntime) → 插件仓库轮询 → 启动 Web UI → idle 等待。用 `asyncio.wait(FIRST_EXCEPTION)` 让平台/WebUI 任一崩溃即退出。 |
| `app.py` | **兼容垫片**。旧代码大量 `from app import ...`，此处把 `get_bot_app`/`get_user_app`/`get_user_apps`/`scheduler`/`logger` 等旧访问点重新导出、指向新内核（`kernel.state`），使旧业务代码零改动可用。 |
| `pyproject.toml` / `requirements.txt` | 依赖声明（Python 3.13）。ruff/mypy/pytest 配置在 pyproject。 |
| `Dockerfile` | 多阶段构建：stage1 Node 构建 Vue 前端 → `webui/static`；stage2 Python 运行时，装依赖(含 wkhtmltopdf、CJK 字体、ddddocr 系统库)、`playwright install-deps`（只装系统库，**不烤浏览器二进制**，内核运行时懒下载到卷）→ `python main.py`。 |
| `docker-compose.yml` | 部署编排。默认 SQLite（无需额外容器），卷映射 `plugins/ logs/ sessions/ db_file/ data/`，暴露端口 18001。可选 MySQL 服务（注释中）。 |
| `README.md` | 面向用户的使用说明。 |
| `.github/workflows/` | CI：`docker-build.yml`（构建推送镜像）、`release.yml`（发版）。 |

### kernel/ — 平台内核（热插拔核心）

| 文件 | 职责 |
|------|------|
| `__init__.py` | 统一出口，导出 `AccountManager`/`PluginRuntime`/`PlatformContext`/`PluginRegistry`/`PluginMeta`/`registry`。 |
| `state.py` | **内核单例持有处**。解决 `python main.py`(模块名 `__main__`) 与 webui `import main`(模块名 `main`) 产生两份全局变量的问题——单例(`accounts`/`runtime`/`started_at`)放这里(只加载一次)，是主进程与 Web API 视图共享单例的桥梁。 |
| `registry.py` | **插件注册表**(线程安全)。`parse_meta` 用 AST `literal_eval` **静态读取** `__plugin__` 字面量(不执行插件代码)，校验必填字段/scope/render_mode/id=文件名。支持单文件与文件夹两种形态。持久化到 `data/plugins_state.json`(enabled/config/account_scope/bot_choice)。 |
| `plugin_runtime.py` | **插件运行时**(真热插拔)。加载：`importlib` 动态导入 → 校验 setup → `deps.ensure`(冲突拒绝) → 构建 ctx → `await setup`。卸载：注销 handler/任务 → teardown → 从 sys.modules 移除。为每插件分配**独立 group 基址**(1000 起，步长 1000)避免互相"吃消息"。单插件失败只标 `error`。`resync` 在账号上下线后重挂 handler。 |
| `context.py` | **PlatformContext**(插件唯一能接触的 API 面)。暴露：账号(`ctx.bot`/`ctx.user`/`ctx.user_apps`按范围过滤)、处理器注册(`on_message`/`on_edited_message`/`on_callback` 自动登记句柄+group 平移)、HTTP 入站(`on_webhook`/`on_api`/`action`)、存储(`kv`/`data_dir`/`config`/`update_config`)、`notify`/`browser`/`download`/`schedule`/`log`。含 `WebhookRequest`。 |
| `account_manager.py` | **账号生命周期**。管理用户账号(`user_apps`)与多 Bot(`bot_apps`，默认 id=`default`)。启动(清损坏 session→起 Bot→起用户账号)、上下线(`.paused` 标记)、删除、多步登录(发码→交码→两步密码→finalize 重绑 DI)。 |
| `deps.py` | **插件依赖管理**。单进程同一包只能一版本，以"已装环境"为准做冲突检测(满足跳过/缺失装/冲突拒绝启用)。`pip --target` 装到 `data/plugin_deps`(卷持久化)+加 sys.path 末尾。走 `PIP_INDEX_URL`(默认清华镜像)，requirement 经 packaging 解析后独立 argv 传 pip 防注入。 |
| `browser.py` | **平台级浏览器能力**(供 `ctx.browser`)。引擎优先 CloakBrowser(过 Cloudflare/指纹)→回退 Playwright Chromium。**懒加载**：首次调用才下内核到 `data/browser_cache`。暴露 async `page_source`/`run`。 |
| `notifier.py` | **通知中心**。插件不直接发通知，`ctx.notify` 提交给平台统一分类(级别标签+插件名+账号名)+格式化+投递(优先路由 Bot→管理员 `MY_TGID`→回退收藏夹)。含 200 条环形历史。 |
| `activity.py` | **插件活跃度统计**。用 contextvar 记录"当前执行 handler 的插件"，只统计插件真正发出的动作。24 个 1 小时桶环形窗口，持久化 `data/activity.json`，供状态页时间线。 |

### plugins/ — 单文件插件（业务全在这）

| 路径 | 职责 |
|------|------|
| `__init__.py` | 包标记。 |
| `_TEMPLATE.py` | **普通插件模板**。展示插件契约：纯字面量 `__plugin__`(id=文件名/scope/config_schema/requirements/webhook 等) + `setup(ctx)` + 可选 `teardown(ctx)`。一切走 `ctx`。 |
| `_TEMPLATE_VUE/` | **Vue 模式插件模板**(自带配置界面)。`__init__.py` 声明 `render_mode:"vue"` + `ctx.on_api` 注册后端接口；`frontend/` 是 Vite+模块联邦工程，`vite.config.js` 暴露 `./Config`、共享宿主 Vue，`src/Config.vue` 接收 `pluginId`+`host` prop；`dist/` 是必须随插件提交的构建产物。 |
| `<id>.py` 或 `<id>/__init__.py` | 用户上传/导入的实际插件。`_` 前缀不被识别为插件。同名单文件优先。 |

### webui/ — 前端 + 后端 API

**后端：**

| 文件 | 职责 |
|------|------|
| `api.py` | **主 FastAPI 后端**(约 1200 行，`APP_VERSION`)。路由分组：鉴权(`/api/auth/*`)、静态前端、插件管理(`/api/plugins/*` 列/传/开关/重载/删/配置/账号范围/会话/动作/webhook 信息)、插件自带前端(`/fe/`)与插件 API(`/api/`)、GitHub 导入、插件商店、多 Bot 路由、账号管理(三步登录)、平台设置(`/api/settings` 读写 config.json，敏感字段打码)、系统重启/代理测试/DB 测试、Webhook 入站(公开靠 apikey)、系统状态(`/api/status`)、运行日志(历史+WebSocket)。 |
| `auth.py` | **鉴权**。用户名+密码(PBKDF2 存 `data/auth.json`)。**无状态令牌** `HMAC(secret, "user:pwd_hash")`(改密自动失效、重启不失效)。依赖：`require_auth`(Bearer)、`require_password_changed`(默认密码返回 428)、`require_resource_access`(资源 Cookie，供 vue 插件 ESM import)。`AWBOTNEST_DEV_NO_AUTH=true` 放行(仅开发)。 |
| `github_import.py` | **从 GitHub 导入插件**。解析多种来源格式(raw/仓库 URL/blob/`owner/repo[/subdir]`，防 SSRF)。优先读 `manifest.json`(插件市场清单)，无则目录扫描。支持单文件与文件夹递归下载，走平台代理，绕 CDN 缓存。 |
| `repo_sync.py` | **插件商店/仓库轮询**。聚合官方仓库(`AWdress/AWBotNest-Plugins`)+用户 `PLUGIN_REPOS` 成商店列表(标 installed/official)。轮询(默认 20 分)只做两件事：刷新商店缓存、给已装且 manifest 版本变化的插件下载更新(在运行的热重载)。**绝不自动启用**。状态存 `data/repo_sync.json`。 |
| `log_stream.py` | **日志流**。接平台 logger 输出到环形缓冲(最近 500 条)+WebSocket 广播，跨线程 `call_soon_threadsafe` 投递。 |
| `__init__.py` | 包标记。 |

**前端（`frontend/`，Vue3 + Vite，产物输出到 `webui/static` 由 FastAPI 托管）：**

| 路径 | 职责 |
|------|------|
| `vite.config.js` | Vue + 模块联邦(平台作宿主，只共享 vue 单例)。产物 outDir=`../static`，target esnext。开发时 `/api` 代理到 18001。含哑 remote 绕联邦空 remotes bug。 |
| `src/main.js` | hash 路由(5 页 status/plugins/accounts/logs/settings) + 注册 PWA SW。 |
| `src/api/index.js` | 统一请求封装(令牌存 localStorage，带 Bearer；401 跳登录)。方法对应后端各路由。 |
| `src/App.vue` | 外壳。鉴权门(未登录→Login，默认密码→强制改密，否则主布局)。10 秒心跳拉状态，6 小时查 GitHub release 更新。重启平台/退出登录。 |
| `src/views/Login.vue` | 登录页。 |
| `src/views/Status.vue` | 系统状态。8 秒轮询，展示概览卡片、调度任务(trigger 转中文)、插件活跃时间线。 |
| `src/views/Plugins.vue` | **核心页**(约 1019 行)。「我的插件/插件市场」两标签。卡片开关/重载/删/上传(点击+拖拽)、配置弹窗(schema→ConfigForm，vue→RemotePluginConfig)、账号范围、插件专属日志、webhook 地址。市场分区(可下载/有更新/已安装)。 |
| `src/views/Accounts.vue` | 账号管理 + 三步登录向导。 |
| `src/views/Logs.vue` | 运行日志。连 WebSocket(断开重连)，级别/关键词过滤、自动滚动、暂停、清空。 |
| `src/views/Settings.vue` | 系统设置(约 593 行)。分标签(登录/Telegram 凭据/通知/Web 控制台/代理/数据库)。脏值检测、重启提示、代理/DB 测试、多 Bot 增删、Webhook 密钥生成。 |
| `src/components/ConfigForm.vue` | 按 config_schema 自动渲染表单(section 分组、show_if 条件、短字段多列/大字段整行、validate 校验)。 |
| `src/components/FieldInput.vue` | 单字段渲染器(覆盖全部字段类型，chat 拉会话、action 触发插件动作)。 |
| `src/components/RemotePluginConfig.vue` | vue 模式插件配置。模块联邦运行时动态加载插件 `./Config` 组件并挂载，传 `host` 能力对象。 |
| `src/components/ConfirmDialog.vue` / `Toast.vue` | 全局确认弹窗 / 悬浮提示，配 `composables/confirm.js`、`toast.js` 命令式调用。 |
| `src/styles/tokens.css` | 设计 token(深色控制台配色 CSS 变量)。 |

### core/ — 框架无关业务内核（六边形架构底座）

| 子目录 | 职责 |
|------|------|
| `domain/` | **领域模型**(纯 dataclass/pydantic，无外部依赖)。`lottery`(抽奖)、`red_packet`(红包+OCR)、`transfer`(转账/排行榜)、`game`(炸弹游戏)、`user`、`ydx`(骰子)、`ai`(AI 对话)。 |
| `ports/` | **端口接口**(Protocol，只依赖 domain)。`messaging`(MessageSender/NotificationPort)、`storage`(各 Repository)、`ocr`、`leaderboard`、`ai`。 |
| `services/` | **业务服务**(依赖 ports，注入端口)。`red_packet_service`(抢红包状态机)、`lottery_service`、`trap_service`(陷阱检测，被复用)、`transfer_service`、`ai_service`、`prize_service`、`ydx_service`、`redpocket_record_service`。 |
| `__init__.py` | 核心层统一出口(聚合导出 Pyrogram 类型/Client/config/domain/services)。 |
| `manager.py` | `Manager` 单例(旧)，管理多 user + bot 账号启动，重启时 `rebind_user_client` 重绑 DI。 |
| `telegram.py` | Pyrogram 轻量导出层(禁依赖 core 其他模块防循环)。 |

### infra/ — 基础设施

| 文件 | 职责 |
|------|------|
| `config.py` | pydantic-settings 统一配置(`AppSettings` 组合 Telegram/Database/Proxy/Ai)。多源优先级：环境变量 > `state.toml` > 旧 `config/config.py`。`get_settings()` lru_cache 单例。 |
| `container.py` | **DI 容器**(dependency-injector)。声明 adapter 与 service 装配关系，外部 `build_container()` 注入 client/session/state。`rebind_user_client()` 热重绑。 |
| `logging.py` | structlog 配置(JSON/彩色双模)，`logger` 向后兼容。 |
| `scheduler.py` | APScheduler 封装(AsyncIOScheduler，Asia/Shanghai)。 |

### adapters/ — 端口的具体实现

| 文件 | 实现的端口 |
|------|------|
| `ai/openai_adapter.py` | `AiEnginePort`(OpenAI 兼容 SDK，走代理，多模态)。 |
| `ocr/ddddocr_adapter.py` | `OcrPort`(ddddocr 双模型+PIL 二值化投票，线程池)。 |
| `leaderboard/imgkit_adapter.py` | `LeaderboardGenerator`(imgkit 渲染 HTML→PNG 排行榜)。 |
| `telegram/sender.py` | `MessageSender`+`NotificationPort`(PyrogramMessageSender/PyrogramNotifier)。 |
| `storage/toml_state.py` | `StateRepository`(包装旧 `libs/state.py`)。 |
| `storage/sqlalchemy/` | 各 Repository(`transfer_repo`/`ai_repo`/`redpocket_repo`/`ydx_repo`，委托旧 ORM)。 |

### models/ — 数据库模型（SQLAlchemy 异步 ORM）

| 文件 | 职责 |
|------|------|
| `database.py` | 声明基类(`Base`/`CreateTimeBase`/`TimeBase`)。 |
| `__init__.py` | 引擎与会话工厂。按 `DB_INFO.dbset` 支持 SQLite(默认,WAL)/MySQL/PostgreSQL。`async_session_maker`、`create_all()`。 |
| `transform_db_modle.py` | 核心业务表 `Raiding`/`Transform`(转账)/`User`(用户名+大量统计查询)。 |
| `redpocket_db_modle.py` | `Redpocket`(红包记录)。 |
| `ydx_db_modle.py` | `Zhuqueydx`(猜大小)+`YdxStock`(K线)+MACD/KDJ 指标。 |
| `ai_db_model.py` | `AiMessageModel`(AI 对话历史)。 |
| `alter_tables.py` | 一次性 DDL 迁移脚本(仅 MySQL)。 |
| `db_to_excel.py` | ORM 表导出 Excel/CSV。 |

### libs/ — 通用工具库

| 文件/子目录 | 职责 |
|------|------|
| `custom_client.py` + `client_base/` | **自定义 Pyrogram 客户端**。`Client` 混入四 Mixin：`invoke`(信号量并发+重试+FloodWait/PeerIdInvalid 处理)、`peers`(无效 peer 黑名单)、`session`(会话生命周期/修复损坏 SQLite)、`interaction`(`ask` 交互问答)。发送方法包活跃度统计钩子。 |
| `proxy.py` | 平台代理统一出口。`proxy_url()` 读 config、`export_env()` 写标准代理环境变量让 httpx/requests/aiohttp 自动走代理。 |
| `state.py` | `StateManager` 读写 `data/state.toml` 运行时状态(全局单例 `state_manager`)。 |
| `toml.py` / `toml_images.py` | TOML 读写底层 / TOML 内容渲染成状态图。 |
| `log.py` | 全局 `logger`(CST 时区、RotatingFileHandler、过滤 Pyrogram 噪音)。 |
| `sys_info.py` | 运行状态文本(主机/平台/Python/Pyrogram 版本)。 |
| `session_cleaner.py` | 扫描清理/备份损坏的 `.session` 文件。 |
| `async_bash.py` | 异步执行 shell。 |
| `command_tablepy.py` / `leaderboard_imge.py` | 命令表 / 打赏排行榜 出 PNG(imgkit)。 |
| `others.py` | 杂项(发送黑名单、用户链接构造、延迟删消息、按 TGID 查用户名、日期解析)。 |
| `transfer_helper.py` | 转账记录统一入口 `do_transfer()`(去重+走 TransferService+post-record 钩子)。 |
| `ydx_betmodel.py` | 朱雀猜大小下注策略模型(策略 A/B/E/S，S 用 KDJ)。 |
| `zhuque_listBackpack.py` / `zhuque_recycleMagicCard.py` | 朱雀站点 API(查背包卡片 / 回收魔法卡)。 |

### schedulers/ — 定时任务（APScheduler）

| 文件 | 职责 |
|------|------|
| `__init__.py` | 全局 `AsyncIOScheduler` 单例，`start_scheduler()` 按 state 的 SCHEDULER 开关启动任务。 |
| `universal/auto_avatar.py` | 定时换随机头像(图片池按账号存)。 |
| `universal/auto_changename.py` | 定时把昵称改成报时。 |
| `universal/custom_auto_reply.py` | 多任务定时自动回复。 |
| `universal/log_cleaner.py` | 每天裁剪日志文件到最后 N 行。 |
| `zhuque/fireGenshinCharacterMagic.py` | 朱雀"释放原神角色技能"自动领灵石。 |

### filters/ — 自定义消息过滤器

`custom_filters.py`：Pyrogram 过滤器集合(回复链归属、识别转入/转出方、按 TGID 授权、各站点/抽奖 bot 过滤、回调数据过滤、`.` 前缀命令等)。

### config/ — 配置加载垫片

`config.py`：真正配置在 `data/config.json`(唯一数据源，前端 `/api/settings` 读写)。此文件导入时读 JSON 铺成模块级变量(`API_ID`/`ACCOUNTS`/`BOTS`/`proxy_set`/`DB_INFO`/`PLUGIN_REPOS` 等)，让旧代码 `import config.config as cfg` 无改动可用。含原子写、派生 `MY_NAME`/`MY_TGID`。**不要手动编辑这里的值**，它不是数据源。

### 运行时目录（卷映射，不入 Git）

| 目录 | 内容 |
|------|------|
| `data/` | **运行时数据(唯一配置源)**：`config.json`、`plugins_state.json`(插件启用/配置/账号范围/Bot 路由)、`auth.json`(登录凭据)、`state.toml`、`repo_sync.json`、`activity.json`、`kv/<id>.sqlite`(插件 KV)、`plugin_data/<id>/`(插件文件)、`plugin_deps/`(插件依赖)、`browser_cache/`(浏览器内核)。 |
| `sessions/` | Telegram 会话文件(`.session`)。 |
| `db_file/` | SQLite 数据库 + `dbflag/`(初始化标记)。 |
| `logs/` | 运行日志。 |

---

## 四、关键机制速记

- **插件契约(三段式)**：纯字面量 `__plugin__`(必填 name/id/version/scope，id=文件名) + `setup(ctx)`(注册处理器) + 可选 `teardown(ctx)`。
- **热插拔**：启用=动态 import+setup；停用=注销句柄+teardown+从 sys.modules 移除。全程不重启。处理器必须走 `ctx.on_message` 等(实例级 add_handler)，禁用类级 `@Client.on_message`。
- **group 隔离**：每插件独立 group 基址，避免 Pyrogram 同 group 内互相"吃消息"；`raise ctx.StopPropagation` 主动阻断后续插件。
- **config_schema**：前端据此自动生成配置表单(type/default/label/help/options/section/show_if；类型 string/password/number/boolean/select/multiselect/slider/text/list/chat/action/info)。值存 `plugins_state.json`，插件用 `ctx.config` 读。
- **Vue 模式**：复杂交互插件自带 Vue 界面(模块联邦)，`render_mode:"vue"` + `ctx.on_api` 后端接口。
- **多账号**：scope=user/both 默认挂所有已连接用户账号，可按插件设账号范围(`account_scope`)。
- **多 Bot**：平台配置多个 Bot，逐插件分配(`bot_choice`)，对 `ctx.bot`/`ctx.notify`/handler 挂载一致生效。插件不感知。
- **依赖**：插件声明 `requirements`(PEP508)，启用时平台代装到 `data/plugin_deps`(卷持久化)，冲突则拒绝启用。必须兼容 Python 3.13。
- **安全**：上传/启用=服务器执行代码，全经鉴权；下载不自动启用；敏感配置打码不回显。

---

## 五、构建与运行

**本地开发（Windows + `.venv`，Python 3.13）：**
```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt   # 装依赖(必须在 .venv)
cd webui/frontend && npm install && npm run build             # 构建前端 → webui/static
.venv/Scripts/python.exe main.py                              # 启动平台
```
Web 端口：`data/config.json` 的 `WEB_UI_PORT`(默认 18001)。

**Docker（推荐）：**
```bash
docker compose up -d        # 访问 http://服务器IP:18001
```

**首次使用**：默认登录 `admin/password`(务必到设置改密) → 填 Telegram API_ID/API_HASH/BOT_TOKEN → 账号管理登录 TG 账号 → 插件市场装插件并在「我的插件」开启。

---

## 六、开发规范要点（详见 docs/SPEC.md）

1. 业务一律是插件，禁止往 `kernel/` 塞业务；插件只通过 `ctx` 访问平台。
2. 禁止 `print`(内核用 `logger`，插件用 `ctx.log`)；跨文件用绝对导入；每个包有 `__init__.py`。
3. 关键逻辑加中文注释，尽量加类型提示。
4. 改 `kernel/` 或 `ctx` 接口前先列受影响文件清单；涉及结构/接口变更先更新 SPEC 再改代码。
5. 改动后必须在 `.venv` 验证导入与启动通过。
6. 依赖必须装在 `.venv`，禁止装全局。

---

## 修改记录

> 每次改动代码后在此追加一条（日期 + 改了什么 + 影响文件）。

- 2026-07-15：创建本文件。通读全项目，梳理平台内核+单文件插件架构、六边形底座、前后端结构与各文件职责，形成完整项目说明文档。
- 2026-07-15：修复 vue 模式插件前端 chunk 加载 404。`webui/api.py` 的 `plugin_frontend_asset` 按文件区分缓存头——`remoteEntry.js`(固定名联邦入口) 设 `no-cache` 每次校验新鲜度，避免缓存旧入口指向已删除的 hash chunk；带 hash 的 chunk 设长缓存 `immutable`。影响文件：`webui/api.py`。
- 2026-07-15：本文件从根目录移到 `docs/CLAUDE.md`，同步修正文首指向 SPEC/PLUGIN_GUIDE 的相对链接。
- 2026-07-15：进一步修复 vue 插件配置界面加载 404（`Failed to fetch dynamically imported module`）。上一版的 `no-cache` 只对新响应生效，浏览器早先缓存的旧 `remoteEntry.js` 仍可能被复用、指向已删除的 hash chunk。改为在客户端给 remoteEntry URL 加时间戳 `?t=Date.now()`，强制每次打开配置都取最新入口（chunk 按加载路径相对解析，查询串不带到 chunk，长缓存不受影响）。影响文件：`webui/frontend/src/components/RemotePluginConfig.vue`。
