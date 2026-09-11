"""Human-friendly documentation for the stable external API."""

from __future__ import annotations

from copy import deepcopy
from html import escape
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse


PUBLIC_TAGS = [
    {"name": "平台", "description": "检查 AWBotNest 是否可用，以及当前插件和账号的运行概况。"},
    {"name": "Telegram", "description": "发送 Telegram 消息并查询当前账号可访问的会话。"},
    {"name": "插件管理", "description": "查看、启用、停用或重载已安装插件。"},
    {"name": "插件配置", "description": "读取和保存插件配置；保存后，已运行的插件会自动重载。"},
    {"name": "插件数据", "description": "读写插件持久化的 KV 数据。账号实例插件需要传入 `instance`。"},
    {"name": "账号与日志", "description": "查询账号连接状态和最近日志。日志可能包含敏感信息。"},
]

OPERATION_DOCS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("/api/v1/status", "get"): ("平台", "查看平台状态", "用于健康检查或自动化开始前的快速确认，不会修改任何数据。"),
    ("/api/v1/messages/send", "post"): ("Telegram", "发送 Telegram 消息", "使用机器人或用户账号向指定会话发送文字消息。`sender` 省略时使用机器人。"),
    ("/api/v1/chats/{chat_id}", "get"): ("Telegram", "查询 Telegram 会话", "通过已连接的用户账号解析会话 ID 或用户名；机器人账号不能用于此查询。"),
    ("/api/v1/plugins", "get"): ("插件管理", "列出所有插件", "返回插件的启用状态、版本、作用域、依赖和运行错误等信息。"),
    ("/api/v1/plugins/{plugin_id}", "get"): ("插件管理", "查看插件详情", "`plugin_id` 是插件英文标识，可先通过“列出所有插件”获取。"),
    ("/api/v1/plugins/{plugin_id}/source", "get"): ("插件管理", "读取插件源码", "返回插件入口文件的相对路径和源码。源码可能包含敏感业务逻辑，请谨慎保存。"),
    ("/api/v1/plugins/{plugin_id}/source", "put"): ("插件管理", "远程修改源码（已禁用）", "出于安全原因，此端点固定返回 `403`。请通过可信的本地文件或插件安装流程修改源码。"),
    ("/api/v1/plugins/{plugin_id}/enable", "post"): ("插件管理", "启用插件", "加载并启动插件；依赖或配置不满足时返回 `409`。"),
    ("/api/v1/plugins/{plugin_id}/disable", "post"): ("插件管理", "停用插件", "停止插件并取消其后台任务，不会删除插件文件和配置。"),
    ("/api/v1/plugins/{plugin_id}/reload", "post"): ("插件管理", "重载插件", "重新载入插件代码和配置，适合在本地更新插件后调用。"),
    ("/api/v1/plugins/{plugin_id}/config", "get"): ("插件配置", "读取插件配置", "敏感字段只返回掩码 `********`，不会返回真实密钥。"),
    ("/api/v1/plugins/{plugin_id}/config", "put"): ("插件配置", "保存插件配置", "请求体中的 `config` 会按插件声明的规则校验。保留敏感字段时可原样传回 `********`。"),
    ("/api/v1/plugins/{plugin_id}/kv", "get"): ("插件数据", "列出插件 KV 数据", "返回该插件命名空间中的全部键和值。账号实例插件必须传 `instance`。"),
    ("/api/v1/plugins/{plugin_id}/kv/{key}", "get"): ("插件数据", "读取一个 KV 值", "读取指定键；键不存在时返回 `404`。"),
    ("/api/v1/plugins/{plugin_id}/kv/{key}", "put"): ("插件数据", "写入一个 KV 值", "`value` 可以是字符串、数字、布尔值、数组、对象或 `null`。"),
    ("/api/v1/plugins/{plugin_id}/kv/{key}", "delete"): ("插件数据", "删除一个 KV 值", "永久删除指定键；键不存在时返回 `404`。"),
    ("/api/v1/accounts", "get"): ("账号与日志", "列出账号连接状态", "仅返回账号标识、类型和连接状态，不返回 Token、手机号或 Session 内容。"),
    ("/api/v1/logs", "get"): ("账号与日志", "读取平台日志", "返回最近的平台日志，`limit` 范围为 1–1000。"),
    ("/api/v1/logs/plugins/{plugin_id}", "get"): ("账号与日志", "读取插件日志", "只返回指定插件的最近日志，便于第三方监控定位问题。"),
}

