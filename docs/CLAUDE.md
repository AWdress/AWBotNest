# AWBotNest 2 项目开发说明

本文为维护者和代码助手提供项目上下文。用户安装与使用见根目录 `README.md`，插件作者见 `PLUGIN_GUIDE.md`。

开始任何修改前必须完整阅读 [SPEC.md](SPEC.md)。SPEC 是强制性平台规范并具有最高优先级；如需改变规范本身，必须执行其中的「变更协议」。本文只提供日常开发入口，不替代 SPEC。

## 产品边界

AWBotNest 2 是 Telethon 驱动的 Telegram 插件平台，负责：

- 管理 Bot 和用户 Session 生命周期。
- 扫描、安装、启停、重载和隔离插件。
- 提供配置、异步 Storage、Session、Telegram Delivery、HTTP、Cookie、浏览器、AI、通知、Webhook、调度和资源治理。
- 提供 Web 控制台与稳定的 `/api/v1` 开放 API。

V2 是当前唯一实现目标。不得导入、运行或恢复 V1 内核兼容层；历史行为只能作为需求依据，最终接口以 V2 的 SPEC 和插件指南为准。

## 目录

```text
awbotnest/
  api/              FastAPI app composition 与领域 routers
  plugin_runtime/   插件扫描、解析、加载与依赖解析
  services/         HTTP、AI、Browser、Cookie 服务实现
  context.py        Plugin SDK Facade
  storage.py        async SQLite PluginKV
  sessions.py       插件实例内存 Session Runtime
  delivery.py       可选 Telegram 发送治理
  plugins.py        插件生命周期协调
frontend/    Vue 3 管理控制台
static/      前端构建产物
plugins/     插件与模板
data/        配置和业务数据
sessions/    Telethon Session
docs/        API、插件指南、规范和本文件
```

## 运行约定

- Python 3.11+
- Node.js 22（仅前端开发需要）
- 默认端口 `18001`
- Python 命令必须使用项目 `.venv`

Windows：`.\.venv\Scripts\python.exe -m awbotnest.main`

Linux：`./.venv/bin/python -m awbotnest.main`

前端修改后执行 `cd frontend && npm run build`。

## 后端约定

1. `main.py` 负责可重复的运行生命周期和页面内部重启。
2. `api/app.py` 负责 FastAPI composition，领域 endpoint 位于 `api/` routers；`app.py` 仅保留 `create_app` compatibility shim；`open_api.py` 提供稳定的 `/api/v1`。
3. 管理员 Bearer Token 与开放 API Key 是不同凭据；开放 API 接受 `X-API-Key`/`Api-Key`。
4. 所有密钥响应必须遮蔽，日志必须过滤常见敏感字段。
5. 文件写入使用临时文件替换，避免留下半写配置。
6. Windows 与 Linux 均为一等环境；Linux 容器还需考虑 cgroup 限额。
7. Telethon handler、API endpoint 和 scheduler callback 不得直接执行阻塞 I/O；`PluginKV` 的正式方法必须 `await`。
8. `CancelledError` 必须传播，Telethon `StopPropagation` 不得计入失败或熔断。
9. 插件生命周期、并发限制和后台任务由 per-plugin Governor 统一管理，不得新增全局生命周期锁或全局 Telegram send lock。

## 插件与 API 约定

插件通过静态元数据扫描，通过 `PluginContext` 获取能力。平台停用插件时必须撤销事件、路由、调度、后台任务、capability、Session、Delivery、Storage 和 cleanup callback。账号实例的 storage、session、scheduler、delivery、任务和数据目录必须保持隔离。

延迟敏感的短 Telegram 回调可使用 handler 的 `interactive=True` 快路径。它绕过普通 Governor、plugin-wide semaphore、通用超时和 circuit，但必须保留异常隔离、取消传播及生命周期清理；业务一致性由插件使用 `ctx.sessions` 加锁。延迟回归使用 `python benchmarks/interactive_latency.py` 分别检查 handler wrapper、Session lock 和 Telegram API 调用起点。

交互 profiling 由 `AWBOTNEST_INTERACTIVE_PROFILE=1` 显式启用，默认不得产生 trace。慢事件阈值由 `AWBOTNEST_INTERACTIVE_PROFILE_SLOW_MS` 控制；记录只保存在有界内存中且 chat 必须脱敏。真实 Telegram RPC 使用 `benchmarks/telegram_interactive_latency.py` 手动测试，不在 CI 中连接 Telegram。

`/api/v1` 是第三方稳定接口，文档中的每条路径必须有实际实现和 API Key 测试。`/api/*` 主要服务控制台，不在开放 API 文档中承诺稳定。危险操作不进入开放 API；远程源码写入固定禁用。

新增或改变公开插件能力时必须同步 `context.py`、`PLUGIN_GUIDE.md`、`plugins/_TEMPLATE.py` 和相关测试；如果改变平台规则或架构边界，必须按 SPEC 的变更协议同步修改 SPEC。

## 提交前检查

1. 使用项目 `.venv` 完整执行 `python -X dev -W default -m unittest discover -s tests -p "test_*.py"`，不得只运行新增测试。
2. 执行 `python -m compileall -q awbotnest tests` 和 `git diff --check`。
3. 前端修改执行 `npm run build` 及现有前端测试。
4. 验证 `/api/health`、`/api/status` 和受影响接口；开放 API 改动必须使用 API Key 验证真实请求与响应。
5. 检查输出中不存在 `coroutine was never awaited`、pending task、未关闭 client/transport 等资源警告。
6. 验证 Windows 路径与 Linux/Docker 默认路径。
7. 确认未提交配置密钥、Session、Cookie、头像和运行数据。
8. 文档路径、默认端口、API contract 和实际实现保持一致。
