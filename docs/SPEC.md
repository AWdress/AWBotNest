# AWBotNest 平台开发规范 (SPEC)

> **版本**: 1.0.0
> **生效日期**: 2026-06-27
> **适用范围**: AWBotNest 平台所有内核、插件、前端代码。
> **强制性**: 本规范是后续所有改动的依据。任何修改必须遵守；如需变更规范本身，见第 11 节「变更协议」。

---

## 0. 设计哲学

AWBotNest 是一个 **平台内核 + 单文件插件** 的机器人平台：

- **内核 (kernel/)** 只提供能力：账号生命周期、插件加载/卸载、给插件的统一上下文。内核稳定、极少改动。
- **插件 (plugins/)** 承载所有业务功能。一个功能 = 一个 `.py` 文件。用户在前端上传、勾选启用、即时生效。
- **前端 (webui/)** 是用户的唯一操作台。

一句话：**想要啥功能，写一个文件丢进去，勾选启用，完事。**

---

## 1. 目录结构

```
AWBotNest/
├── kernel/                  # 内核（稳定，少改）
│   ├── __init__.py          # 统一出口
│   ├── account_manager.py   # 账号生命周期
│   ├── plugin_runtime.py    # 插件加载/卸载/热插拔
│   ├── context.py           # PlatformContext（给插件的能力）
│   └── registry.py          # 插件元数据 + 启用状态持久化
│
├── plugins/                 # 用户的单文件插件（业务都在这）
│   ├── _TEMPLATE.py         # 插件模板（_ 开头不被识别为插件）
│   └── *.py                 # 每个文件一个插件
│
├── webui/                   # 前端 + API
│   ├── api.py               # FastAPI 后端
│   ├── auth.py              # 鉴权
│   ├── static/              # 前端构建产物（FastAPI 托管）
│   └── frontend/            # Vue3 + Vite 源码
│
├── core/ infra/ adapters/ models/ libs/ schedulers/ filters/
│                            # 复用自旧项目的底座（统一出口仍是 core/）
├── config/                  # 平台代码（垫片 config.py，不做卷映射）
├── data/                    # 运行时数据（卷映射）：config.json（唯一配置源）、
│                            # plugins_state.json、auth.json、state.toml、kv/、游戏状态等
├── sessions/                # Telegram 会话文件
├── db_file/                 # SQLite 数据库
├── docs/                    # 文档：SPEC.md（本文件）、PLUGIN_GUIDE.md、设计参照图
├── README.md                # 项目说明（根目录）
└── main.py                  # 平台入口
```

---

## 2. 内核与插件分离（核心铁律）

1. **业务一律是插件**。禁止往 `kernel/` 塞任何业务逻辑。内核只提供通用能力。
2. **能力经 ctx**。插件只能通过 `PlatformContext`（`ctx`）访问平台。插件中**禁止**：
   - `import pyrogram`（用 `ctx.filters` / `ctx.on_message`）
   - 直接 `from config.config import ...`（用 `ctx.config`）
   - `from core import ...` / `from kernel import ...`（用 `ctx` 提供的能力）
3. 内核可以引用底座（core/infra/...），插件不可以直接引用底座。

---

## 3. 单文件插件契约

每个插件 = `plugins/` 下一个 `.py` 文件，必须满足三段式：

### 3.1 元数据 `__plugin__`（必填）

纯字面量字典（平台用 AST 静态解析，不执行代码即可读取）：

```python
__plugin__ = {
    "name": "举牌",            # 必填：前端显示名
    "id": "jupai",             # 必填：必须等于文件名（去 .py）
    "version": "1.0.0",        # 必填
    "scope": "user",           # 必填：user | bot | both
    "author": "AW",            # 可选
    "description": "...",       # 可选：前端展示
    "icon": "",                # 可选：图标 URL，前端卡片用；空则回退平台 logo
    "default_enabled": False,  # 可选：上传后是否默认启用
    "config_schema": {...},    # 可选：前端自动生成配置表单
}
```

- 缺必填字段、`id` ≠ 文件名、`scope` 非法 → 前端标红，禁止启用。
- `icon` 在「我的插件」与「插件市场」卡片展示；GitHub 发布时 `manifest.json` 的 `icon` 用于市场，二者保持一致。

### 3.2 `setup(ctx)`（必填）

启用时调用，在此注册处理器。可为 `async` 或同步函数。