PARAMETER_DESCRIPTIONS = {
    "plugin_id": "插件英文标识，例如 `pt_multi_checkin`。可通过 `GET /api/v1/plugins` 获取。",
    "chat_id": "Telegram 会话 ID（如 `-1001234567890`）或可解析的用户名。",
    "session": "用户 Session 名；省略时使用第一个在线用户账号。",
    "instance": "账号实例插件对应的用户 Session 名。shared 插件不要传此参数。",
    "key": "插件 KV 数据中的键名。",
    "limit": "返回条数，允许 1–1000，默认 100。",
}

REQUEST_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("/api/v1/messages/send", "post"): {
        "required": True,
        "content": {"application/json": {
            "schema": {
                "type": "object", "required": ["chat_id", "text"],
                "properties": {
                    "chat_id": {"oneOf": [{"type": "integer"}, {"type": "string"}], "description": "Telegram 会话 ID 或用户名"},
                    "text": {"type": "string", "description": "消息正文"},
                    "sender": {"type": "string", "enum": ["bot", "user"], "default": "bot", "description": "发送账号类型"},
                    "session": {"type": "string", "description": "Bot ID 或用户 Session 名；省略时自动选择"},
                    "parse_mode": {"type": "string", "enum": ["HTML", "Markdown"], "description": "可选的 Telegram 文本解析模式"},
                },
            },
            "example": {"chat_id": -1001234567890, "text": "Hello from AWBotNest", "sender": "bot", "session": "default", "parse_mode": "HTML"},
        }},
    },
    ("/api/v1/plugins/{plugin_id}/config", "put"): {
        "required": True,
        "content": {"application/json": {
            "schema": {"type": "object", "required": ["config"], "properties": {"config": {"type": "object", "additionalProperties": True, "description": "完整的插件配置对象"}}},
            "example": {"config": {"keyword": "hi", "enabled": True}},
        }},
    },
    ("/api/v1/plugins/{plugin_id}/kv/{key}", "put"): {
        "required": True,
        "content": {"application/json": {
            "schema": {"type": "object", "required": ["value"], "properties": {"value": {"description": "要保存的 JSON 值"}}},
            "example": {"value": 42},
        }},
    },
}


def public_openapi_schema(app: FastAPI) -> dict[str, Any]:
    """Return a focused contract containing only the stable external API."""
    schema = deepcopy(app.openapi())
    schema["info"] = {
        "title": "AWBotNest 开放平台 API",
        "version": schema.get("info", {}).get("version", ""),
        "description": (
            "供自动化脚本、AI 助手和第三方系统调用的稳定 REST API。"
            "所有接口都以 `/api/v1` 开头，请先在控制台生成 API Key。"
        ),
    }
    schema["tags"] = PUBLIC_TAGS
    schema["paths"] = {
        path: deepcopy(item)
        for path, item in schema.get("paths", {}).items()
        if path.startswith("/api/v1/")
    }
    components = schema.setdefault("components", {})
    components["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "在“系统设置 → 开放接口”生成的 API Key，无需添加 Bearer 前缀。",
        },
    }
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation["security"] = [{"ApiKeyAuth": []}]
            operation["parameters"] = [
                parameter for parameter in operation.get("parameters", [])
                if parameter.get("name", "").lower() not in {"x-api-key", "api-key"}
            ]
            for parameter in operation["parameters"]:
                description = PARAMETER_DESCRIPTIONS.get(parameter.get("name", ""))
                if description:
                    parameter["description"] = description
            docs = OPERATION_DOCS.get((path, method.lower()))
            if docs:
                tag, summary, description = docs
                operation["tags"] = [tag]
                operation["summary"] = summary
                operation["description"] = description
            body = REQUEST_BODIES.get((path, method.lower()))
            if body:
                operation["requestBody"] = body
            responses = operation.setdefault("responses", {})
            if "200" in responses:
                responses["200"]["description"] = "请求成功"
            if "422" in responses:
                responses["422"]["description"] = "请求参数校验失败"
            responses.setdefault("401", {"description": "API Key 缺失或无效"})
            responses.setdefault("503", {"description": "API Key 尚未配置，或依赖的账号/服务不可用"})
    return schema


