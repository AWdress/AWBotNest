# AWBotNest 开放平台 API 文档

AWBotNest 提供了一套完整的 RESTful API，让第三方工具、AI 助手、自动化脚本可以远程管理平台和插件。

## 认证方式

所有 API 请求需要在请求头中携带 API Key：

```
X-API-Key: your_api_key_here
```

或使用备选头名：

```
Api-Key: your_api_key_here
```

### 获取 API Key

1. 登录 AWBotNest Web 控制台
2. 进入「系统设置」
3. 在「通知」区域找到「API_KEY」字段
4. 点击「随机」按钮生成新密钥
5. 保存设置

**注意**：API Key 是敏感信息，请妥善保管。泄露后请立即重新生成。

## 基础信息

- **基础路径**：`/api/v1`
- **请求格式**：JSON
- **响应格式**：JSON
- **字符编码**：UTF-8

## 接口列表

### 1. 插件管理

#### 1.1 列出所有插件

```http
GET /api/v1/plugins
```

**响应示例**：
```json
{
  "plugins": [
    {
      "id": "example_plugin",
      "name": "示例插件",
      "version": "1.0.0",
      "author": "作者名",
      "description": "插件描述",
      "scope": "user",
      "enabled": true,
      "loaded": true,
      "has_config": true,
      "webhook": false
    }
  ]
}
```

#### 1.2 获取插件详情

```http
GET /api/v1/plugins/{plugin_id}
```

**响应示例**：
```json
{
  "id": "example_plugin",
  "name": "示例插件",
  "version": "1.0.0",
  "author": "作者名",
  "description": "插件描述",
  "changelog": "v1.0.0 初始版本",
  "scope": "user",
  "default_enabled": false,
  "enabled": true,
  "loaded": true,
  "config_schema": {...},
  "webhook": false,
  "render_mode": "schema"
}
```

#### 1.3 读取插件源代码

```http
GET /api/v1/plugins/{plugin_id}/source
```

**响应示例**：
```json
{
  "plugin_id": "example_plugin",
  "path": "plugins/example_plugin.py",
  "source": "# 插件源代码...",
  "is_package": false
}
```

#### 1.4 更新插件源代码（已禁用）

```http
PUT /api/v1/plugins/{plugin_id}/source
```

**⚠️ 安全提示**：出于安全考虑，此接口已在 v1.1.2.2 版本中禁用。修改插件源代码相当于远程执行代码，存在极高的安全风险。

如需修改插件代码，请通过以下方式：
- 使用 Web 控制台的插件编辑器
- 直接编辑服务器上的插件文件
- 通过 SSH/FTP 等安全方式上传新版本

**错误响应**：
```json
{
  "detail": "此 API 端点已因安全原因被禁用"
}
```

#### 1.5 启用插件

```http
POST /api/v1/plugins/{plugin_id}/enable
```

**响应示例**：
```json
{
  "ok": true,
  "message": "插件已启用"
}
```

#### 1.6 停用插件

```http
POST /api/v1/plugins/{plugin_id}/disable
```

**响应示例**：
```json
{
  "ok": true,
  "message": "插件已停用"
}
```

#### 1.7 重载插件

```http
POST /api/v1/plugins/{plugin_id}/reload
```

**响应示例**：
```json
{
  "ok": true,
  "message": "插件已重载"
}
```

### 2. 插件配置

#### 2.1 读取插件配置

```http
GET /api/v1/plugins/{plugin_id}/config
```

**响应示例**：
```json
{
  "plugin_id": "example_plugin",
  "config": {
    "keyword": "hello",
    "enabled": true
  }
}
```

#### 2.2 修改插件配置

```http
PUT /api/v1/plugins/{plugin_id}/config
```

**请求体**：
```json
{
  "config": {
    "keyword": "hi",
    "enabled": false
  }
}
```

**响应示例**：
```json
{
  "ok": true,
  "message": "配置已更新并重载",
  "reloaded": true
}
```

### 3. 插件数据存储（KV）

#### 3.1 列出所有键

```http
GET /api/v1/plugins/{plugin_id}/kv
```

**响应示例**：
```json
{
  "plugin_id": "example_plugin",
  "keys": ["count", "last_run", "users"]
}
```

#### 3.2 读取某个键

```http
GET /api/v1/plugins/{plugin_id}/kv/{key}
```

**响应示例**：
```json
{
  "plugin_id": "example_plugin",
  "key": "count",
  "value": 42
}
```

#### 3.3 设置键值

```http
PUT /api/v1/plugins/{plugin_id}/kv/{key}
```

**请求体**：
```json
{
  "value": 100
}
```

**响应示例**：
```json
{
  "ok": true,
  "message": "键值已设置"
}
```

#### 3.4 删除键

```http
DELETE /api/v1/plugins/{plugin_id}/kv/{key}
```

**响应示例**：
```json
{
  "ok": true,
  "message": "键已删除"
}
```

### 4. 消息发送

#### 4.1 发送消息

```http
POST /api/v1/messages/send
```

**请求体**：
```json
{
  "chat_id": -1001234567890,
  "text": "Hello World",
  "sender": "bot",
  "parse_mode": "HTML"
}
```

**参数说明**：
- `chat_id`：目标会话 ID（必填）
- `text`：消息文本（必填）
- `sender`：发送者类型，`"bot"` 或 `"user"`（可选，默认 `"bot"`）
- `parse_mode`：解析模式，`"HTML"` 或 `"Markdown"`（可选）

**响应示例**：
```json
{
  "ok": true,
  "message_id": 12345,
  "chat_id": -1001234567890,
  "date": "2026-07-24T10:30:00"
}
```

