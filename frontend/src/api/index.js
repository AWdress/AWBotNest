// API 客户端：统一封装对后端的请求
// 鉴权：密码登录后拿到令牌，存 localStorage，请求带 Authorization: Bearer。

const TOKEN_KEY = 'awbotnest_token'
export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) {
  if (t !== getToken()) clearStatusCache()
  t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY)
}

function authHeaders() {
  const headers = { 'Content-Type': 'application/json' }
  const t = getToken()
  if (t) headers['Authorization'] = `Bearer ${t}`
  return headers
}

// 401 时触发的回调（由 App 注册，跳登录页）
let onUnauthorized = null
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

async function request(method, url, body) {
  const opts = { method, headers: authHeaders() }
  if (body !== undefined) opts.body = JSON.stringify(body)
  const res = await fetch(url, opts)
  if (res.status === 401) {
    setToken('')
    if (onUnauthorized) onUnauthorized()
    throw new Error('未登录或登录已过期')
  }
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch {}
    throw new Error(detail)
  }
  return res.json()
}

async function requestBlob(url, signal) {
  const res = await fetch(url, { headers: authHeaders(), signal })
  if (res.status === 401) {
    setToken('')
    if (onUnauthorized) onUnauthorized()
    throw new Error('未登录或登录已过期')
  }
  if (!res.ok) throw new Error(res.statusText)
  return res.blob()
}

let statusRequest = null
let statusCache = null
let statusCacheAt = 0
let statusGeneration = 0

function clearStatusCache() {
  statusGeneration += 1
  statusRequest = null
  statusCache = null
  statusCacheAt = 0
}

async function getStatus(force = false) {
  const now = Date.now()
  if (!force && statusCache && now - statusCacheAt < 1000) return statusCache
  if (statusRequest) return statusRequest
  const generation = statusGeneration
  statusRequest = request('GET', '/api/status')
    .then((data) => {
      if (generation === statusGeneration) {
        statusCache = data
        statusCacheAt = Date.now()
      }
      return data
    })
    .finally(() => { if (generation === statusGeneration) statusRequest = null })
  return statusRequest
}

