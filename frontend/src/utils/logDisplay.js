const SOURCE_LABELS = {
  main: '平台服务', api: '平台接口', scheduler: '定时任务', repo_sync: '插件仓库',
  plugin: '插件管理', plugin_runtime: '插件运行', plugins: '插件管理',
  account: '账号管理', account_manager: '账号管理', telegram: 'Telegram 客户端',
  custom_client: 'Telegram 客户端', notification: '通知服务', notifier: '通知服务',
  notification_channels: '通知渠道', backup: '备份服务', deps: '依赖管理',
  registry: '插件注册', context: '插件上下文', routing: '路由服务', activity: '活动统计',
  'apscheduler.scheduler': '定时任务', 'uvicorn.error': 'Web 服务',
  'uvicorn.access': '访问记录', httpx: '网络请求',
  'telethon.network.mtprotosender': 'Telegram 网络',
  'telethon.client.users': 'Telegram 账号',
  'telethon.client.telegrambaseclient': 'Telegram 客户端',
  'telethon.client.downloads': 'Telegram 下载',
}

const MESSAGE_RULES = [
  [/^Scheduler started$/i, '定时服务已启动'],
  [/^Scheduler has been shut down$/i, '定时服务已停止'],
  [/^Added job .* to job store .*$/i, '定时任务已注册'],
  [/^Removed job .*$/i, '定时任务已移除'],
  [/^Running job /i, '正在执行定时任务：'],
  [/^Job .* executed successfully$/i, '定时任务执行成功'],
  [/^Connecting to ([^…]+)…$/i, '正在连接 $1…'],
  [/^Connection to ([^ ]+) complete!$/i, '已连接到 $1'],
  [/^Disconnecting from ([^…]+)…$/i, '正在断开 $1…'],
  [/^Disconnection from ([^ ]+) complete!$/i, '已断开 $1'],
  [/^HTTP Request: (.+)$/i, '网络请求：$1'],
  [/^Starting direct file download in chunks of (\d+) at (\d+), stride (\d+)$/i, '开始下载 Telegram 文件（分块 $1 字节，起点 $2，步长 $3）'],
  [/^Server closed the connection: (.+)$/i, 'Telegram 服务器关闭了连接：$1'],
  [/^Connection closed while receiving data: (.+)$/i, '接收数据时连接已关闭：$1'],
  [/^Closing current connection to begin reconnect/i, '当前连接已关闭，准备重新连接'],
  [/^Telethon Bot \[([^\]]+)] 已连接$/i, 'Bot「$1」已连接'],
  [/^Telethon 用户账号 (.+) 已连接$/i, '用户账号「$1」已连接'],
  [/^Started server process/i, 'Web 服务进程已启动'],
  [/^Waiting for application startup/i, '正在等待平台启动'],
  [/^Application startup complete/i, '平台启动完成'],
  [/^Shutting down/i, '正在停止 Web 服务'],
  [/^Application shutdown complete/i, '平台已停止'],
]

export function logLevelLabel(level) {
  return String(level || 'INFO').toUpperCase()
}

const NOISY_SOURCES = /^(telethon\.|apscheduler\.|httpx$|httpcore\.|uvicorn\.access$)/i

export function isDisplayLog(item = {}) {
  const level = String(item.level || 'INFO').toUpperCase()
  return !NOISY_SOURCES.test(String(item.source || '')) || ['ERROR', 'CRITICAL'].includes(level)
}

export function logSourceLabel(source, pluginNames = {}) {
  const raw = String(source || '').trim()
  if (!raw) return '平台服务'
  if (pluginNames[raw]) return pluginNames[raw]
  if (SOURCE_LABELS[raw]) return SOURCE_LABELS[raw]
  if (raw.startsWith('awbotnest.plugin.')) return pluginNames[raw.slice(17)] || '插件运行'
  if (raw.startsWith('awbotnest.')) return SOURCE_LABELS[raw.slice(10)] || '平台模块'
  return /^[a-z][a-z0-9_.-]*$/i.test(raw) ? '系统组件' : raw
}

export function logMessageLabel(message) {
  const raw = String(message || '').replace(/\s+/g, ' ').trim()
  if (!raw) return '收到一条运行记录'
  for (const [pattern, replacement] of MESSAGE_RULES) {
    if (pattern.test(raw)) return raw.replace(pattern, replacement)
  }
  return raw
}