```python
async def setup(ctx):
    @ctx.on_message(ctx.filters.text, group=-10)
    async def handler(client, message):
        ...
```

### 3.3 `teardown(ctx)`（可选）

停用时调用，释放**自管理**资源。`ctx.on_message` / `ctx.schedule` 注册的东西由平台自动清理。

### 3.4 单文件单插件 / 文件夹插件

插件 ID 全局唯一，支持两种形态：

- **单文件**：`plugins/<id>.py` —— 文件名 = 插件 ID。简单插件用这个。
- **文件夹**：`plugins/<id>/__init__.py` —— 目录名 = 插件 ID。复杂插件（带辅助模块、资源、图标）用这个，`__plugin__` 与 `setup` 写在 `__init__.py`，目录内可正常 `from .xxx import ...`（作为包导入）。同名时单文件优先。

约定：
- `__plugin__["id"]` 必须等于文件名 / 目录名。
- 以 `_` 开头的文件/目录不被识别为插件（用于模板、私有辅助）。
- **插件之间禁止互相 import**。共享逻辑下沉为平台服务；插件内部辅助放在自己的文件夹里（文件夹形态）或 `_` 开头同目录文件。

---

## 4. 热插拔（必须支持）

1. 所有处理器经 `ctx.on_message` / `ctx.on_callback` 注册——它们内部用实例级 `client.add_handler`，并登记句柄。**禁止使用类级 `@Client.on_message`**（无法热卸载）。
2. 所有自管理资源（定时任务、连接、后台 task）必须可在 `teardown` 或通过 `ctx.add_cleanup` 释放。
3. 启用 = 导入文件 + `setup`；停用 = 注销句柄 + `teardown` + 从 `sys.modules` 卸载。全程不重启进程。
4. **容错**：单个插件 `setup` 抛异常只标记该插件 `error`，不影响内核与其它插件。

---

## 5. 数据与配置

0. **平台配置存 `data/config.json`（唯一数据源）**。
   - `config/config.py` 只是**加载垫片**：导入时读 `data/config.json`，把各项暴露成模块级变量（`API_ID` / `ACCOUNTS` / `proxy_set` / `DB_INFO` 等），使旧代码的 `import config.config as cfg` + `getattr` 无改动可用。**不要手动编辑 config.py 里的值**，它不是数据源。
   - 平台级配置（登录凭据、账号、Web 控制台、ngrok、代理、数据库）全部在前端「系统设置」页修改，经 `GET/PUT /api/settings` 读写 `data/config.json`。敏感字段读取时打码、写入时跳过打码值保留原值。
   - 部分关键项（API 凭据等）改后需重启平台生效，接口返回 `restart_required`。
1. **插件自带配置，禁止碰平台配置**。
   - 平台级配置只含：登录凭据(API_ID/HASH/BOT_TOKEN)、账号(ACCOUNTS)、Web 控制台、ngrok、运行代理、数据库(DB_INFO，平台存储基础设施)。
   - **不含任何业务数据**：群组 ID(PT_GROUP_ID)、抽奖/奖品/陷阱/AI/炸弹等全部属于插件，写在各插件的 `config_schema` 里。
   - 业务功能的所有参数（开关、密钥、群组、文案等）一律写进插件自己的 `__plugin__["config_schema"]`，由前端「配置」按钮自动生成 UI，值存于 `data/plugins_state.json`。插件用 `ctx.config` 读取。
   - 严禁插件向平台配置写入或依赖业务键。旧项目的完整配置已归档在 `config/config.legacy.py`，仅供迁移时参照。
2. **config_schema 字段规范**（前端据此渲染设置界面）：
   ```python
   "字段名": {
       "type": "string|password|number|boolean|select|multiselect|slider|text",  # 必填
       "default": ...,          # 必填：默认值（multiselect 用 list，slider/number 用数字）
       "label": "显示名",        # 建议
       "help": "字段说明",       # 可选：显示在字段下方
       "options": [...],         # select/multiselect 必填；可为 ["a","b"] 或 [{value,label}]
       "min": 0, "max": 100, "step": 1,  # number/slider 可选
       "section": "分区标题",     # 可选：同 section 的字段在 UI 里归为一组卡片
       "show_if": {"其他字段": 值},  # 可选：条件显示，仅当该字段当前值匹配才显示本字段
   }
   ```
   - 字段类型：`string`(单行)/`password`(密码)/`number`(数字)/`boolean`(开关)/`select`(下拉)/`multiselect`(多选标签)/`slider`(滑块)/`text`(多行)。
   - 用 `section` 把「功能开关」与「参数」分块；用 `show_if` 做联动（如某开关打开才显示相关参数），实现「打开插件 = 一个带分区、会联动的设置面板」。