export const api = {
  // 鉴权
  authStatus: () => request('GET', '/api/auth/status'),
  authLogin: (username, password) => request('POST', '/api/auth/login', { username, password }),
  authSetup: (username, password) => request('POST', '/api/auth/setup', { username, password }),
  // 恢复登录态（localStorage 令牌）后补种资源 Cookie，确保能加载 vue 模式插件前端
  ensureResourceToken: () => request('POST', '/api/auth/resource_token'),
  changeCredentials: async (old_password, new_username, new_password) => {
    const result = await request('POST', '/api/auth/change_credentials', { old_password, new_username, new_password })
    if (result.token) setToken(result.token)
    return result
  },

  // 插件
  listPlugins: () => request('GET', '/api/plugins'),
  savePluginOrder: (order) => request('PUT', '/api/plugins/order', { order }),
  listAiPlugins: () => request('GET', '/api/ai/plugins'),
  enablePlugin: (id) => request('POST', `/api/plugins/${id}/enable`),
  disablePlugin: (id) => request('POST', `/api/plugins/${id}/disable`),
  reloadPlugin: (id) => request('POST', `/api/plugins/${id}/reload`),
  selfCheckPlugin: (id) => request('POST', `/api/plugins/${id}/self-check`),
  pluginDependencies: () => request('GET', '/api/plugins/dependencies'),
  pluginRuntime: (id) => request('GET', `/api/plugins/${id}/runtime`),
  replayPluginEvent: (id, eventId) => request('POST', `/api/plugins/${id}/events/${eventId}/replay`),
  deletePlugin: (id) => request('DELETE', `/api/plugins/${id}`),
  getPluginConfig: (id) => request('GET', `/api/plugins/${id}/config`),
  setPluginConfig: (id, values) => request('PUT', `/api/plugins/${id}/config`, { values }),
  getPluginAccounts: (id) => request('GET', `/api/plugins/${id}/accounts`),
  setPluginAccounts: (id, sessions) => request('PUT', `/api/plugins/${id}/accounts`, { sessions }),
  getPluginWebhook: (id) => request('GET', `/api/plugins/${id}/webhook`),
  listPluginChats: (id, session = '') =>
    request('GET', `/api/plugins/${id}/dialogs${session ? `?session=${encodeURIComponent(session)}` : ''}`),
  invokePluginAction: (id, action) => request('POST', `/api/plugins/${id}/actions/${encodeURIComponent(action)}`, { payload: {} }),
  // vue 模式插件：调用其 ctx.on_api 注册的端点（管理员令牌鉴权）
  callPluginApi: (id, path, method = 'GET', body) =>
    request(method, `/api/plugin/${id}/${String(path).replace(/^\/+/, '')}`, body),

  // GitHub 导入
  githubList: (source) => request('POST', '/api/plugins/github/list', { source }),
  githubImport: (plugins) => request('POST', '/api/plugins/github/import', { plugins }),

  // 插件商店（多仓库聚合）
  pluginStore: (refresh = true) => request('GET', `/api/plugins/store?refresh=${refresh}`),
  storeDownload: async (plugins) => {
    const installed = []
    const errors = []
    for (const plugin of plugins) {
      try {
        await request('POST', '/api/plugins/store/install', { plugin })
        installed.push(plugin.id)
      } catch (error) {
        errors.push(`${plugin.id}: ${error.message}`)
      }
    }
    return { result: { installed, reloaded: [], restored: [], reload_errors: [], errors, install_counts: {} } }
  },
  repoStatus: () => request('GET', '/api/plugins/repo/status'),

  // 上传（multipart）
  uploadPlugin: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const headers = authHeaders()
    delete headers['Content-Type'] // 让浏览器自动设置 multipart 边界
    const res = await fetch('/api/plugins/upload', { method: 'POST', headers, body: form })
    if (res.status === 401) {
      setToken('')
      if (onUnauthorized) onUnauthorized()
      throw new Error('未登录或登录已过期')
    }
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json()).detail || detail } catch {}
      throw new Error(detail)
    }
    return res.json()
  },

  // 系统状态
  // 页面外壳和状态页可能同时读取状态，短时间内共用同一个请求。
  status: getStatus,

  // 运行日志
  recentLogs: () => request('GET', '/api/logs/recent'),

  // 顶部控制中心
  getUiProfile: () => request('GET', '/api/ui/profile'),
  getAbout: () => request('GET', '/api/ui/about'),
  getAboutVersion: (version) => request('GET', `/api/ui/about/versions/${encodeURIComponent(version)}`),
  uploadAvatar: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const headers = authHeaders()
    delete headers['Content-Type']
    const res = await fetch('/api/ui/avatar', { method: 'POST', headers, body: form })
    if (res.status === 401) {
      setToken('')
      if (onUnauthorized) onUnauthorized()
      throw new Error('未登录或登录已过期')
    }
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json()).detail || detail } catch {}
      throw new Error(detail)
    }
    return res.json()
  },
  getNotifications: () => request('GET', '/api/ui/notifications'),
  readNotifications: () => request('POST', '/api/ui/notifications/read'),
  clearNotifications: () => request('DELETE', '/api/ui/notifications'),
  getHealth: () => request('GET', '/api/ui/health'),
  getNetworkTargets: () => request('GET', '/api/ui/network-targets'),
  testNetworkTarget: (id) => request('POST', '/api/ui/network-test', { id }),
  runSchedulerJob: (id) => request('POST', `/api/ui/scheduler/${encodeURIComponent(id)}/run`),

  // 账号
  listAccounts: () => request('GET', '/api/accounts'),
  accountOnline: (s) => request('POST', `/api/accounts/${s}/online`),
  accountOffline: (s) => request('POST', `/api/accounts/${s}/offline`),
  deleteAccount: (s) => request('DELETE', `/api/accounts/${s}`),
  loginSendCode: (session, phone) => request('POST', '/api/accounts/login/send_code', { session, phone }),
  loginSubmitCode: (session, code) => request('POST', '/api/accounts/login/submit_code', { session, code }),
  loginSubmitPassword: (session, password) => request('POST', '/api/accounts/login/submit_password', { session, password }),
  accountAvatar: (session, version = '', signal) => requestBlob(
    `/api/accounts/${encodeURIComponent(session)}/avatar${version ? `?v=${encodeURIComponent(version)}` : ''}`,
    signal,
  ),

  // 系统设置（config.json）
  getSettings: () => request('GET', '/api/settings'),
  saveSettings: (settings) => request('PUT', '/api/settings', { settings }),
  revealSecret: (kind, field, id = '') => request('POST', '/api/settings/reveal-secret', { kind, field, id }),
  saveNotificationChannels: (channels) => request('PUT', '/api/settings/notification-channels', { channels }),
  getAiSettings: () => request('GET', '/api/ai/settings'),
  saveAiSettings: (settings) => request('PUT', '/api/ai/settings', { settings }),
  getAiModels: (provider) => request('POST', '/api/ai/provider-models', { provider }),
  testAiCapability: (capability) => request('POST', '/api/ai/test', { capability }),
  getAiStatus: () => request('GET', '/api/ai/status'),
  getCookieSettings: () => request('GET', '/api/cookies/settings'),
  saveCookieSettings: (settings) => request('PUT', '/api/cookies/settings', { settings }),
  generateCookieCredentials: () => request('POST', '/api/cookies/credentials'),
  checkCookieSync: () => request('POST', '/api/cookies/check'),
  syncRemoteCookies: () => request('POST', '/api/cookies/remote-sync'),
  clearCookieData: () => request('DELETE', '/api/cookies/data'),
  restartPlatform: () => request('POST', '/api/system/restart'),
  migrateV1: async (file) => {
    const form = new FormData(); form.append('file', file)
    const headers = authHeaders(); delete headers['Content-Type']
    const res = await fetch('/api/system/migrate-v1', { method: 'POST', headers, body: form })
    if (!res.ok) { let detail = res.statusText; try { detail = (await res.json()).detail || detail } catch {}; throw new Error(detail) }
    return res.json()
  },
  downloadBackup: async () => {
    const res = await fetch('/api/system/backup', { method: 'POST', headers: authHeaders() })
    if (res.status === 401) {
      setToken('')
      if (onUnauthorized) onUnauthorized()
      throw new Error('未登录或登录已过期')
    }
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json()).detail || detail } catch {}
      throw new Error(detail)
    }
    const disposition = res.headers.get('content-disposition') || ''
    const m = /filename="?([^"]+)"?/.exec(disposition)
    return { blob: await res.blob(), filename: m?.[1] || 'awbotnest-backup.zip' }
  },
  downloadStoredBackup: async (filename) => {
    const res = await fetch(`/api/system/backups/${encodeURIComponent(filename)}`, { headers: authHeaders() })
    if (res.status === 401) {
      setToken('')
      if (onUnauthorized) onUnauthorized()
      throw new Error('未登录或登录已过期')
    }
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json()).detail || detail } catch {}
      throw new Error(detail)
    }
    return { blob: await res.blob(), filename }
  },
  restoreBackup: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const headers = authHeaders()
    delete headers['Content-Type']
    const res = await fetch('/api/system/restore', { method: 'POST', headers, body: form })
    if (res.status === 401) {
      setToken('')
      if (onUnauthorized) onUnauthorized()
      throw new Error('未登录或登录已过期')
    }
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json()).detail || detail } catch {}
      throw new Error(detail)
    }
    return res.json()
  },
  testProxy: (proxy_set) => request('POST', '/api/settings/test_proxy', { proxy_set }),
  testDb: (DB_INFO) => request('POST', '/api/settings/test_db', { DB_INFO }),
  cleanLogs: () => request('POST', '/api/system/clean_logs'),

  // 多 Bot / 通知推送路由
  listBots: () => request('GET', '/api/bots'),
  getBotsRouting: () => request('GET', '/api/bots/routing'),
  setBotRouting: (plugin_id, bot_id) => request('PUT', '/api/bots/routing', { plugin_id, bot_id }),

  // 工具方法
  clearCache: clearStatusCache,
}