def _docs_html(version: str) -> str:
    page = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#07101b">
  <title>AWBotNest 开放平台 API</title>
  <link rel="icon" href="/favicon.ico">
  <link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    :root { color-scheme: dark; --base:#07101b; --surface:#0d1623; --raised:#172131; --line:#26384d; --text:#eef1f7; --muted:#aab4c6; --blue:#4a90ff; --green:#22c99a; }
    * { box-sizing:border-box; }
    html { scroll-behavior:smooth; background:var(--base); }
    body { margin:0; min-width:320px; background:radial-gradient(900px 520px at 14% -14%, rgba(48,128,240,.16), transparent 68%), var(--base); color:var(--text); font-family:"Aptos","Microsoft YaHei UI","PingFang SC",sans-serif; -webkit-font-smoothing:antialiased; }
    ::selection { background:rgba(74,144,255,.36); color:#fff; }
    :focus-visible { outline:2px solid var(--blue); outline-offset:3px; }
    ::-webkit-scrollbar { width:9px; height:9px; } ::-webkit-scrollbar-track { background:transparent; } ::-webkit-scrollbar-thumb { background:#2a3c52; border:2px solid var(--base); border-radius:8px; }
    .docs-nav { height:64px; display:flex; align-items:center; justify-content:space-between; padding:0 max(24px, env(safe-area-inset-right)) 0 max(24px, env(safe-area-inset-left)); border-bottom:1px solid rgba(117,151,190,.18); background:rgba(7,16,27,.9); backdrop-filter:blur(18px); position:sticky; top:0; z-index:20; }
    .brand { display:flex; align-items:center; gap:12px; color:var(--text); text-decoration:none; font-weight:760; }
    .brand-mark { width:40px; height:40px; display:block; flex:0 0 40px; object-fit:contain; border-radius:10px; box-shadow:0 8px 22px rgba(40,121,232,.2); }
    .brand small { color:var(--muted); font-size:12px; font-weight:520; }
    .nav-actions { display:flex; align-items:center; gap:10px; }
    .version { color:#91a1b8; font-size:12px; font-variant-numeric:tabular-nums; }
    .back-link { min-height:38px; display:inline-flex; align-items:center; padding:0 14px; border:1px solid var(--line); border-radius:9px; color:#dbe6f5; text-decoration:none; font-size:13px; font-weight:650; transition:background .18s ease,border-color .18s ease,transform .18s ease; }
    .back-link:hover { background:var(--raised); border-color:#416083; transform:translateY(-1px); }
    .docs-intro { max-width:1240px; margin:0 auto; padding:68px 28px 34px; }
    .docs-intro h1 { max-width:760px; margin:0 0 16px; color:#fff; font-size:clamp(34px,5vw,58px); line-height:1.08; letter-spacing:-.035em; }
    .lead { max-width:720px; margin:0 0 38px; color:#b9c5d7; font-size:17px; line-height:1.75; }
    .quickstart { display:grid; grid-template-columns:minmax(0,1.4fr) minmax(310px,.8fr); gap:20px; align-items:stretch; }
    .start-panel, .verify-panel { border:1px solid rgba(98,132,171,.28); border-radius:15px; background:rgba(13,22,35,.84); box-shadow:0 18px 48px rgba(0,0,0,.2); }
    .start-panel { padding:26px 28px; }
    .start-panel h2, .verify-panel h2, .reference-head h2 { margin:0; color:#f5f8fc; font-size:20px; letter-spacing:-.015em; }
    .steps { display:grid; gap:20px; margin-top:24px; }
    .step { display:grid; grid-template-columns:30px minmax(0,1fr); gap:14px; align-items:start; }
    .step-number { width:28px; height:28px; display:grid; place-items:center; border-radius:9px; background:rgba(48,128,240,.17); color:#78adff; font-weight:760; font-size:12px; font-variant-numeric:tabular-nums; }
    .step strong { display:block; margin-bottom:4px; color:#edf4ff; font-size:14px; }
    .step p { margin:0; color:var(--muted); font-size:13px; line-height:1.65; }
    .step code, .base-url { color:#8fc0ff; font-family:"SFMono-Regular",Consolas,monospace; }
    .verify-panel { overflow:hidden; background:#09121e; }
    .verify-head { display:flex; align-items:center; justify-content:space-between; padding:20px 20px 14px; }
    .copy-button { min-height:34px; padding:0 11px; border:1px solid var(--line); border-radius:8px; background:var(--raised); color:#dbe6f5; font:600 12px inherit; cursor:pointer; }
    .copy-button:hover { border-color:#4a6c91; background:#1c2a3d; }
    .verify-panel pre { margin:0; padding:18px 20px 22px; border-top:1px solid rgba(98,132,171,.2); overflow:auto; color:#cfe0f7; font:13px/1.75 "SFMono-Regular",Consolas,monospace; }
    .verify-panel .comment { color:#71839a; }
    .security-note { display:flex; gap:10px; align-items:flex-start; margin-top:20px; color:#b8c4d6; font-size:12px; line-height:1.6; }
    .security-note svg { width:17px; height:17px; flex:0 0 17px; margin-top:1px; color:#e9b44c; }
    .reference { max-width:1300px; margin:0 auto; padding:38px 18px 80px; }
    .reference-head { padding:0 10px 18px; }
    .reference-head p { max-width:720px; margin:8px 0 0; color:var(--muted); line-height:1.65; }
    #swagger-ui { min-height:420px; }
    .swagger-ui { color:var(--text); font-family:inherit; }
    .swagger-ui .topbar, .swagger-ui .information-container, .swagger-ui .servers, .swagger-ui .models { display:none; }
    .swagger-ui .wrapper { max-width:none; padding:0 10px; }
    .swagger-ui .scheme-container { margin:0 0 22px; padding:18px 20px; border:1px solid var(--line); border-radius:14px; background:var(--surface); box-shadow:0 12px 34px rgba(0,0,0,.16); }
    .swagger-ui .btn.authorize { border-color:#397fde; color:#83b6ff; background:rgba(48,128,240,.1); }
    .swagger-ui .btn.authorize svg { fill:#83b6ff; }
    .swagger-ui .opblock-tag { margin:16px 0 8px; padding:17px 14px; border-bottom:1px solid var(--line); color:#edf4ff; font-family:inherit; }
    .swagger-ui .opblock-tag small { color:var(--muted); font-family:inherit; }
    .swagger-ui .opblock { margin:0 0 10px; border-radius:12px; background:var(--surface); box-shadow:none; overflow:hidden; }
    .swagger-ui .opblock .opblock-summary { min-height:58px; padding:8px 12px; }
    .swagger-ui .opblock .opblock-summary-method { min-width:72px; border-radius:7px; font-family:"SFMono-Regular",Consolas,monospace; text-shadow:none; }
    .swagger-ui .opblock .opblock-summary-path, .swagger-ui .opblock .opblock-summary-description, .swagger-ui .opblock-description-wrapper p, .swagger-ui .parameter__name, .swagger-ui table thead tr td, .swagger-ui table thead tr th, .swagger-ui .response-col_status { color:#e8eef7; font-family:inherit; }
    .swagger-ui .opblock .opblock-summary-path { font-family:"SFMono-Regular",Consolas,monospace; font-size:14px; }
    .swagger-ui .opblock .opblock-summary-description { color:#c2ccda; font-size:13px; }
    .swagger-ui .opblock-body, .swagger-ui .responses-inner { color:var(--text); }
    .swagger-ui .opblock-section-header { background:#121d2b; box-shadow:none; }
    .swagger-ui .opblock-section-header h4, .swagger-ui .opblock-section-header label { color:#e9f0f9; font-family:inherit; }
    .swagger-ui .parameter__type, .swagger-ui .parameter__in, .swagger-ui .response-col_description, .swagger-ui .markdown p, .swagger-ui .renderedMarkdown p { color:var(--muted); }
    .swagger-ui input[type=text], .swagger-ui textarea, .swagger-ui select { border:1px solid var(--line); border-radius:8px; background:#09121e; color:#edf4ff; font-family:inherit; }
    .swagger-ui .highlight-code > .microlight, .swagger-ui .model-example { background:#08111c !important; border-radius:9px; color:#d8e6f7 !important; }
    .swagger-ui .tab li button.tablinks, .swagger-ui .model-title, .swagger-ui .model { color:#dce7f5; font-family:inherit; }
    .swagger-ui .btn { border-radius:8px; color:#dce7f5; font-family:inherit; }
    .swagger-ui .loading-container .loading:after { color:var(--muted); }
    @media (max-width:760px) {
      .docs-nav { height:58px; padding-left:max(14px,env(safe-area-inset-left)); padding-right:max(14px,env(safe-area-inset-right)); }
      .brand small, .version { display:none; }
      .docs-intro { padding:42px 16px 24px; }
      .docs-intro h1 { font-size:36px; }
      .lead { font-size:15px; margin-bottom:28px; }
      .quickstart { grid-template-columns:1fr; }
      .start-panel { padding:22px 18px; }
      .reference { padding:28px 6px calc(54px + env(safe-area-inset-bottom)); }
      .reference-head { padding-left:12px; padding-right:12px; }
      .swagger-ui .wrapper { padding:0 4px; }
      .swagger-ui .scheme-container { margin-left:4px; margin-right:4px; padding:14px; }
      .swagger-ui .filter-container { width:100%; margin:0; padding:0 4px; }
      .swagger-ui .filter-container .operation-filter-input { width:100%; max-width:none; }
      .swagger-ui .opblock-tag { align-items:flex-start; flex-wrap:wrap; row-gap:5px; }
      .swagger-ui .opblock-tag small { flex:1 0 100%; margin:0; line-height:1.55; }
      .swagger-ui .opblock .opblock-summary { align-items:flex-start; flex-wrap:wrap; }
      .swagger-ui .opblock .opblock-summary-path { max-width:calc(100vw - 122px); overflow-wrap:anywhere; }
      .swagger-ui .opblock .opblock-summary-description { width:100%; padding:2px 0 4px 82px; }
    }
  </style>
</head>
<body>
  <nav class="docs-nav" aria-label="文档导航">
    <a class="brand" href="/"><img class="brand-mark" src="/pwa-192.png" alt="AWBotNest Logo"><span>AWBotNest <small>开放平台</small></span></a>
    <div class="nav-actions"><span class="version">v__VERSION__</span><a class="back-link" href="/">返回控制台</a></div>
  </nav>
  <main>
    <section class="docs-intro">
      <h1>用 API 连接 AWBotNest</h1>
      <p class="lead">通过稳定的 REST API 管理插件、读写插件数据和发送 Telegram 消息。下面三步即可完成第一次调用。</p>
      <div class="quickstart">
        <div class="start-panel">
          <h2>开始调用</h2>
          <div class="steps">
            <div class="step"><span class="step-number">1</span><div><strong>生成 API Key</strong><p>前往“系统设置 → 开放接口”，生成密钥并保存设置。密钥只应交给可信程序。</p></div></div>
            <div class="step"><span class="step-number">2</span><div><strong>完成文档授权</strong><p>点击接口列表上方的“授权”，直接粘贴 API Key，不要添加 <code>Bearer</code> 前缀。</p></div></div>
            <div class="step"><span class="step-number">3</span><div><strong>发送测试请求</strong><p>展开“查看平台状态”，点击“试用接口 → 执行”。收到 <code>200</code> 即表示接入成功。</p></div></div>
          </div>
        </div>
        <div class="verify-panel">
          <div class="verify-head"><h2>cURL 验证</h2><button class="copy-button" type="button" data-copy>复制命令</button></div>
          <pre><span class="comment"># 将 YOUR_API_KEY 替换为真实密钥</span>
curl -H "X-API-Key: YOUR_API_KEY" \
  <span data-origin></span>/api/v1/status</pre>
        </div>
      </div>
      <div class="security-note">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/></svg>
        <span>API Key 具有管理员级自动化权限。公网调用请使用 HTTPS，且不要把密钥写入源码、URL、聊天记录或公开日志。</span>
      </div>
    </section>
    <section class="reference" aria-labelledby="reference-title">
      <div class="reference-head"><h2 id="reference-title">接口参考</h2><p>接口已按使用场景分组。授权一次后，可以直接在每个接口内填写参数并发送请求。</p></div>
      <div id="swagger-ui"></div>
    </section>
  </main>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    document.querySelector('[data-origin]').textContent = location.origin;
    document.querySelector('[data-copy]').addEventListener('click', async (event) => {
      const command = `curl -H "X-API-Key: YOUR_API_KEY" ${location.origin}/api/v1/status`;
      try { await navigator.clipboard.writeText(command); event.currentTarget.textContent = '已复制'; }
      catch { event.currentTarget.textContent = '请手动复制'; }
      window.setTimeout(() => { event.currentTarget.textContent = '复制命令'; }, 1600);
    });
    const swaggerTranslations = new Map([
      ['Authorize', '授权'], ['Available authorizations', '可用的授权方式'],
      ['Close', '关闭'], ['Logout', '退出授权'], ['Value:', '密钥：'],
      ['Name:', '请求头名称：'], ['In:', '传递位置：'],
      ['Filter by tag', '按分组筛选'], ['Parameters', '请求参数'],
      ['No parameters', '无需参数'], ['Request body', '请求体'],
      ['Required', '必填'], ['Responses', '响应'], ['Response samples', '响应示例'],
      ['Example Value', '示例值'], ['Schema', '数据结构'], ['Model', '数据模型'],
      ['Try it out', '试用接口'], ['Execute', '执行'], ['Clear', '清空'],
      ['Cancel', '取消'], ['Download', '下载'], ['Request URL', '请求地址'],
      ['Server response', '服务器响应'], ['Response body', '响应正文'],
      ['Response headers', '响应头'], ['Code', '状态码'], ['Details', '详情'],
      ['Description', '说明'], ['Links', '链接'], ['Media type', '媒体类型'],
      ['Controls Accept header.', '设置 Accept 请求头。'],
      ['No links', '无链接'], ['header', '请求头'], ['apiKey', 'API Key'],
      ['Example', '示例'], ['Request duration', '请求耗时'],
      ['Successful Response', '请求成功'], ['Validation Error', '请求参数校验失败'],
      ['Loading', '正在加载'], ['Network Error', '网络错误'],
      ['Failed to fetch', '请求失败'], ['Fetch error', '请求错误'],
      ['No operations defined in spec!', '文档中没有可用接口。'],
      ['Copy to clipboard', '复制到剪贴板'], ['Copied', '已复制'],
      ['Expand operation', '展开接口'], ['Collapse operation', '收起接口'],
      ['authorization button unlocked', '尚未授权'],
      ['authorization button locked', '已授权'], ['Jump to', '跳转到'],
    ]);
    function translateSwagger(root = document.getElementById('swagger-ui')) {
      if (!root) return;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const value = node.nodeValue;
        const trimmed = value.trim();
        const translated = swaggerTranslations.get(trimmed);
        if (translated) node.nodeValue = value.replace(trimmed, translated);
      }
      root.querySelectorAll('.auth-container h4').forEach((element) => {
        if (element.textContent.replace(/\s+/g, '') === 'ApiKeyAuth(apiKey)') {
          element.innerHTML = '<code>API Key</code>&nbsp;（请求头）';
        }
      });
      root.querySelectorAll('.response-control-media-type__accept-message').forEach((element) => {
        if (element.textContent.trim() === 'Controls Accept header.') {
          element.innerHTML = '设置 <code>Accept</code> 请求头。';
        }
      });
      root.querySelectorAll('[placeholder],[title],[aria-label]').forEach((element) => {
        for (const attribute of ['placeholder', 'title', 'aria-label']) {
          const value = element.getAttribute(attribute);
          const translated = value && swaggerTranslations.get(value.trim());
          if (translated) element.setAttribute(attribute, translated);
        }
      });
    }
    const swaggerRoot = document.getElementById('swagger-ui');
    const swaggerObserverOptions = { childList:true, characterData:true, subtree:true };
    let translationScheduled = false;
    const swaggerObserver = new MutationObserver(() => {
      if (translationScheduled) return;
      translationScheduled = true;
      requestAnimationFrame(() => {
        swaggerObserver.disconnect();
        translateSwagger(swaggerRoot);
        swaggerObserver.observe(swaggerRoot, swaggerObserverOptions);
        translationScheduled = false;
      });
    });
    swaggerObserver.observe(swaggerRoot, swaggerObserverOptions);
    window.ui = SwaggerUIBundle({
      url: '/openapi-public.json', dom_id: '#swagger-ui', deepLinking: true,
      displayRequestDuration: true, filter: true, persistAuthorization: true,
      docExpansion: 'list', defaultModelsExpandDepth: -1, tryItOutEnabled: false,
      syntaxHighlight: { activate: true, theme: 'obsidian' },
      presets: [SwaggerUIBundle.presets.apis], layout: 'BaseLayout'
    });
    requestAnimationFrame(() => translateSwagger(swaggerRoot));
  </script>
</body>
</html>'''
    return page.replace("__VERSION__", escape(version))


def register_api_docs(app: FastAPI, version: str) -> None:
    """Register the public OpenAPI contract and its dedicated documentation page."""
    @app.get("/openapi-public.json", include_in_schema=False)
    async def public_openapi() -> dict[str, Any]:
        return public_openapi_schema(app)

    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    async def api_docs() -> HTMLResponse:
        return HTMLResponse(_docs_html(version))
