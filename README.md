<div align="center">
  <img src="webui/frontend/src/assets/logo.png" alt="AWBotNest" width="120" />

  # AWBotNest

  一个 Telegram 机器人平台。所有功能都是**插件**——在网页控制台里点几下就能安装、开关，不用懂代码、不用重启。
</div>

## 它能做什么

- **插件即功能**：想要什么功能就装什么插件，开关一点立即生效。
- **插件市场**：内置官方插件仓库，也能添加你自己的 GitHub 仓库，浏览后一键安装。
- **多账号**：在网页上用手机号登录你的 Telegram 账号，可同时管理多个，随时上线/下线。
- **网页控制台**：插件、账号、日志、定时任务、设置，全在一个深色界面里管理。
- **安全可控**：插件从市场下载后不会自动运行，必须你亲手开启才生效。

## 安装运行（推荐 Docker）

在 Linux 服务器执行下面这一条命令：

```bash
curl -fsSL https://raw.githubusercontent.com/AWdress/AWBotNest/main/install.sh | sudo bash
```

运行后会直接询问安装位置和访问端口，直接按回车会使用默认目录 `/opt/AWBotNest` 和默认端口 `18001`。脚本会自动检查并安装 Docker、拉取最新镜像、创建持久化目录并启动平台。

运行数据会保存在你选择的安装目录中。以后重新执行同一条命令并选择原来的目录，即可拉取最新镜像并更新容器，已有数据不会丢失。

> 如果已经安装 Docker，脚本会直接使用，不会重复安装。需要调整代理、数据库或其他高级选项时，可以编辑所选安装目录里的 `docker-compose.yml`，然后在该目录执行 `docker compose up -d`。

## 第一次使用

1. **登录控制台**：默认账号 `admin`，密码 `password`。进去后请到「系统设置 → 控制台登录」**马上改掉密码**。
2. **填 Telegram 凭据**：到「系统设置 → Telegram 凭据」，填入从 [my.telegram.org](https://my.telegram.org) 申请的 `API_ID` / `API_HASH`，机器人功能再填 [@BotFather](https://t.me/BotFather) 给的 `BOT_TOKEN`。保存后重启平台生效。
3. **登录你的 Telegram 账号**：到「账号管理」，按提示输入手机号 → 验证码 →（如有）两步验证密码，完成登录。
4. **装插件**：到「插件管理 → 插件市场」，挑想要的插件点「安装」，再回「我的插件」打开它的开关。完事。

## 日常使用

- **插件管理**：`我的插件`看已安装的、开关/配置/删除；`插件市场`浏览并安装新插件。每个插件的「配置」按钮就是它的设置面板。
- **账号管理**：登录、查看、上线下线、删除 Telegram 账号。
- **运行日志**：实时查看平台和插件的运行日志，可按级别/关键词过滤。
- **系统状态**：账号在线情况、已加载插件、定时任务、近 24 小时活跃情况。
- **系统设置**：控制台登录密码、Telegram 凭据、Web 控制台、代理、数据库、插件仓库地址。

## 想自己写插件？

一个插件就是一个 `.py` 文件。最简单的样子：

```python
__plugin__ = {
    "name": "我的功能", "id": "my_feature", "version": "1.0.0",
    "scope": "user",   # user=用你的账号 / bot=用机器人 / both=都用
}

async def setup(ctx):
    @ctx.on_message(ctx.filters.text)
    async def handler(client, message):
        await message.reply("收到")
```

写好后在「插件管理」点「上传插件」选这个文件，或放进 GitHub 仓库供市场安装。

详细教程见 **[插件开发指南](docs/PLUGIN_GUIDE.md)**，开发规范见 **[SPEC](docs/SPEC.md)**。

## 常见问题

**忘了控制台密码？** 删掉 `data/auth.json` 文件再重启，会恢复成默认 `admin / password`。

**装了插件但没反应？** 插件下载后默认是关闭的，要去「我的插件」打开开关。

**插件报错/标红？** 在插件卡片上能看到错误原因，多半是插件本身的问题，删掉重装或换一个即可，不影响平台和其他插件。

**数据存在哪？** 全在 `data/` 目录（配置、登录态、插件数据）。备份它就等于备份了整个平台。

---

需要技术细节、部署进阶或二次开发，见 [docs/](docs/)。