3. **数据隔离**：插件用 `ctx.kv` 存键值，每插件独立 sqlite 命名空间（`data/kv/<id>.sqlite`）。需要关系型存储时，表名/键名必须带 `plugin_id` 前缀，禁止污染他人数据。
4. 平台级敏感配置（API_ID/HASH/BOT_TOKEN 等）存 `data/config.json`，**`data/` 禁止提交 Git**，禁止在日志/响应中回显明文（`/api/settings` 读取时打码）。
5. **可写数据目录 `ctx.data_dir`**：需要存实际文件（如头像图片池、下载的素材）的插件用 `ctx.data_dir` 拿一个**每插件独立**的可写目录 `data/plugin_data/<id>/`（`Path`，首次访问自动建）。`ctx.kv` 只存键值，文件存这里。

---

## 6. 前端规范

1. **技术栈**：Vue3 + Vite，构建产物输出到 `webui/static/`，由 FastAPI 托管。
2. **视觉**：深色控制台风格，参照 `web 示例.png`。设计 token（背景/卡片/强调/文字色）取自该示例图，集中定义为 CSS 变量，禁止散落硬编码颜色。
3. **布局**：左侧「图标+文字」固定侧边栏 + 右侧主面板，当前页高亮。
4. **页面**：插件管理（卡片+开关+上传）、插件配置（schema 自动表单）、账号管理、运行日志、系统状态、系统设置（平台配置编辑）。
5. **强调色/圆角/间距**统一走 token，组件风格一致。

---

## 7. 安全

1. **上传/导入 .py = 服务器执行任意代码**。`/api/plugins/upload`、`/api/plugins/github/*`、`enable`、`disable`、`delete`、配置写入、`/api/settings` 等接口**必须**经过鉴权依赖（`require_auth`，见 `webui/auth.py`）。
2. 鉴权方式：用户名+密码登录，PBKDF2 哈希存 `data/auth.json`，令牌为无状态 HMAC（重启不失效、改密码自动失效）。前端发 `Authorization: Bearer <token>`。本地开发可设 `AWBOTNEST_DEV_NO_AUTH=true` 放开鉴权，**生产环境严禁**。
3. 不读取/回显密钥明文。新增对外网络出口需在 PR/说明中标注。
4. 上传/导入文件名校验：仅 `.py`、禁止路径穿越、禁止 `_` 开头覆盖模板/辅助。
5. GitHub 导入只下载与保存，不自动启用；导入的代码与本地上传同等对待（启用时才执行）。

## 7.5 GitHub 仓库导入（插件市场）

平台可从 GitHub 仓库导入插件（前端「从 GitHub 导入」）。约定：

1. **优先读市场清单 `manifest.json`**（或 `manifest.v2.json`，放仓库根或子目录）。有清单则渲染成插件市场（名称/版本/作者/图标/描述）。格式（对象，key=插件 id）：
   ```json
   {
     "jupai":   {"name":"举牌","version":"1.0.0","author":"AW","description":"...","icon":"https://.../i.png","path":"jupai.py"},
     "lottery": {"name":"抽奖","version":"2.0.0","path":"lottery/"}
   }
   ```
   `path` 指向入口：单文件以 `.py` 结尾，文件夹以 `/` 结尾（导入时递归下载整个目录）。
2. **无清单则目录扫描**：列仓库根或 `plugins/` 下的 `.py` 单文件与 `<id>/__init__.py` 文件夹插件。`_` 开头忽略。
3. **每个插件仍须符合插件契约**（`__plugin__` + `setup`）。
4. **支持的来源格式**：
   - `owner/repo`、`owner/repo/子目录`
   - `https://github.com/owner/repo`（可带 `/tree/分支/子目录`）
   - 直接 raw 链接：`https://raw.githubusercontent.com/owner/repo/分支/路径/plugin.py`
5. **私有仓库**：前端可填 GitHub token（通过 Authorization 头访问），token 不落盘。
6. 导入 = 下载落盘到 `plugins/`（文件夹插件保留目录结构）+ 静态校验元数据，**不自动启用**；用户在列表里手动开启。

