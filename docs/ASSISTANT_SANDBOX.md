# 独立助手沙箱

这是一项可选功能。未部署执行服务、未填写连接凭据或未打开开关时，助手仍按原来的方式查询和调用插件，不会在主系统运行代码。

## 能做什么

助手可在当前会话的临时工作目录中运行 Python 或 Shell、列出和读写文件，或使用独立 Chromium 浏览器打开网页、查看内容、点击、填写、选择、执行网页脚本和截图。每次操作都显示具体内容，确认后才执行。

这些操作发生在沙箱，不是系统服务器。沙箱中没有现有插件、配置、Telegram Session、CookieCloud 数据或系统密钥。助手生成了代码，不等于已安装插件；修改了沙箱文件，也不等于修改了系统源码。发布、安装及 GitHub PR 仍不能靠一句成功提示代替实际结果。

浏览器可保留本次沙箱会话的网页状态；请求只允许公网 HTTP(S)，不访问本机、内网、Docker 服务或云元数据地址。不支持 WebSocket、任意端口、附件下载或无限量资源加载。网页资料仍不可信，网页中的“确认”不能批准系统操作。

## 隔离方式

主系统通过管理员设置的地址调用 broker，不安装 Docker SDK，也不挂载 Docker socket。broker 是独立的受信管理服务，由它创建执行容器。

每个会话使用单独容器。runner 使用非 root 用户、只读根目录、临时工作目录、资源和时长上限，没有系统目录或 Docker socket 挂载，也没有直接网络。网页资源由 broker 验证全部 DNS 地址后固定公网 IP 访问，并保留 HTTPS 域名证书验证。公网重定向仍须经过下一次地址验证。

正常完成后可在同一会话继续使用临时文件；空闲、停止、超时、连接配置变化或结果未知时会清理。不要把沙箱当作长期存储，不要在其中填写系统密钥。取消不会撤销已经发送到外部网站的操作。

Docker 不是绝对安全边界。broker 的 Docker socket 等同高权限管理入口，只应部署到受信 Docker 主机；不要暴露到公网、不要转发给插件或模型。处理不可信代码时，更建议把执行服务放在独立主机。此功能默认关闭，不应为了方便给主系统容器增加特权、宿主目录或 Docker socket。

## Docker Compose 部署

以下方式需要 Linux Docker 主机、Docker Compose，以及与当前版本对应的源码。先完成系统原有部署，再在源码根目录操作；不要对不明来源的镜像或脚本提供 Docker 权限。

1. 创建 `secrets` 目录，生成专用随机令牌文件。令牌只用于系统连接执行端，不是 AI、GitHub 或 Telegram 的密钥。

   ```sh
   mkdir -p secrets
   umask 077
   python3 -c "from pathlib import Path; import secrets; p=Path('secrets/assistant-sandbox.token'); assert not p.exists(), '令牌已存在，不覆盖'; p.write_text(secrets.token_urlsafe(32), encoding='utf-8')"
   ```

   `.token` 文件已被 Git 和 Docker 构建忽略。不要提交、截图或把令牌发给助手。

2. 构建两个执行端镜像，再只启动 broker。

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.sandbox.yml build assistant-runner assistant-sandbox
   docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d assistant-sandbox
   ```

   `assistant-runner` 是镜像构建辅助服务，不要手动常驻启动。broker 会按需创建并回收会话容器；现有系统镜像不会因此变大。首次构建会下载 Chromium 及执行依赖。

3. 打开系统助手的「设置 → 独立沙箱」。同一 Compose 网络使用 `http://assistant-sandbox:8765`，令牌填写刚才文件中的值，打开开关后保存。

   保存只记录配置，不表示已经连接。点击「检查连接」查看实际状态。还需在助手设置中允许执行修改操作；每次执行仍有确认卡。

   此配置默认不发布主机端口。系统使用 host 网络或执行端在其他主机时，须另外配置受保护的连接入口；远程连接使用 HTTPS，不要直接把 8765 暴露到公网。

4. 在助手中要求「在沙箱用 Python 计算 6×7」。确认卡应只包含该代码，返回结果应为 42；再要求写入、读取一个临时文本文件，验证同一会话的文件隔离。

   浏览器、执行超时和停止后的容器清理也应在实际主机验证。GitHub 构建会验证两种架构的容器启动、隔离和回收，浏览器部分使用离线 HTML；这不代表公网网站、重定向或交互流程已在你的网络环境中通过实测。

## 停用和更新

先停止正在执行的助手任务，再关闭独立沙箱开关。要停止执行服务：

```sh
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml stop assistant-sandbox
```

更新前先等待执行完成，再重新构建两个镜像并重新创建 broker。工作目录只作临时用途，不保证跨重建保留；助手会话和配置仍保存在原来的助手数据库中。

执行失败或结果不确定时，不要反复批准同一外部写操作。先核对网站实际结果。连接错误只返回简短原因，不把上游响应、连接凭据或 Docker 错误全文交给模型。

Docker 创建容器的结果不确定时，该会话会被隔离，并继续占用两个会话名额之一，避免重试不断创建容器。先恢复 Docker，再重启 broker；broker 启动时会按专属标签清理遗留执行容器。不要通过反复修改连接令牌绕过隔离状态。若清理失败，执行服务不会继续接收操作。

更换连接令牌前，先停止 broker，并确认旧执行容器已经回收，再替换令牌文件和系统设置，最后启动 broker。专属标签与令牌相关，更换令牌后不会自动清理旧令牌所属的容器；若 Docker 故障导致清理未完成，先保留原令牌恢复和清理，不要直接换令牌重建。
