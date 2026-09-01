# AWBotNest 2 项目开发说明

本文为维护者和代码助手提供项目上下文。用户安装与使用见根目录 `README.md`，插件作者见 `PLUGIN_GUIDE.md`。

## 产品边界

AWBotNest 2 是 Telethon 驱动的 Telegram 插件平台，负责：

- 管理 Bot 和用户 Session 生命周期。
- 扫描、安装、启停、重载和隔离插件。
- 提供配置、KV、HTTP、Cookie、浏览器、AI、通知、Webhook 和调度服务。
- 提供 Web 控制台与稳定的 `/api/v1` 开放 API。

V1 仅用于产品与文档参考。V2 不得导入或运行 V1 代码，不得修改 V1 目录。

## 目录

```text
awbotnest/   FastAPI、Telethon、插件运行时和平台服务
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
2. `app.py` 提供控制台内部 API；`open_api.py` 提供稳定的 `/api/v1`。
3. 管理员 Bearer Token 与开放 API Key 是不同凭据；开放 API 接受 `X-API-Key`/`Api-Key`。
4. 所有密钥响应必须遮蔽，日志必须过滤常见敏感字段。
5. 文件写入使用临时文件替换，避免留下半写配置。
6. Windows 与 Linux 均为一等环境；Linux 容器还需考虑 cgroup 限额。

## 插件与 API 约定

插件通过静态元数据扫描，通过 `PluginContext` 获取能力。平台停用插件时必须撤销事件、路由、调度与后台任务。

`/api/v1` 是第三方稳定接口，文档中的每条路径必须有实际实现和 API Key 测试。`/api/*` 主要服务控制台，不在开放 API 文档中承诺稳定。危险操作不进入开放 API；远程源码写入固定禁用。

新增插件能力时同步 `context.py`、`PLUGIN_GUIDE.md`、必要时的 `SPEC.md` 和 `plugins/_TEMPLATE.py`。

## 提交前检查

1. `.venv` 中执行 `python -m compileall -q awbotnest`。
2. 前端修改执行 `npm run build`。
3. 验证 `/api/health`、`/api/status` 和受影响接口。
4. 验证 Windows 路径与 Linux/Docker 默认路径。
5. 确认未提交配置密钥、Session、Cookie、头像和运行数据。
6. 文档路径、默认端口和实际实现保持一致。