## 7.6 插件商店 / 仓库自动同步（多仓库）

平台可配置**多个** GitHub 插件仓库，聚合成「插件商店」。插件管理页分两段：
**我的插件**（本地已下载）+ **插件商店**（仓库里尚未下载的，逐个「下载」）。
仓库地址在插件页「设置仓库地址」对话框管理。

1. **配置项**（平台级，存 `data/config.json`，前端可改）：
   - `PLUGIN_REPO_ENABLE`：**已废弃**——轮询强制常开，不再受此开关控制（仍写 `true` 兼容旧字段）。
   - `PLUGIN_REPOS`：仓库列表 `[{"url": "...", "token": "..."}, ...]`。`url` 格式同 §7.5；`token` 为私有仓库令牌（敏感，打码存储）。官方仓库 `AWdress/AWBotNest-Plugins` 由平台内置，不在此列。
   - `PLUGIN_REPO_INTERVAL`：轮询间隔（分钟，默认 20，最小 1）。
2. **仓库格式**：与 §7.5 完全一致——优先 `manifest.json`（推荐，带版本号才能识别"更新"），无清单则目录扫描。多仓库出现同 id 插件时先到先得。
3. **插件商店（显示，不自动下载）**：聚合所有仓库的插件列表，标记 `installed`。商店只展示**未安装**的；用户点「下载」才落盘。下载 = 写入 `plugins/`，**绝不自动启用**（启用 = 在服务器执行远程代码，须用户到「我的插件」手动开启，同 §7.5 安全铁律）。
4. **自动轮询只做两件事**（不自动下载新插件）：
   - ①刷新商店列表缓存（让仓库新插件出现在商店）；
   - ②对**已安装**插件，若 manifest 版本号变化则下载覆盖更新；**若该插件当前正在运行，则自动热重载使新代码生效**（未运行的只更新文件、不自动启用，保持 §7.5）。无版本信号的不动；用户手动停用的插件，轮询不碰其启用状态。
5. **任务展示**：轮询任务以平台级身份（id `插件仓库轮询`）注册到 scheduler，显示在「系统状态」页定时任务卡片。
6. **触发时机**：平台启动 + 设置变更（仓库/间隔）即时重排任务（轮询常开，无需开关）。插件页「刷新市场」按钮手动拉取（`GET /api/plugins/store?refresh=true`），「下载」走 `POST /api/plugins/store/download`。
7. 商店缓存 + 各插件已知版本存 `data/repo_sync.json`。

---

## 8. 代码风格与日志

1. 关键逻辑、公共 API 必须有清晰中文注释。
2. 尽量加类型提示。
3. **禁止 `print`**。内核用 `logger`，插件用 `ctx.log`（自动带 `[插件id]` 前缀）。
4. 跨文件引用用绝对导入（`from kernel.registry import ...`），禁止模糊相对引用。
5. 每个包目录必须有 `__init__.py`。

### 8.1 通知中心（插件 → 平台 → 主人）

插件**不直接**发通知，而是提交给平台通知中心 `kernel/notifier.py`，由平台统一处理：

1. 插件调 `await ctx.notify(text, level="info", category=None, account=client)` —— 只提供内容、级别、分类，以及（多账号时）触发的账号。
2. 平台 `notifier.submit` 负责**分类与统一格式**：按 `level`（info/success/warning/error）打图标标签，前缀插件名 + 可选 `category`；**多账号场景标注账号名**（从传入的 `account` client 解析 `me.first_name`→session 名，与账号管理页一致），让主人知道是哪个账号的消息。
3. 平台**统一投递**给平台主人：优先 Bot 私聊（`MY_TGID`，需主人 /start 过 Bot），Bot 不可用时回退主账号「收藏夹」。
4. 每条通知同时记入运行日志（带插件名）与通知中心历史环形缓冲（最近 200 条）。

「发给谁、什么格式、怎么投递」是平台策略，插件不实现也不绕过——禁止插件为了发通知自己拼 `ctx.bot.send` 给 `owner_id`，统一走 `ctx.notify`。

---

## 9. 异步规范

1. 禁止 `app.run()`。统一 `asyncio.run(main())` + `await`。
2. 数据库统一 SQLAlchemy 2.0 async（`AsyncSession`）。
3. 插件 `setup`/`teardown` 可 async，平台会正确 await。

