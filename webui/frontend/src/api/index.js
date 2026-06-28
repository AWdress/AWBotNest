// API 客户端：统一封装对后端的请求
// 鉴权：密码登录后拿到令牌，存 localStorage，请求带 Authorization: Bearer。

const TOKEN_KEY = 'awbotnest_token'
export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) }

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

export const api = {
  // 鉴权
  authStatus: () => request('GET', '/api/auth/status'),
  authLogin: (username, password) => request('POST', '/api/auth/login', { username, password }),
  changeCredentials: (old_password, new_username, new_password) =>
    request('POST', '/api/auth/change_credentials', { old_password, new_username, new_password }),

  // 插件
  listPlugins: () => request('GET', '/api/plugins'),
  enablePlugin: (id) => request('POST', `/api/plugins/${id}/enable`),
  disablePlugin: (id) => request('POST', `/api/plugins/${id}/disable`),
  reloadPlugin: (id) => request('POST', `/api/plugins/${id}/reload`),
  deletePlugin: (id) => request('DELETE', `/api/plugins/${id}`),
  getPluginConfig: (id) => request('GET', `/api/plugins/${id}/config`),
  setPluginConfig: (id, values) => request('PUT', `/api/plugins/${id}/config`, values),
  getPluginAccounts: (id) => request('GET', `/api/plugins/${id}/accounts`),
  setPluginAccounts: (id, sessions) => request('PUT', `/api/plugins/${id}/accounts`, { sessions }),

  // GitHub 导入
  githubList: (source, token) => request('POST', '/api/plugins/github/list', { source, token }),
  githubImport: (plugins, token) => request('POST', '/api/plugins/github/import', { plugins, token }),

  // 插件商店（多仓库聚合）
  pluginStore: (refresh = true) => request('GET', `/api/plugins/store?refresh=${refresh}`),
  storeDownload: (plugins) => request('POST', '/api/plugins/store/download', { plugins }),
  repoStatus: () => request('GET', '/api/plugins/repo/status'),

  // 上传（multipart）
  uploadPlugin: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const headers = authHeaders()
    delete headers['Content-Type'] // 让浏览器自动设置 multipart 边界
    const res = await fetch('/api/plugins/upload', { method: 'POST', headers, body: form })
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json()).detail || detail } catch {}
      throw new Error(detail)
    }
    return res.json()
  },

  // 系统状态
  status: () => request('GET', '/api/status'),

  // 账号
  listAccounts: () => request('GET', '/api/accounts'),
  accountOnline: (s) => request('POST', `/api/accounts/${s}/online`),
  accountOffline: (s) => request('POST', `/api/accounts/${s}/offline`),
  deleteAccount: (s) => request('DELETE', `/api/accounts/${s}`),
  loginSendCode: (session, phone) => request('POST', '/api/accounts/login/send_code', { session, phone }),
  loginSubmitCode: (session, code) => request('POST', '/api/accounts/login/submit_code', { session, code }),
  loginSubmitPassword: (session, password) => request('POST', '/api/accounts/login/submit_password', { session, password }),

  // 平台设置（config.json）
  getSettings: () => request('GET', '/api/settings'),
  saveSettings: (settings) => request('PUT', '/api/settings', { settings }),
}
