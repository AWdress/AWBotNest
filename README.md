<div align="center">
  <img src="frontend/src/assets/logo.png" alt="AWBotNest" width="120" />

  # AWBotNest 2

  一个 Telegram 机器人与用户账号管理工具。插件、账号、通知、定时任务和日志，都可以在网页里完成管理。
</div>

## 它能做什么

- **插件即功能**：安装、配置、启用和停用都在网页里完成，不用改主程序。
- **插件市场**：从兼容 AWBotNest 2 的仓库浏览和安装插件。
- **多账号与多 Bot**：同时管理 Telegram 用户账号和机器人账号。
- **定时与通知**：插件可以运行定时任务，并通过 Telegram、企业微信、Bark 或 Webhook 推送结果。
- **网页管理**：运行状态、实时日志、账号、插件和系统设置集中在一个控制台。
- **开放 API**：第三方脚本可以用 API Key 管理插件、读写插件数据和发送 Telegram 消息。

AWBotNest 2 使用 Telethon。旧版 Pyrogram/Kurigram 插件和 Session 不能直接使用，需要迁移为 2.0 插件。

## Docker 部署

在项目目录执行：

```bash
docker build -t awbotnest .
docker run -d --name awbotnest \
  -p 18001:18001 \
  -v awbotnest-data:/app/data \
  -v awbotnest-sessions:/app/sessions \
  -v awbotnest-plugins:/app/plugins \
  --restart unless-stopped \
  awbotnest
```

打开 `http://服务器地址:18001`。三个挂载卷分别保存设置与业务数据、Telegram Session 和已安装插件。

## Windows / Linux 本地运行

需要 Python 3.11 或更高版本。所有 Python 命令都应在虚拟环境中执行。

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m awbotnest.main
```

Linux：

```bash
source .venv/bin/activate
python -m pip install -e .
python -m awbotnest.main
```

管理页面默认地址为 `http://127.0.0.1:18001`。

如需修改前端，需要 Node.js 22：

```bash
cd frontend
npm install
npm run build
```

## 第一次使用

1. 打开管理页面，设置管理员用户名和密码。
2. 到「系统设置 → 账号与凭据」填写从 [my.telegram.org](https://my.telegram.org) 获取的 `API_ID` 和 `API_HASH`。
3. 需要 Bot 时，到「通知渠道」添加 Telegram 渠道并填写 [@BotFather](https://t.me/BotFather) 提供的 Bot Token。
4. 保存后按页面提示重启平台。
5. 到「账号管理」输入手机号、Telegram 验证码和可选的两步验证密码，登录用户账号。
6. 到「插件管理 → 插件市场」安装插件，再到「我的插件」配置并启用。

如果当前网络不能直连 Telegram，请在「系统设置 → 运行环境 → 运行代理」填写完整地址，例如 `socks5://127.0.0.1:7890`，测试成功后保存并重启。

## 日常使用

- **运行概览**：查看 CPU、内存、账号、插件、活动与定时任务。
- **账号管理**：登录用户账号，控制账号上线、下线和删除。
- **插件管理**：安装、更新、配置、启停、重载和自检插件。
- **运行日志**：实时查看平台与插件日志并按条件筛选。
- **系统设置**：配置 Telegram、通知、代理、AI、Cookie 服务、开放 API 和备份。

## 数据与备份

- `data/`：平台设置、插件配置、KV、头像和业务数据。
- `sessions/`：Telegram 登录 Session，等同于账号登录凭据，必须妥善保护。
- `plugins/`：已安装插件。

备份前建议停止平台写入。不要公开 `data/config.json`、`sessions/` 或任何 Token、API Hash、验证码和密码。

## 想自己写插件？

最简单的插件只有一个 Python 文件：

```python
__plugin__ = {
    "id": "hello_world",
    "name": "Hello World",
    "version": "1.0.0",
    "scope": "standalone",
}

async def setup(ctx):
    ctx.log.info("Hello World 已启动")
```

从 `plugins/_TEMPLATE.py` 开始最快。完整说明见 [插件开发指南](docs/PLUGIN_GUIDE.md)，硬性兼容要求见 [SPEC](docs/SPEC.md)。

## 开放 API

在「系统设置 → 开放接口」生成 API Key 后，第三方工具可调用 `/api/v1`。接口列表、认证和示例见 [开放平台 API 文档](docs/API.md)。

## 常见问题

**Telegram 验证码发送失败？** 先确认 API ID/API Hash 正确，再测试到 Telegram 的代理连接。连接失败发生在验证码发送之前，与手机号格式无关。

**插件安装后没反应？** 市场安装不会自动启用；回到“我的插件”打开开关，并检查插件需要的账号和配置。

**插件为什么不在市场出现？** AWBotNest 2 只读取带 `manifest_v2.json` 的兼容仓库，避免误装旧版插件。

**生产环境如何开放访问？** 建议让 AWBotNest 只监听可信网络，并使用 Nginx/Caddy 配置 HTTPS；不要直接把管理端口暴露到公网。

更多开发资料见 [docs](docs/)。