---

## 10. 环境与运行

| 项目 | 规范 |
|------|------|
| 操作系统 | Windows 11 |
| Shell | bash (Git Bash) |
| 虚拟环境 | `.venv/`（Python 3.13） |
| 运行 | `.venv/Scripts/python.exe main.py` |
| 依赖安装 | `.venv/Scripts/python.exe -m pip install -r requirements.txt` |
| 前端构建 | `cd webui/frontend && npm install && npm run build` |
| Web 端口 | `data/config.json` 的 `WEB_UI_PORT`（当前 18001） |

**所有依赖必须装在 `.venv` 虚拟环境内，禁止装到全局。**

---

## 11. 变更协议

1. 改动前先读本文件。
2. 改 `kernel/` 或 `ctx` 接口前，先输出受影响文件清单，经确认再动。
3. 涉及结构/接口变更，先更新本 SPEC 再改代码（文档先行）。
4. 严禁循环依赖，发现立即停止并报告。
5. 改动后必须在 `.venv` 中验证导入与启动通过。

---

## 附录：内核能力速查（ctx 提供）

| 能力 | 用法 |
|------|------|
| 过滤器 | `ctx.filters.text` 等 |
| 注册消息 | `@ctx.on_message(filter, group=0, target="auto")` |
| 注册回调 | `@ctx.on_callback(filter, group=0, target="auto")` |
| 中断传播 | `raise ctx.StopPropagation`（在 handler 内主动阻止后续插件处理这条消息） |
| Bot 发送 | `await ctx.bot.send(chat_id, text)` |
| 用户发送 | `await ctx.user.send(chat_id, text)` |
| 多账号列表 | `ctx.user_apps`（所有已连接用户账号；未连接时发送代理抛 `RuntimeError`，可判 `ctx.bot/user.connected`） |
| 通知所有者 | `await ctx.notify(text, level="info", category=None, account=client)`（提交给平台通知中心 → 平台分类+统一格式+标注账号 → Bot 发给主人，回退主账号收藏夹） |
| 所有者 ID | `ctx.owner_id`（平台主人 Telegram 数字 ID，无主账号为 0） |
| 配置 | `ctx.config`（dict） |
| 键值存储 | `ctx.kv.get/set/delete/keys`（每插件私有） |
| 可写目录 | `ctx.data_dir`（`Path`，每插件独立 `data/plugin_data/<id>/`） |
| 日志 | `ctx.log.info/debug/warning/error`（自动带 `[插件id]` 前缀，前端日志页可见） |
| 定时任务 | `ctx.schedule(fn, "interval", seconds=60)`（可传 `id="名称"`，自动归属本插件并显示在系统状态页） |
| 清理回调 | `ctx.add_cleanup(fn)` |

`target`: `"user"` / `"bot"` / `"both"` / `"auto"`（按插件 scope 自动选择）。

**group 隔离（防止互相"吃消息"）**：Pyrogram 在同一 group 内只执行第一个匹配的 handler 即跳出该组。平台为**每个插件分配独立的 group 基址**（`PluginRuntime._group_base_for`，步长 1000），`ctx.on_message/on_callback` 把插件写的 `group=` 当作「**插件内相对优先级**」平移到该区间。因此：① 不同插件监听同类消息互不抢占，都能收到；② 单个插件内部仍可用多个相对 group 排序（数值越小越先）。插件作者无需关心其它插件的 group。若插件希望"我处理后不让后续插件再处理"，在 handler 内 `raise ctx.StopPropagation`。

**多账号下的账号范围**：`scope=user`/`both` 的插件默认挂到**所有**已连接用户账号；用户可在插件卡片「账号」按钮里选择只应用到部分账号（前端 `PUT /api/plugins/<id>/accounts`，空数组=全部）。范围存于 `data/plugins_state.json` 的 `account_scope`，由 `ctx._scoped_user_apps` 按 client 的 session 名统一过滤。改动后自动重载重挂 handler。

该范围对 handler 挂载、`ctx.user`、`ctx.user_apps` **一致生效**（三者共用 `_scoped_user_apps`）：只勾选一个账号时，处理器只挂到该账号，主动发送类逻辑遍历 `ctx.user_apps` 也只拿到该账号，不会出现「只勾一个账号、两个账号都在发消息」。