### 5. 会话信息

#### 5.1 获取会话信息

```http
GET /api/v1/chats/{chat_id}?session=account_name
```

**查询参数**：
- `session`：指定用哪个账号查询（可选，不填则使用首个已连接的用户账号）

**响应示例**：
```json
{
  "id": -1001234567890,
  "title": "示例群组",
  "type": "supergroup"
}
```

### 6. 账号管理

#### 6.1 列出所有账号

```http
GET /api/v1/accounts
```

**响应示例**：
```json
{
  "accounts": [
    {
      "type": "bot",
      "session": "default",
      "name": "Bot",
      "connected": true
    },
    {
      "type": "user",
      "session": "my_account",
      "name": "my_account",
      "connected": true
    }
  ]
}
```

### 7. 日志查询

#### 7.1 获取平台日志

```http
GET /api/v1/logs?limit=100
```

**查询参数**：
- `limit`：返回最近 N 条日志（可选，默认 100）

**响应示例**：
```json
{
  "logs": [
    {
      "level": "INFO",
      "message": "插件已启用: example_plugin",
      "timestamp": "2026-07-24T10:30:00"
    }
  ]
}
```

#### 7.2 获取插件日志

```http
GET /api/v1/logs/plugins/{plugin_id}?limit=100
```

**查询参数**：
- `limit`：返回最近 N 条日志（可选，默认 100）

**响应示例**：
```json
{
  "plugin_id": "example_plugin",
  "logs": [
    {
      "level": "INFO",
      "message": "[example_plugin] 处理了一条消息",
      "timestamp": "2026-07-24T10:30:00"
    }
  ]
}
```

### 8. 平台状态

#### 8.1 获取平台状态

```http
GET /api/v1/status
```

**响应示例**：
```json
{
  "version": "1.1.2.1",
  "bot_connected": true,
  "user_accounts_count": 1,
  "total_plugins": 10,
  "enabled_plugins": 5,
  "enabled_plugin_ids": ["plugin1", "plugin2", "plugin3", "plugin4", "plugin5"]
}
```

## 错误响应

所有接口在出错时返回标准错误格式：

```json
{
  "detail": "错误描述信息"
}
```

常见 HTTP 状态码：

- `400`：请求参数错误
- `401`：API Key 无效或缺失
- `404`：资源不存在（插件/键/文件等）
- `500`：服务器内部错误
- `503`：服务不可用（如 API Key 未配置、插件未加载等）

## 使用示例

### Python 示例

```python
import requests

API_BASE = "http://localhost:18001/api/v1"
API_KEY = "your_api_key_here"

headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

# 列出所有插件
response = requests.get(f"{API_BASE}/plugins", headers=headers)
plugins = response.json()["plugins"]
print(f"找到 {len(plugins)} 个插件")

# 发送消息
data = {
    "chat_id": -1001234567890,
    "text": "Hello from API!",
    "sender": "bot"
}
response = requests.post(f"{API_BASE}/messages/send", json=data, headers=headers)
print(response.json())
```

### cURL 示例

```bash
# 列出所有插件
curl -H "X-API-Key: your_api_key_here" \
  http://localhost:18001/api/v1/plugins

# 启用插件
curl -X POST \
  -H "X-API-Key: your_api_key_here" \
  http://localhost:18001/api/v1/plugins/example_plugin/enable

# 发送消息
curl -X POST \
  -H "X-API-Key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": -1001234567890, "text": "Hello", "sender": "bot"}' \
  http://localhost:18001/api/v1/messages/send
```

## 安全建议

1. **API Key 保护**：不要在代码中硬编码 API Key，使用环境变量或配置文件
2. **HTTPS**：生产环境建议使用 HTTPS 加密传输
3. **网络隔离**：如果不需要公网访问，建议只监听 localhost
4. **定期轮换**：定期重新生成 API Key
5. **访问控制**：如果需要多个第三方工具接入，考虑为每个工具分配不同的密钥（当前版本不支持，需自行实现）
6. **已禁用的危险接口**：以下接口因安全原因已被禁用（返回 403 错误）：
   - `PUT /api/v1/plugins/{id}/source` - 修改插件源代码（等同于远程代码执行）
   - 这些操作只能通过 Web 控制台或直接访问服务器完成

## 适用场景

- **AI 辅助开发**：Hermes、OpenClaw、Claude Code 等 AI 工具调用 API 帮助开发和调试插件
- **自动化运维**：编写脚本批量管理插件、定时收集日志、监控平台状态
- **CI/CD 集成**：在持续集成流程中自动部署插件、运行测试
- **第三方集成**：将 AWBotNest 与其他系统集成，实现消息互通、数据同步等

## 更新日志

### v1.1.2.2 (2026-07-24)

**修复问题：**
- 修复插件列表接口偶尔返回错误的问题
- 修复查看插件源码时路径解析错误
- 修复消息发送接口超时问题，添加 10 秒超时和清晰的错误提示
- 改进错误处理，返回用户友好的错误信息（如"群组 ID 不存在"）

**安全加固：**
- 禁用 `PUT /api/v1/plugins/{id}/source` 接口（修改插件源码）
- 所有 19 个 v1 API 接口测试通过

### v1.1.2.1 (2026-07-24)

- 优化插件开发文档，新增字段类型速查表和推荐排版规范

### v1.1.2.0 (2026-07-24)

- 首次发布开放平台 API
- 支持插件管理、配置修改、数据读写
- 支持消息发送、会话查询、日志获取
- 支持平台状态监控
