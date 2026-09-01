# AWBotNest 2 开放平台 API

AWBotNest 提供 `/api/v1` REST API，供自动化脚本、AI 助手和第三方系统管理插件、读写插件数据以及发送 Telegram 消息。

本文只描述稳定的开放 API，不包含 Web 控制台使用的内部 `/api/*` 接口。

## 认证

先在「系统设置 → 开放接口」生成并保存 API Key。每个请求携带以下任一请求头：

```http
X-API-Key: your_api_key
```

```http
Api-Key: your_api_key
```

基础地址为 `http://127.0.0.1:18001/api/v1`。请求和响应均使用 JSON、UTF-8。API Key 未配置时返回 `503`，缺失或错误时返回 `401`。

## 插件管理

### 列出插件

```http
GET /api/v1/plugins
```

```json
{
  "plugins": [{
    "id": "hello", "name": "启动检查", "version": "1.0.0",
    "scope": "standalone", "enabled": true, "loaded": true, "error": ""
  }]
}
```

### 插件详情与源码

```http
GET /api/v1/plugins/{plugin_id}
GET /api/v1/plugins/{plugin_id}/source
```

源码响应包含 `plugin_id`、相对 `path`、`source` 和 `is_package`。

远程修改源码已禁用：

```http
PUT /api/v1/plugins/{plugin_id}/source
```

该接口固定返回 `403`。修改源码等同远程执行代码，只能通过可信的本地文件管理或插件安装流程完成。

### 启用、停用、重载

```http
POST /api/v1/plugins/{plugin_id}/enable
POST /api/v1/plugins/{plugin_id}/disable
POST /api/v1/plugins/{plugin_id}/reload
```

成功响应：

```json
{"ok": true, "message": "插件已启用"}
```

## 插件配置

```http
GET /api/v1/plugins/{plugin_id}/config
PUT /api/v1/plugins/{plugin_id}/config
```

保存请求：

```json
{"config": {"keyword": "hi"}}
```

平台按插件 `config_schema` 校验。已加载插件保存后会自动重载。

## 插件 KV 数据

```http
GET    /api/v1/plugins/{plugin_id}/kv
GET    /api/v1/plugins/{plugin_id}/kv/{key}
PUT    /api/v1/plugins/{plugin_id}/kv/{key}
DELETE /api/v1/plugins/{plugin_id}/kv/{key}
```

设置值的请求体：

```json
{"value": 42}
```

单值最大 10 MB，单插件 KV 数据库最大 256 MB。

## 发送 Telegram 消息

```http
POST /api/v1/messages/send
Content-Type: application/json
```

```json
{
  "chat_id": -1001234567890,
  "text": "Hello from AWBotNest",
  "sender": "bot",
  "session": "default",
  "parse_mode": "HTML"
}
```

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `chat_id` | 是 | Telegram 会话 ID 或可解析的用户名 |
| `text` | 是 | 消息正文 |
| `sender` | 否 | `bot` 或 `user`，默认 `bot` |
| `session` | 否 | Bot ID 或用户 Session 名；省略时使用默认 Bot/首个在线用户 |
| `parse_mode` | 否 | 例如 `HTML` 或 `Markdown` |

成功响应包含 `ok`、`message_id`、`chat_id` 和 `date`。

## 查询会话

```http
GET /api/v1/chats/{chat_id}?session=user_account
```

此接口使用已连接的用户账号查询。返回 `id`、`title`、`username` 和 `type`。

## 账号列表

```http
GET /api/v1/accounts
```

```json
{
  "accounts": [
    {"id": "default", "kind": "bot", "connected": true},
    {"id": "user_account", "kind": "user", "connected": true}
  ]
}
```

开放 API 不提供手机号登录、验证码提交、Session 删除或密钥读取接口；这些高风险操作仅在管理控制台完成。

## 日志

```http
GET /api/v1/logs?limit=100
GET /api/v1/logs/plugins/{plugin_id}?limit=100
```

`limit` 范围为 1–1000。第三方程序应把日志当作敏感数据处理。

## 平台状态

```http
GET /api/v1/status
```

```json
{
  "version": "2.0.0.dev1",
  "bot_connected": true,
  "user_accounts_count": 1,
  "total_plugins": 5,
  "enabled_plugins": 3,
  "enabled_plugin_ids": ["a", "b", "c"]
}
```

## Webhook

平台通知入站 Webhook 使用独立的 Webhook Secret，不使用开放 API Key：

```http
POST /api/v1/webhook?apikey=<WEBHOOK_SECRET>
Content-Type: application/json

{"title": "构建完成", "text": "发布成功", "category": "CI"}
```

插件公开 Webhook 地址为 `/api/plugin/{plugin_id}/{path}`，插件应自行验证签名或共享密钥。

## 错误格式

```json
{"detail": "错误说明"}
```

- `400`：参数或配置不合法。
- `401`：API Key 缺失或无效。
- `403`：操作因安全原因禁用。
- `404`：插件、键或会话不存在。
- `409`：插件加载或状态冲突。
- `502`：Telegram 或外部服务调用失败。
- `503`：API Key 未配置、账号离线或服务不可用。

## Python 示例

```python
import os
import requests

base = "http://127.0.0.1:18001/api/v1"
headers = {"X-API-Key": os.environ["AWBOTNEST_API_KEY"]}

response = requests.get(f"{base}/plugins", headers=headers, timeout=10)
response.raise_for_status()
print(response.json())
```

## cURL 示例

```bash
curl -H "X-API-Key: $AWBOTNEST_API_KEY" \
  http://127.0.0.1:18001/api/v1/status

curl -X POST \
  -H "X-API-Key: $AWBOTNEST_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":-1001234567890,"text":"Hello","sender":"bot"}' \
  http://127.0.0.1:18001/api/v1/messages/send
```

## 安全建议

1. 不要把 API Key 写进源码、聊天记录、URL 或公开日志，优先使用环境变量。
2. 公网访问必须配置 HTTPS 和访问控制；不需要公网时只允许可信网络访问。
3. 第三方工具不再使用时立即轮换 API Key。
4. 开放 API 可以启停插件、修改配置和发送消息，应视为管理员级自动化权限。
5. 不要向不可信程序提供读取插件源码或日志的权限。
