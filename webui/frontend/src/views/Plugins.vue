<script setup>
import { ref, onMounted, onUnmounted, computed, nextTick, watch } from 'vue'
import { api, getToken } from '../api'
import ConfigForm from '../components/ConfigForm.vue'
import RemotePluginConfig from '../components/RemotePluginConfig.vue'
import { confirm } from '../composables/confirm'
import { toast } from '../composables/toast'
import logo from '../assets/logo.png'

const tab = ref('mine')   // mine | store

const plugins = ref([])
const loading = ref(true)
const error = ref('')
const busy = ref({})
const fileInput = ref(null)

// 配置弹窗
const configOpen = ref(false)
const configTarget = ref(null)
const configSchema = ref({})
const configValues = ref({})
const configSaving = ref(false)
const configRenderMode = ref('schema')   // schema | vue
const configHasFrontend = ref(false)
const configBots = ref([])
const configBotChoice = ref([])       // 多选，存渠道 id 数组
const configBotConfirmed = ref([])
const configBotLoading = ref(false)
const configBotSaving = ref(false)
const configBotReady = ref(false)
let configBotRequestId = 0

// 三点下拉菜单：记录当前展开菜单的插件 id
const menuFor = ref(null)
const menuAlignRight = ref(false)   // 靠近右边界时菜单改为向左展开
function toggleMenu(p, ev) {
  if (menuFor.value === p.id) { menuFor.value = null; return }
  menuAlignRight.value = false
  menuFor.value = p.id
  // 展开后测量是否会超出视口右边界，会则贴着按钮左侧展开
  const wrap = ev?.currentTarget?.closest('.kebab-wrap')
  nextTick(() => {
    const menu = wrap?.querySelector('.dropdown')
    if (!menu) return
    const r = menu.getBoundingClientRect()
    if (r.right > window.innerWidth - 8) menuAlignRight.value = true
  })
}
function closeMenu() { menuFor.value = null }

// 应用账号弹窗（多账号下按账号选择插件）
const acctOpen = ref(false)
const acctTarget = ref(null)
const acctOptions = ref([])    // [{session,name}]
const acctSelected = ref([])   // 勾选的 session
const acctAllMode = ref(true)  // 应用到全部账号
const acctSaving = ref(false)

const scopeLabel = { user: '用户账号', bot: '机器人', both: '双账号' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.listPlugins()
    plugins.value = data.plugins
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function toggle(p) {
  if (p.error) return
  busy.value[p.id] = true
  try {
    const data = p.enabled ? await api.disablePlugin(p.id) : await api.enablePlugin(p.id)
    Object.assign(p, data.plugin)
  } catch (e) {
    error.value = `${p.name}: ${e.message}`
  } finally {
    busy.value[p.id] = false
  }
}

async function reload(p) {
  closeMenu()
  busy.value[p.id] = true
  try {
    const data = await api.reloadPlugin(p.id)
    Object.assign(p, data.plugin)
    toast.success(`插件「${p.name}」已重载`)
  } catch (e) {
    error.value = `${p.name}: ${e.message}`
  } finally {
    busy.value[p.id] = false
  }
}

async function remove(p) {
  closeMenu()
  const ok = await confirm({
    title: '删除插件',
    message: `确定删除插件「${p.name}」？\n此操作不可恢复。`,
    confirmText: '删除', danger: true,
  })
  if (!ok) return
  busy.value[p.id] = true
  try {
    await api.deletePlugin(p.id)
    plugins.value = plugins.value.filter((x) => x.id !== p.id)
    await loadStore(false)
  } catch (e) {
    error.value = `${p.name}: ${e.message}`
  } finally {
    busy.value[p.id] = false
  }
}

// ── 应用账号弹窗 ──
async function openAccounts(p) {
  closeMenu()
  acctTarget.value = p
  acctOptions.value = []
  acctSelected.value = []
  acctAllMode.value = true
  try {
    const data = await api.getPluginAccounts(p.id)
    acctOptions.value = data.accounts || []
    acctSelected.value = [...(data.selected || [])]
    acctAllMode.value = acctSelected.value.length === 0
    acctOpen.value = true
  } catch (e) { error.value = e.message }
}

async function openConfig(p) {
  closeMenu()
  configTarget.value = p
  try {
    const data = await api.getPluginConfig(p.id)
    configSchema.value = data.schema || {}
    configValues.value = data.values || {}
    configRenderMode.value = data.render_mode || 'schema'
    configHasFrontend.value = !!data.has_frontend
    configOpen.value = true
    loadWebhook()
    loadConfigBot(p.id)
  } catch (e) {
    error.value = e.message
  }
}

async function loadConfigBot(pluginId) {
  const requestId = ++configBotRequestId
  configBotLoading.value = true
  configBotReady.value = false
  configBots.value = []
  configBotChoice.value = []
  configBotConfirmed.value = []
  try {
    const data = await api.getBotsRouting()
    if (requestId !== configBotRequestId) return
    const selectedStr = (data.plugins || []).find((item) => item.id === pluginId)?.bot || ''
    configBots.value = data.bots || []
    const selected = selectedStr ? selectedStr.split(',').map(s => s.trim()).filter(Boolean) : []
    configBotChoice.value = selected
    configBotConfirmed.value = [...selected]
    configBotReady.value = true
  } catch (e) {
    if (requestId === configBotRequestId) toast.error('读取通知渠道失败：' + e.message)
  } finally {
    if (requestId === configBotRequestId) configBotLoading.value = false
  }
}

async function saveConfigBot() {
  if (!configTarget.value || configBotSaving.value) return
  const previous = [...configBotConfirmed.value]
  configBotSaving.value = true
  try {
    const botIdStr = configBotChoice.value.join(',')
    const data = await api.setBotRouting(configTarget.value.id, botIdStr)
    const saved = data.bot ? data.bot.split(',').map(s => s.trim()).filter(Boolean) : []
    configBotChoice.value = saved
    configBotConfirmed.value = [...saved]
    api.clearCache()
    eventBus.emit(EVENTS.BOT_ROUTING_CHANGED, { pluginId: configTarget.value.id, botId: data.bot || '' })
    toast.success(`「${configTarget.value.name}」通知渠道已更新`)
  } catch (e) {
    configBotChoice.value = previous
    toast.error('保存通知渠道失败：' + e.message)
  } finally {
    configBotSaving.value = false
  }
}

function configDefaultBotName() {
  return configBots.value.find((bot) => bot.is_default)?.name || '主要通知渠道'
}

function toggleConfigBot(id) {
  const idx = configBotChoice.value.indexOf(id)
  if (idx >= 0) configBotChoice.value.splice(idx, 1)
  else configBotChoice.value.push(id)
  saveConfigBot()
}

// 复制到剪贴板：优先 navigator.clipboard（需安全上下文），
// 非 HTTPS（http://IP:端口）下降级到临时 textarea + execCommand('copy')。
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* 落到降级方案 */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus(); ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch { return false }
}

// ── 插件 webhook（入站地址；密钥用平台统一的 WEBHOOK_SECRET，在「系统设置 → 通知」生成） ──
const webhookSecret = ref('')
const webhookPath = ref('')

const webhookUrl = computed(() => {
  if (!webhookSecret.value || !webhookPath.value) return ''
  return `${location.origin}${webhookPath.value}?apikey=${webhookSecret.value}`
})

async function loadWebhook() {
  webhookSecret.value = ''
  webhookPath.value = ''
  if (!configTarget.value?.webhook) return
  try {
    const d = await api.getPluginWebhook(configTarget.value.id)
    webhookSecret.value = d.secret || ''
    webhookPath.value = d.path || ''
  } catch { /* 配置弹窗照常打开，webhook 区留空 */ }
}

async function copyWebhookUrl() {
  if (!webhookUrl.value) return
  if (await copyText(webhookUrl.value)) toast.success('已复制 webhook 地址')
  else toast.error('复制失败，请手动选择复制')
}

const configFormRef = ref(null)

async function saveConfig() {
  // 保存前校验（必填 / 数字范围）；不过就停在弹窗里提示，不提交
  if (configFormRef.value && !configFormRef.value.validate()) {
    toast.error('请检查标红的必填项或超范围的数值')
    return
  }
  configSaving.value = true
  try {
    await api.setPluginConfig(configTarget.value.id, configValues.value)
    configOpen.value = false
  } catch (e) {
    error.value = e.message
  } finally {
    configSaving.value = false
  }
}

function toggleAcct(session) {
  const i = acctSelected.value.indexOf(session)
  if (i >= 0) acctSelected.value.splice(i, 1)
  else acctSelected.value.push(session)
}
async function saveAccounts() {
  acctSaving.value = true
  try {
    const sessions = acctAllMode.value ? [] : acctSelected.value
    await api.setPluginAccounts(acctTarget.value.id, sessions)
    acctOpen.value = false
    toast.success('账号范围已保存')
  } catch (e) { error.value = e.message } finally { acctSaving.value = false }
}

// ── 插件日志弹窗（只看当前插件的日志） ──
const logsOpen = ref(false)
const logsTarget = ref(null)
const logsList = ref([])          // 已按插件过滤的日志
const logsConnected = ref(false)
const logsBox = ref(null)
let logsWs = null
let logsReconnect = null

const logLevelClass = (lv) => ({
  DEBUG: 'lv-debug', INFO: 'lv-info', WARNING: 'lv-warn',
  ERROR: 'lv-err', CRITICAL: 'lv-err',
}[lv] || 'lv-info')

const pluginLogTime = (item) => item.date ? `${item.date.slice(5)} ${item.time}` : item.time

function matchesPlugin(item, id) {
  // 插件 ctx.log 会把 source 设成插件 id；兜底再匹配 msg 里的 [id] 前缀
  return item.source === id || (item.msg && item.msg.startsWith(`[${id}]`))
}

function logsScrollBottom() {
  if (logsBox.value) logsBox.value.scrollTop = logsBox.value.scrollHeight
}

function logsWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/api/logs/ws?token=${encodeURIComponent(getToken())}`
}

function logsConnect() {
  logsWs = new WebSocket(logsWsUrl())
  logsWs.onopen = () => { logsConnected.value = true }
  logsWs.onmessage = (e) => {
    try {
      const item = JSON.parse(e.data)
      if (logsTarget.value && matchesPlugin(item, logsTarget.value.id)) {
        logsList.value.push(item)
        if (logsList.value.length > 1000) logsList.value.splice(0, logsList.value.length - 1000)
        nextTick(logsScrollBottom)
      }
    } catch {}
  }
  logsWs.onclose = () => {
    logsConnected.value = false
    if (logsOpen.value) logsReconnect = setTimeout(logsConnect, 3000)
  }
  logsWs.onerror = () => { logsWs?.close() }
}

function logsDisconnect() {
  clearTimeout(logsReconnect)
  if (logsWs) { logsWs.onclose = null; logsWs.close(); logsWs = null }
  logsConnected.value = false
}

async function openLogs(p) {
  closeMenu()
  logsTarget.value = p
  logsList.value = []
  logsOpen.value = true
  // 先拉历史，过滤出当前插件
  try {
    const d = await api.recentLogs()
    logsList.value = (d.logs || []).filter((it) => matchesPlugin(it, p.id))
  } catch (e) { error.value = e.message }
  nextTick(logsScrollBottom)
  logsConnect()
}

function closeLogs() {
  logsOpen.value = false
  logsDisconnect()
}

function triggerUpload() { fileInput.value?.click() }
async function onFile(e) {
  const file = e.target.files?.[0]
  if (!file) return
  try {
    await api.uploadPlugin(file)
    await load()
    toast.success(`插件「${file.name}」安装完成`)
  } catch (err) {
    error.value = `上传失败: ${err.message}`
    toast.error('插件上传失败')
  } finally {
    e.target.value = ''
  }
}

// 拖拽上传
const dragging = ref(false)
async function onDrop(e) {
  dragging.value = false
  if (tab.value !== 'mine') return
  const file = e.dataTransfer.files?.[0]
  if (!file || !file.name.endsWith('.py')) {
    error.value = '只接受 .py 文件'
    return
  }
  try {
    await api.uploadPlugin(file)
    await load()
    toast.success(`插件「${file.name}」安装完成`)
  } catch (err) {
    error.value = `上传失败: ${err.message}`
    toast.error('插件上传失败')
  }
}

const stats = computed(() => ({
  total: plugins.value.length,
  enabled: plugins.value.filter((p) => p.enabled).length,
  error: plugins.value.filter((p) => p.error).length,
}))

const pluginFilter = ref('all')
const filteredPlugins = computed(() => {
  if (pluginFilter.value === 'enabled') return plugins.value.filter((p) => p.enabled && !p.error)
  if (pluginFilter.value === 'disabled') return plugins.value.filter((p) => !p.enabled && !p.error)
  if (pluginFilter.value === 'error') return plugins.value.filter((p) => p.error)
  return plugins.value
})

// ── 插件市场（多仓库聚合） ──
const store = ref([])
const officialIds = ref([])
const storeBusy = ref(false)
const storeErr = ref('')
const storeLastSync = ref(null)
const dlBusy = ref({})

// 应用筛选和排序
function applyStoreFilters(list) {
  let filtered = [...list]

  // 应用作者筛选
  if (filterAuthor.value) {
    filtered = filtered.filter(p => p.author === filterAuthor.value)
  }

  // 应用仓库筛选
  if (filterRepo.value) {
    filtered = filtered.filter(p => p.repoUrl === filterRepo.value)
  }

  // 应用排序
  if (filterSort.value === 'name') {
    filtered.sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id, 'zh-CN'))
  } else if (filterSort.value === 'author') {
    filtered.sort((a, b) => (a.author || '').localeCompare(b.author || '', 'zh-CN'))
  } else if (filterSort.value === 'repo') {
    filtered.sort((a, b) => (a.repo || '').localeCompare(b.repo || '', 'zh-CN'))
  } else if (filterSort.value === 'latest') {
    filtered.sort((a, b) => (b.id || '').localeCompare(a.id || ''))
  }
  // hot 排序保持原顺序（已在后端或初始列表中排序）

  return filtered
}

const storeAvailable = computed(() => applyStoreFilters(store.value.filter((p) => !p.installed)))
// 已安装但仓库有新版本的插件：仅当平台记录过下载版本(local_version)且与远端不同才提示，
// 本地上传/手动导入(无 local_version)不误报更新，避免静默覆盖本地改动。
function hasUpdate(p) {
  return p.installed && p.from_manifest && p.local_version && p.version && p.local_version !== p.version
}
const storeUpdatable = computed(() => applyStoreFilters(store.value.filter(hasUpdate)))
const officialSet = computed(() => new Set(officialIds.value))
function isOfficial(p) { return p.official || officialSet.value.has(p.id) }

// ── 插件搜索（已安装 + 市场） ──
const searchOpen = ref(false)
const searchQuery = ref('')
const searchInput = ref(null)
const searchFilter = ref('all')
const searchAuthorFilter = ref('')
const searchRepoFilter = ref('')
const searchSort = ref('hot')  // hot, name, author, repo, latest
const searchActiveIndex = ref(0)

// ── 插件市场筛选弹窗 ──
const filterOpen = ref(false)
const filterAuthor = ref('')
const filterRepo = ref('')
const filterSort = ref('hot')  // hot, name, author, repo, latest

const searchablePlugins = computed(() => {
  const localById = new Map(plugins.value.map((p) => [p.id, p]))
  const marketById = new Map(store.value.map((p) => [p.id, p]))
  const ids = new Set([...localById.keys(), ...marketById.keys()])

  return [...ids].map((id) => {
    const local = localById.get(id)
    const market = marketById.get(id)
    const installed = !!local || !!market?.installed
    return {
      id,
      name: local?.name || market?.name || id,
      description: local?.description || market?.description || '',
      changelog: local?.changelog || market?.changelog || '',
      author: local?.author || market?.author || '',
      icon: local?.icon || market?.icon || '',
      version: market?.version || local?.version || '',
      enabled: !!local?.enabled,
      error: local?.error || '',
      installed,
      updateAvailable: !!market && hasUpdate(market),
      official: isOfficial(market || local || { id }),
      repo: shortRepo(market?.repo_url || ''),
      repoUrl: market?.repo_url || '',
      tags: market?.tags || local?.tags || [],
      localPlugin: local,
      marketPlugin: market,
    }
  })
})

const searchResults = computed(() => {
  const words = searchQuery.value.trim().toLowerCase().split(/\s+/).filter(Boolean)
  let results = searchablePlugins.value.filter((p) => {
    if (searchFilter.value === 'installed' && !p.installed) return false
    if (searchFilter.value === 'updates' && !p.updateAvailable) return false
    if (searchFilter.value === 'available' && p.installed) return false
    if (searchAuthorFilter.value && p.author !== searchAuthorFilter.value) return false
    if (searchRepoFilter.value && p.repoUrl !== searchRepoFilter.value) return false
    if (!words.length) return true
    const text = [p.name, p.id, p.description, p.author, p.repo].join(' ').toLowerCase()
    return words.every((word) => text.includes(word))
  })

  // 应用排序
  if (searchSort.value === 'hot') {
    // 热门排序：可更新 > 已安装 > 未安装，同级别按名称
    results.sort((a, b) => {
      const rank = (p) => p.updateAvailable ? 0 : p.installed ? 1 : 2
      return rank(a) - rank(b) || a.name.localeCompare(b.name, 'zh-CN')
    })
  } else if (searchSort.value === 'name') {
    results.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  } else if (searchSort.value === 'author') {
    results.sort((a, b) => (a.author || '').localeCompare(b.author || '', 'zh-CN') || a.name.localeCompare(b.name, 'zh-CN'))
  } else if (searchSort.value === 'repo') {
    results.sort((a, b) => (a.repo || '').localeCompare(b.repo || '', 'zh-CN') || a.name.localeCompare(b.name, 'zh-CN'))
  } else if (searchSort.value === 'latest') {
    // 最新发布：假设version越高越新，降序排列
    results.sort((a, b) => {
      const versionCompare = (b.version || '0').localeCompare(a.version || '0', undefined, { numeric: true })
      return versionCompare || a.name.localeCompare(b.name, 'zh-CN')
    })
  }

  return results
})

// 获取所有作者列表（用于筛选下拉）
const allAuthors = computed(() => {
  const authors = new Set()
  searchablePlugins.value.forEach(p => {
    if (p.author && p.author.trim()) authors.add(p.author.trim())
  })
  return [...authors].sort((a, b) => a.localeCompare(b, 'zh-CN'))
})

// 获取市场中的所有作者列表（用于市场筛选）
const storeAuthors = computed(() => {
  const authors = new Set()
  store.value.forEach(p => {
    if (p.author && p.author.trim()) authors.add(p.author.trim())
  })
  return [...authors].sort((a, b) => a.localeCompare(b, 'zh-CN'))
})

// 获取所有仓库列表（用于市场筛选）
const storeRepos = computed(() => {
  const repos = new Map()  // repoUrl -> shortName
  store.value.forEach(p => {
    if (p.repoUrl && p.repo) {
      repos.set(p.repoUrl, p.repo)
    }
  })
  return [...repos.entries()]
    .map(([url, name]) => ({ url, name }))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
})

// 获取所有仓库列表
const allRepos = computed(() => {
  const repos = new Map()  // repoUrl -> shortName
  searchablePlugins.value.forEach(p => {
    if (p.repoUrl && p.repo) {
      repos.set(p.repoUrl, p.repo)
    }
  })
  return [...repos.entries()]
    .map(([url, name]) => ({ url, name }))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
})

watch([searchQuery, searchFilter, searchAuthorFilter, searchRepoFilter, searchSort], () => { searchActiveIndex.value = 0 })

function openPluginSearch() {
  searchQuery.value = ''
  searchFilter.value = 'all'
  searchAuthorFilter.value = ''
  searchRepoFilter.value = ''
  searchSort.value = 'hot'
  searchActiveIndex.value = 0
  searchOpen.value = true
  if (store.value.length === 0 && !storeBusy.value) loadStore(false)
  nextTick(() => searchInput.value?.focus())
}

function onSearchInputKeydown(e) {
  if (!searchResults.value.length) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    searchActiveIndex.value = (searchActiveIndex.value + 1) % searchResults.value.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    searchActiveIndex.value = (searchActiveIndex.value - 1 + searchResults.value.length) % searchResults.value.length
  } else if (e.key === 'Enter') {
    e.preventDefault()
    runSearchAction(searchResults.value[searchActiveIndex.value])
  }
}

function closePluginSearch() {
  searchOpen.value = false
}

async function runSearchAction(p) {
  if (p.updateAvailable || !p.installed) {
    if (p.marketPlugin) await download(p.marketPlugin)
    return
  }
  if (p.localPlugin) {
    closePluginSearch()
    await openConfig(p.localPlugin)
  }
}

// ── 版本历史弹窗 ──
const changelogOpen = ref(false)
const changelogTarget = ref(null)

function openChangelog(p) {
  closeMenu()
  changelogTarget.value = p
  changelogOpen.value = true
}

function onSearchHotkey(e) {
  if (e.key === 'Escape' && searchOpen.value) {
    closePluginSearch()
    return
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    openPluginSearch()
  }
}

// GitHub 链接和简写统一显示为 owner/repo。
function normalizeRepo(value) {
  if (!value) return ''
  let source = String(value).trim()
  source = source.replace(/^https?:\/\/(www\.)?github\.com\//i, '')
  source = source.split(/[?#]/, 1)[0].replace(/^\/+|\/+$/g, '')
  const parts = source.split('/')
  if (parts.length < 2) return ''
  const owner = parts[0].trim()
  const repo = parts[1].trim().replace(/\.git$/i, '')
  const validPart = /^[A-Za-z0-9_.-]+$/
  return validPart.test(owner) && repo.toLowerCase() === 'awbotnest-plugins'
    ? `${owner}/AWBotNest-Plugins`
    : ''
}

function shortRepo(value) {
  return normalizeRepo(value) || String(value || '').trim()
}

function pluginHomepage(p) {
  const source = store.value.find((item) => item.id === p.id)?.repo_url
  const repo = normalizeRepo(source)
  return repo ? `https://github.com/${repo}` : ''
}

function openPluginHomepage(p) {
  const url = pluginHomepage(p)
  if (!url) return
  closeMenu()
  const opened = window.open(url, '_blank', 'noopener,noreferrer')
  if (opened) opened.opener = null
}

async function loadStore(refresh = false) {
  storeBusy.value = true; storeErr.value = ''
  try {
    const d = await api.pluginStore(refresh)
    store.value = d.plugins || []
    officialIds.value = d.official_ids || []
    storeLastSync.value = d.last_sync
    if (d.errors && d.errors.length) storeErr.value = d.errors.join('；')
  } catch (e) {
    storeErr.value = e.message
  } finally {
    storeBusy.value = false
  }
}

async function download(p) {
  const isUpdate = hasUpdate(p)
  dlBusy.value[p.id] = true; storeErr.value = ''
  try {
    const r = await api.storeDownload([p])
    const res = r.result || {}
    if (res.errors && res.errors.length) {
      storeErr.value = res.errors.join('；')
      toast.error(`${p.name} ${isUpdate ? '更新' : '安装'}失败`)
    } else {
      p.installed = true
      p.local_version = p.version   // 记录新版本，清除「有更新」提示
      await load()
      const reloaded = (res.reloaded || []).includes(p.id)
      toast.success(isUpdate
        ? `插件「${p.name}」已更新到 v${p.version}${reloaded ? '（运行中实例已热重载）' : ''}`
        : `插件「${p.name}」安装完成`)
    }
  } catch (e) {
    storeErr.value = `${p.name}: ${e.message}`
    toast.error(`${p.name} ${isUpdate ? '更新' : '安装'}失败`)
  } finally {
    dlBusy.value[p.id] = false
  }
}

// ── 设置 GitHub 仓库地址（多仓库） ──
const repoOpen = ref(false)
const repoList = ref([])
const repoSaving = ref(false)
const repoErr = ref('')

async function openRepos() {
  repoErr.value = ''
  try {
    const d = await api.getSettings()
    const s = d.settings || {}
    repoList.value = (s.PLUGIN_REPOS || []).map((r) => ({ url: normalizeRepo(r.url) || '' }))
    repoOpen.value = true
  } catch (e) {
    repoErr.value = e.message
  }
}

function addRepo() { repoList.value.push({ url: '' }) }
function delRepo(i) { repoList.value.splice(i, 1) }
function normalizeRepoRow(repo) {
  const source = (repo.url || '').trim()
  if (!source) return
  const normalized = normalizeRepo(source)
  if (normalized) repo.url = normalized
}

async function saveRepos() {
  repoSaving.value = true; repoErr.value = ''
  try {
    const repos = []
    const seen = new Set()
    const duplicates = []
    let officialSkipped = false
    for (let i = 0; i < repoList.value.length; i += 1) {
      const source = (repoList.value[i].url || '').trim()
      if (!source) continue
      const url = normalizeRepo(source)
      if (!url) throw new Error(`第 ${i + 1} 个仓库不符合要求，仓库名必须是 AWBotNest-Plugins`)
      repoList.value[i].url = url
      const key = url.toLowerCase()
      if (key === 'awdress/awbotnest-plugins') {
        officialSkipped = true
        continue
      }
      if (seen.has(key)) {
        duplicates.push(url)
        continue
      }
      seen.add(key)
      repos.push({ url })
    }
    await api.saveSettings({
      PLUGIN_REPOS: repos,
    })
    repoOpen.value = false
    await loadStore(true)
    if (officialSkipped) toast.info('官方仓库 AWdress/AWBotNest-Plugins 已内置，无需重复添加')
    if (duplicates.length) toast.info(`仓库已存在，已忽略：${[...new Set(duplicates)].join('、')}`)
    if (!officialSkipped && !duplicates.length) {
      toast.success(repos.length ? `已保存 ${repos.length} 个额外仓库` : '额外仓库已清空')
    } else if (repos.length) {
      toast.success(`其余 ${repos.length} 个额外仓库已保存`)
    }
  } catch (e) {
    repoErr.value = e.message
  } finally {
    repoSaving.value = false
  }
}

function goStore() { tab.value = 'store'; if (store.value.length === 0) loadStore(true) }

onMounted(() => {
  load(); loadStore(false)
  document.addEventListener('click', closeMenu)
  window.addEventListener('keydown', onSearchHotkey)
})
onUnmounted(() => {
  logsDisconnect()
  document.removeEventListener('click', closeMenu)
  window.removeEventListener('keydown', onSearchHotkey)
})
</script>

<template>
  <div
    class="plugins"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDrop"
  >
    <!-- 顶部：Tab 切换 + 操作 -->
    <div class="toolbar">
      <div class="tabs">
        <button class="tab" :class="{ active: tab === 'mine' }" @click="tab = 'mine'">
          我的插件 <span class="tab-count">{{ stats.total }}</span>
        </button>
        <button class="tab" :class="{ active: tab === 'store' }" @click="goStore">
          插件市场 <span class="tab-count" v-if="storeAvailable.length + storeUpdatable.length">{{ storeAvailable.length + storeUpdatable.length }}</span>
        </button>
      </div>
      <div class="row gap">
        <template v-if="tab === 'mine'">
          <span class="stats">已启用 <b style="color:var(--accent-2)">{{ stats.enabled }}</b><template v-if="stats.error"> · <span style="color:var(--danger)">异常 {{ stats.error }}</span></template></span>
          <button class="btn" @click="load">刷新</button>
          <button class="btn btn-primary" @click="triggerUpload">+ 上传插件</button>
          <input ref="fileInput" type="file" accept=".py" hidden @change="onFile" />
        </template>
        <template v-else>
          <span class="stats muted small" v-if="storeLastSync">上次刷新 {{ storeLastSync }}</span>
          <div class="filter-dropdown-wrapper">
            <button class="btn" @click.stop="filterOpen = !filterOpen">筛选</button>
            <div v-if="filterOpen" class="filter-dropdown" @click.stop>
              <div class="filter-section">
                <label class="filter-label">排序方式</label>
                <div class="filter-options">
                  <button :class="{ active: filterSort === 'hot' }" @click="filterSort = 'hot'">热门</button>
                  <button :class="{ active: filterSort === 'name' }" @click="filterSort = 'name'">名称</button>
                  <button :class="{ active: filterSort === 'author' }" @click="filterSort = 'author'">作者</button>
                  <button :class="{ active: filterSort === 'latest' }" @click="filterSort = 'latest'">最新</button>
                </div>
              </div>
              <div class="filter-section" v-if="storeAuthors.length > 0">
                <label class="filter-label">按作者筛选</label>
                <select class="select" v-model="filterAuthor">
                  <option value="">全部作者</option>
                  <option v-for="author in storeAuthors" :key="author" :value="author">{{ author }}</option>
                </select>
              </div>
              <div class="filter-section" v-if="storeRepos.length > 0">
                <label class="filter-label">按仓库筛选</label>
                <select class="select" v-model="filterRepo">
                  <option value="">全部仓库</option>
                  <option v-for="repo in storeRepos" :key="repo.url" :value="repo.url">{{ repo.name }}</option>
                </select>
              </div>
              <div class="filter-actions">
                <button class="btn btn-sm" @click="filterAuthor = ''; filterRepo = ''; filterSort = 'hot'">重置</button>
              </div>
            </div>
          </div>
          <button class="btn" @click="openRepos">设置仓库地址</button>
          <button class="btn btn-primary" @click="loadStore(true)" :disabled="storeBusy">
            {{ storeBusy ? '刷新中…' : '刷新市场' }}
          </button>
        </template>
      </div>
    </div>

    <div class="plugin-controls">
      <div v-if="tab === 'mine'" class="filter-pills" aria-label="筛选插件">
        <button :class="{ active: pluginFilter === 'all' }" @click="pluginFilter='all'">全部 {{ stats.total }}</button>
        <button :class="{ active: pluginFilter === 'enabled' }" @click="pluginFilter='enabled'">已启用 {{ stats.enabled }}</button>
        <button :class="{ active: pluginFilter === 'disabled' }" @click="pluginFilter='disabled'">未启用 {{ stats.total - stats.enabled - stats.error }}</button>
        <button v-if="stats.error" class="danger" :class="{ active: pluginFilter === 'error' }" @click="pluginFilter='error'">异常 {{ stats.error }}</button>
      </div>
      <div v-else class="control-caption">浏览并安装公开仓库中的插件</div>
    </div>

    <div v-if="error" class="alert">{{ error }} <span @click="error=''" class="close"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span></div>

    <!-- ════ 我的插件 ════ -->
    <template v-if="tab === 'mine'">
      <div v-if="loading" class="muted center">加载中…</div>
      <div v-else-if="plugins.length === 0" class="empty card">
        <p>还没有插件。</p>
        <p class="muted">去「插件市场」安装，或把 .py 文件拖到这里 / 点「上传插件」。</p>
      </div>
      <div v-else-if="filteredPlugins.length === 0" class="empty card filter-empty">
        <p>没有符合当前筛选条件的插件。</p>
        <button class="btn" @click="pluginFilter='all'">查看全部插件</button>
      </div>
      <div v-else class="grid">
        <div v-for="p in filteredPlugins" :key="p.id" class="card plugin-card clickable"
             :class="{ err: p.error, 'menu-open': menuFor === p.id }"
             @click="openConfig(p)">
          <div class="card-head">
            <div class="store-title">
              <img :src="p.icon || logo" class="store-icon" :class="{ 'store-icon-fallback': !p.icon }" alt="" />
              <div class="card-title">
                <span class="name">
                  <span class="name-text">{{ p.name }}</span>
                  <span v-if="isOfficial(p)" class="badge-official">官方</span>
                  <span v-if="p.render_mode === 'vue'" class="badge-vue">Vue</span>
                </span>
                <span class="badge" :class="p.error ? 'badge-err' : (p.enabled ? 'badge-on' : 'badge-off')">
                  {{ p.error ? '异常' : (p.enabled ? '已启用' : '未启用') }}
                </span>
              </div>
            </div>
            <div class="toggle" :class="{ on: p.enabled, disabled: p.error || busy[p.id] }"
                 @click.stop="toggle(p)"></div>
          </div>

          <p class="desc">{{ p.description || '（无描述）' }}</p>
          <div v-if="p.error" class="err-msg">{{ p.error }}</div>

          <div class="card-meta">
            <span class="meta-item">{{ scopeLabel[p.scope] || p.scope }}</span>
            <span class="meta-item">v{{ p.version }}</span>
            <span v-if="p.author" class="meta-item">{{ p.author }}</span>
            <div class="kebab-wrap">
              <button class="kebab" :class="{ active: menuFor === p.id }" @click.stop="toggleMenu(p, $event)"
                      title="更多" aria-label="更多">
                <svg class="kebab-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="1"/>
                  <circle cx="12" cy="5" r="1"/>
                  <circle cx="12" cy="19" r="1"/>
                </svg>
              </button>
              <div v-if="menuFor === p.id" class="dropdown" :class="{ 'align-right': menuAlignRight }" @click.stop>
                <button class="menu-item" @click.stop="openConfig(p)">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg> 配置
                </button>
                <button class="menu-item" v-if="p.changelog" @click.stop="openChangelog(p)">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8M16 17H8M10 9H8"/></svg> 版本历史
                </button>
                <button class="menu-item" v-if="pluginHomepage(p)" @click.stop="openPluginHomepage(p)">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg> 项目主页
                </button>
                <button class="menu-item" @click.stop="openLogs(p)">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8M16 17H8M10 9H8"/></svg> 查看日志
                </button>
                <button class="menu-item" v-if="p.scope === 'user' || p.scope === 'both'" @click.stop="openAccounts(p)">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM4 21a8 8 0 0 1 16 0"/></svg> 应用账号
                </button>
                <button class="menu-item" @click.stop="reload(p)" :disabled="busy[p.id]">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> 重载
                </button>
                <div class="menu-sep"></div>
                <button class="menu-item danger" @click.stop="remove(p)" :disabled="busy[p.id]">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/></svg> 删除
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- ════ 插件市场 ════ -->
    <template v-else>
      <div class="hint muted store-hint">来自官方仓库与你配置的 GitHub 仓库。点「安装」拉到本地（不自动启用），安装后到「我的插件」开启。</div>
      <div v-if="storeErr" class="alert">{{ storeErr }} <span @click="storeErr=''" class="close"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span></div>

      <div v-if="storeBusy && store.length === 0" class="muted center">加载市场…</div>
      <template v-else>
        <!-- 有更新的已安装插件 -->
        <div v-if="storeUpdatable.length" class="update-section">
          <div class="section-label">可更新（{{ storeUpdatable.length }}）</div>
          <div class="grid" :class="{ compact: density === 'compact' }">
            <div v-for="p in storeUpdatable" :key="p.id" class="card plugin-card store-card has-update">
              <div class="card-head">
                <div class="store-title">
                  <img :src="p.icon || logo" class="store-icon" :class="{ 'store-icon-fallback': !p.icon }" alt="" />
                  <div class="card-title">
                    <span class="name">{{ p.name }}
                      <span v-if="isOfficial(p)" class="badge-official">官方</span>
                    </span>
                    <span class="badge badge-update">v{{ p.local_version }} → v{{ p.version }}</span>
                  </div>
                </div>
              </div>

              <p class="desc">{{ p.description || '（无描述）' }}</p>
              <div class="card-meta">
                <span v-if="p.author" class="meta-item">{{ p.author }}</span>
                <span class="meta-item mono">{{ shortRepo(p.repo_url) }}</span>
              </div>

              <div class="card-actions">
                <button class="btn sm btn-primary" @click.stop="download(p)" :disabled="dlBusy[p.id]">
                  <template v-if="dlBusy[p.id]">更新中…</template>
                  <template v-else>
                    <svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                         stroke-linecap="round" stroke-linejoin="round">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
                    </svg>更新
                  </template>
                </button>
              </div>
            </div>
          </div>
        </div>

        <div class="section-label" v-if="storeUpdatable.length && storeAvailable.length">可安装</div>
        <div v-if="storeAvailable.length === 0 && storeUpdatable.length === 0" class="empty card">
          <p class="muted">市场里没有可安装的新插件（仓库里的都已安装），或还没配置额外仓库。</p>
        </div>
        <div v-else-if="storeAvailable.length" class="grid" :class="{ compact: density === 'compact' }">
        <div v-for="p in storeAvailable" :key="p.id" class="card plugin-card store-card">
          <div class="card-head">
            <div class="store-title">
              <img :src="p.icon || logo" class="store-icon" :class="{ 'store-icon-fallback': !p.icon }" alt="" />
              <div class="card-title">
                <span class="name">{{ p.name }}
                  <span v-if="isOfficial(p)" class="badge-official">官方</span>
                </span>
                <span class="badge badge-off" v-if="p.version">v{{ p.version }}</span>
              </div>
            </div>
          </div>

          <p class="desc">{{ p.description || '（无描述）' }}</p>
          <div class="card-meta">
            <span v-if="p.author" class="meta-item">{{ p.author }}</span>
            <span class="meta-item mono">{{ shortRepo(p.repo_url) }}</span>
          </div>

          <div class="card-actions">
            <button class="btn sm btn-primary" @click.stop="download(p)" :disabled="dlBusy[p.id]">
              <template v-if="dlBusy[p.id]">安装中…</template>
              <template v-else>
                <svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
                </svg>安装
              </template>
            </button>
          </div>
        </div>
      </div>
      </template>
    </template>

    <!-- 拖拽遮罩 -->
    <div v-if="dragging && tab === 'mine'" class="drag-overlay">松手上传 .py 插件</div>

    <!-- 右下角插件搜索 -->
    <button class="plugin-search-fab" type="button" title="搜索插件（Ctrl+K）"
            aria-label="搜索插件" @click="openPluginSearch">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
           stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>
      </svg>
    </button>

    <div v-if="searchOpen" class="modal-mask search-mask" @click.self="closePluginSearch">
      <div class="search-modal card" role="dialog" aria-modal="true" aria-label="搜索插件">
        <div class="search-head">
          <div class="search-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>
            </svg>
            <span>搜索插件</span>
          </div>
          <button class="search-close" type="button" aria-label="关闭" @click="closePluginSearch">
            <svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button>
        </div>

        <div class="search-input-wrap">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>
          </svg>
          <input ref="searchInput" v-model="searchQuery" type="search"
                 placeholder="输入插件名称、作者、说明或英文标识" autocomplete="off"
                 @keydown="onSearchInputKeydown" />
          <span class="search-count">{{ searchResults.length }}</span>
        </div>

        <div class="search-filters" aria-label="搜索范围">
          <button :class="{ active: searchFilter === 'all' }" @click="searchFilter='all'">全部</button>
          <button :class="{ active: searchFilter === 'installed' }" @click="searchFilter='installed'">已安装</button>
          <button :class="{ active: searchFilter === 'updates' }" @click="searchFilter='updates'">可更新</button>
          <button :class="{ active: searchFilter === 'available' }" @click="searchFilter='available'">可安装</button>
        </div>

        <div class="search-list">
          <div v-if="storeBusy && searchablePlugins.length === 0" class="search-empty">正在读取插件…</div>
          <div v-else-if="searchResults.length === 0" class="search-empty">
            <template v-if="searchQuery.trim()">没有找到“{{ searchQuery.trim() }}”相关的插件</template>
            <template v-else>暂时没有可搜索的插件</template>
          </div>
          <div v-for="(p, index) in searchResults" :key="p.id" class="search-item"
               :class="{ actionable: p.installed && !p.updateAvailable, active: index === searchActiveIndex }"
               @mouseenter="searchActiveIndex=index"
               @click="p.installed && !p.updateAvailable && runSearchAction(p)">
            <img :src="p.icon || logo" class="search-icon" :class="{ fallback: !p.icon }" alt="" />
            <div class="search-info">
              <div class="search-name-row">
                <span class="search-name">{{ p.name }}</span>
                <span class="search-version" v-if="p.version">v{{ p.version }}</span>
                <span v-if="p.official" class="badge-official">官方</span>
                <span v-if="p.updateAvailable" class="search-state update">可更新</span>
                <span v-else-if="p.error" class="search-state error">异常</span>
                <span v-else-if="p.installed" class="search-state installed">{{ p.enabled ? '已启用' : '已安装' }}</span>
                <span v-else class="search-state available">可安装</span>
              </div>
              <div class="search-desc">{{ p.description || '暂无说明' }}</div>
              <div class="search-meta">
                <span class="mono">{{ p.id }}</span>
                <span v-if="p.author">{{ p.author }}</span>
                <span v-if="p.repo">{{ p.repo }}</span>
              </div>
            </div>
            <button class="search-action" type="button" :disabled="dlBusy[p.id]"
                    @click.stop="runSearchAction(p)">
              <template v-if="dlBusy[p.id]">处理中…</template>
              <template v-else-if="p.updateAvailable">更新</template>
              <template v-else-if="p.installed">配置</template>
              <template v-else>安装</template>
            </button>
          </div>
        </div>
        <div class="search-foot">
          <span>支持搜索名称、作者、说明和英文标识</span>
          <span><kbd>↑↓</kbd> 选择 <kbd>Enter</kbd> 打开 <kbd>Esc</kbd> 关闭</span>
        </div>
      </div>
    </div>

    <!-- 版本历史弹窗 -->
    <div v-if="changelogOpen && changelogTarget" class="modal-mask" @click.self="changelogOpen=false">
      <div class="modal card modal-changelog">
        <div class="modal-head">
          <h2>{{ changelogTarget.name }} · 版本历史</h2>
          <span class="close" @click="changelogOpen=false"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span>
        </div>
        <div class="modal-body">
          <div class="changelog-header">
            <div class="changelog-plugin-info">
              <img :src="changelogTarget.icon || logo" class="changelog-icon" :class="{ 'changelog-icon-fallback': !changelogTarget.icon }" alt="" />
              <div>
                <div class="changelog-plugin-name">{{ changelogTarget.name }}</div>
                <div v-if="changelogTarget.version" class="changelog-plugin-version">v{{ changelogTarget.version }}</div>
              </div>
            </div>
          </div>
          <div class="changelog-content">
            <h3>更新日志</h3>
            <pre class="changelog-text">{{ changelogTarget.changelog }}</pre>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn btn-primary" @click="changelogOpen=false">关闭</button>
        </div>
      </div>
    </div>

    <!-- 配置弹窗 -->
    <div v-if="configOpen" class="modal-mask" @click.self="configOpen=false">
      <div class="modal card" :class="{ 'modal-wide': configRenderMode === 'vue' || Object.keys(configSchema).length }">
        <div class="modal-head">
          <h2>{{ configTarget?.name }} · 配置</h2>
          <span class="close" @click="configOpen=false"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span>
        </div>
        <RemotePluginConfig v-if="configRenderMode === 'vue'"
                    :key="configTarget?.id"
                    :plugin-id="configTarget?.id" :has-frontend="configHasFrontend" />
        <ConfigForm v-else-if="Object.keys(configSchema).length" ref="configFormRef"
                    v-model="configValues" :schema="configSchema" :plugin-id="configTarget?.id" />
        <div v-else class="muted center" style="padding:24px">这个插件没有可配置项。</div>

        <div class="config-routing-box">
          <div>
            <div class="config-routing-title">通知渠道</div>
            <div class="hint muted small">选择这个插件的通知发到哪些渠道，可多选，切换后立即生效。</div>
          </div>
          <div v-if="configBotLoading" class="muted small">正在读取…</div>
          <div v-else-if="!configBotReady" class="muted small">加载失败</div>
          <div v-else class="config-bot-checks">
            <!-- 默认选项 -->
            <label class="config-bot-item">
              <input type="checkbox"
                     :checked="configBotChoice.length === 0"
                     @change="configBotChoice = []; saveConfigBot()"
                     :disabled="configBotSaving" />
              <span>默认（{{ configDefaultBotName() }}）</span>
            </label>
            <!-- 所有可用渠道 -->
            <label v-for="bot in configBots.filter(b => !b.is_default)" :key="bot.id" class="config-bot-item">
              <input type="checkbox"
                     :checked="configBotChoice.includes(bot.id)"
                     @change="toggleConfigBot(bot.id)"
                     :disabled="configBotSaving" />
              <span>{{ bot.name || bot.id }}
                <span class="muted small">{{ bot.username ? bot.username : '' }}</span>
                <span v-if="!bot.online" class="muted small">（离线）</span>
              </span>
            </label>
          </div>
        </div>

        <!-- Webhook（仅插件声明 "webhook": True 时显示） -->
        <div v-if="configTarget?.webhook" class="webhook-box">
          <div class="webhook-title">Webhook 入站地址</div>
          <div class="hint muted small">
            外部服务可 POST 到此地址触发本插件。密钥与平台统一（在「系统设置 → 通知」生成），
            所有插件共用；需插件已启用并实现了处理器才会真正响应。
          </div>
          <template v-if="webhookSecret">
            <div class="webhook-url mono">{{ webhookUrl }}</div>
            <div class="webhook-actions">
              <button class="btn sm" @click="copyWebhookUrl">复制地址</button>
            </div>
          </template>
          <div v-else class="muted small">
            尚未设置 Webhook 密钥。请先到「系统设置 → 通知 → 平台 Webhook」生成密钥。
          </div>
        </div>
        <!-- vue 模式由插件组件自己管保存，右上角已有 × 关闭，底部不再重复关闭按钮；schema 模式给平台保存按钮 -->
        <div v-if="configRenderMode !== 'vue'" class="modal-foot">
          <button class="btn" @click="configOpen=false">取消</button>
          <button class="btn btn-primary" @click="saveConfig" :disabled="configSaving || !Object.keys(configSchema).length">
            {{ configSaving ? '保存中…' : '保存并应用' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 应用账号弹窗 -->
    <div v-if="acctOpen" class="modal-mask" @click.self="acctOpen=false">
      <div class="modal card">
        <div class="modal-head">
          <h2>{{ acctTarget?.name }} · 应用账号</h2>
          <span class="close" @click="acctOpen=false"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span>
        </div>
        <div class="form">
          <div class="hint muted">选择这个插件在哪些账号上生效。多账号时可让不同号开不同插件。</div>
          <label class="acct-row">
            <input type="radio" :checked="acctAllMode" @change="acctAllMode = true" />
            <span>全部账号（默认）</span>
          </label>
          <label class="acct-row">
            <input type="radio" :checked="!acctAllMode" @change="acctAllMode = false" />
            <span>仅指定账号</span>
          </label>
          <div v-if="!acctAllMode" class="acct-list">
            <div v-if="acctOptions.length === 0" class="muted small">还没有账号，去「账号管理」登录。</div>
            <label v-for="a in acctOptions" :key="a.session" class="acct-item">
              <input type="checkbox" :checked="acctSelected.includes(a.session)" @change="toggleAcct(a.session)" />
              <span>{{ a.name }}</span>
              <span class="muted mono small">{{ a.session }}</span>
            </label>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="acctOpen=false">取消</button>
          <button class="btn btn-primary" @click="saveAccounts" :disabled="acctSaving">
            {{ acctSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 插件日志弹窗（只看当前插件） -->
    <div v-if="logsOpen" class="modal-mask" @click.self="closeLogs">
      <div class="modal card logs-modal">
        <div class="modal-head">
          <h2>
            {{ logsTarget?.name }} · 日志
            <span class="conn" :class="{ on: logsConnected }"><span class="dot"></span>{{ logsConnected ? '实时' : '连接中' }}</span>
          </h2>
          <span class="close" @click="closeLogs"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span>
        </div>
        <div class="log-box" ref="logsBox">
          <div v-if="logsList.length === 0" class="muted center">该插件暂无日志</div>
          <div v-for="(l, i) in logsList" :key="i" class="log-line">
            <span class="time">{{ pluginLogTime(l) }}</span>
            <span class="level" :class="logLevelClass(l.level)">{{ l.level }}</span>
            <span class="msg">{{ l.msg }}</span>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="logsList = []">清空</button>
          <button class="btn" @click="closeLogs">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="repoOpen" class="modal-mask" @click.self="repoOpen=false">
      <div class="modal card">
        <div class="modal-head">
          <h2>设置 GitHub 仓库地址</h2>
          <span class="close" @click="repoOpen=false"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span>
        </div>
        <div v-if="repoErr" class="alert">{{ repoErr }}</div>
        <div class="form">
          <div class="muted small">官方仓库已内置，无需添加。这里配置你自己的额外仓库。</div>
          <div class="field">
            <label>额外公开插件仓库（可加多个）</label>
            <div v-for="(r, i) in repoList" :key="i" class="repo-row">
              <input class="input" v-model="r.url"
                     placeholder="例如 AWdress/AWBotNest-Plugins"
                     @blur="normalizeRepoRow(r)" />
              <button class="btn sm btn-danger" @click="delRepo(i)"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
            </div>
            <button class="btn sm" @click="addRepo">+ 添加仓库</button>
            <div class="hint muted">仓库名必须是 AWBotNest-Plugins，例如 AWdress/AWBotNest-Plugins。</div>
            <div class="hint muted">推荐仓库带 manifest.json 并写好 version，平台才能识别「更新」。</div>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="repoOpen=false">取消</button>
          <button class="btn btn-primary" @click="saveRepos" :disabled="repoSaving">
            {{ repoSaving ? '保存中…' : '保存并刷新市场' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.plugins { position: relative; min-height: 100%; }

.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; gap: 16px; }
.plugin-controls {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin: -8px 0 20px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
}
.filter-pills, .search-filters { display: flex; align-items: center; gap: 8px; }
.filter-pills button, .search-filters button {
  border: 1px solid transparent; border-radius: 999px; padding: 6px 12px;
  color: var(--text-muted); background: transparent; font: inherit; font-size: 11px; font-weight: 650; cursor: pointer;
  transition: all 0.2s ease;
}
.filter-pills button:hover, .search-filters button:hover {
  color: var(--text-primary); background: var(--bg-hover);
  transform: translateY(-1px);
}
.filter-pills button:active, .search-filters button:active {
  transform: translateY(0) scale(0.98);
}
.filter-pills button.active, .search-filters button.active {
  color: var(--accent); border-color: rgba(48,128,240,.22); background: var(--accent-dim);
}
.filter-pills button.danger.active {
  color: var(--danger); border-color: var(--danger-dim); background: var(--danger-dim);
}
.control-caption { color: var(--text-muted); font-size: 12px; }

/* Tab 切换 */
.tabs { display: flex; gap: 4px; background: var(--bg-elevated); padding: 4px; border-radius: 10px; }
.tab {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 18px; border: none; background: transparent; cursor: pointer;
  color: var(--text-secondary); font-size: 14px; font-weight: 600; border-radius: 7px;
  transition: all 0.15s;
}
.tab:hover { color: var(--text-primary); }
.tab.active { background: var(--bg-card); color: var(--text-primary); box-shadow: 0 1px 3px rgba(0,0,0,0.25); }
.tab-count {
  font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 10px;
  background: var(--bg-elevated); color: var(--text-muted);
}
.tab.active .tab-count { background: var(--accent-dim); color: var(--accent); }

.stats { color: var(--text-secondary); font-size: 13px; }
.stats b { color: var(--text-primary); }
.small { font-size: 12px; }

.alert {
  background: var(--danger-dim); color: var(--danger);
  padding: 12px 16px; border-radius: var(--radius-sm);
  margin-bottom: 16px; font-size: 13px;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  border-left: 3px solid var(--danger);
}
.alert::before {
  content: '';
  display: inline-block;
  width: 18px; height: 18px; flex-shrink: 0;
  background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="%23ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>') no-repeat center;
  background-size: contain;
}
.alert .close { cursor: pointer; font-size: 16px; display: inline-flex; align-items: center; transition: transform 0.2s; }
.alert .close:hover { transform: scale(1.1); }
.x-ico { width: 16px; height: 16px; }

.store-hint { font-size: 12px; margin-bottom: 16px; }
.center { text-align: center; padding: 40px; }
.empty { text-align: center; padding: 48px; }
.empty p { margin-bottom: 8px; }
.filter-empty .btn { margin-top: 8px; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 380px));
  gap: 18px;
}
.plugin-card {
  min-height: 180px; display: flex; flex-direction: column; gap: 14px;
  transition: border-color 0.2s ease, transform 0.2s ease, box-shadow 0.2s ease;
}
.plugin-card:hover {
  border-color: var(--border-light);
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  transform: translateY(-2px);
}
.plugin-card.clickable { cursor: pointer; }
.plugin-card.clickable:hover {
  border-color: rgba(48,128,240,.38);
  box-shadow: 0 8px 24px rgba(48,128,240,0.2), 0 2px 8px rgba(0,0,0,.3);
  transform: translateY(-3px);
}
.plugin-card.clickable:active { transform: translateY(-1px) scale(0.995); }
.plugin-card.menu-open { position: relative; z-index: 50; }
.plugin-card.err {
  border-color: var(--danger-dim);
  border-left: 3px solid var(--danger);
}

.kebab-wrap { margin-left: auto; position: relative; }
.kebab {
  display: flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; padding: 0;
  border: none; background: transparent; cursor: pointer; border-radius: 8px;
  transition: background 0.2s ease, transform 0.2s ease;
}
.kebab:hover, .kebab.active {
  background: var(--bg-elevated);
  transform: scale(1.05);
}
.kebab:active {
  transform: scale(0.95);
}
.kebab-ico {
  width: 18px; height: 18px;
  stroke: var(--text-muted);
  transition: stroke 0.2s ease;
}
.kebab:hover .kebab-ico, .kebab.active .kebab-ico {
  stroke: var(--text-primary);
}

/* 三点下拉菜单 */
.dropdown {
  position: absolute; left: 0; top: calc(100% + 6px);
  min-width: 150px; z-index: 160;
  background: var(--bg-card); border: 1px solid var(--border-light);
  border-radius: var(--radius-sm); padding: 5px;
  box-shadow: 0 8px 28px rgba(0,0,0,0.45);
  display: flex; flex-direction: column; gap: 1px;
  animation: dropdown-slide-in 0.2s ease both;
  transform-origin: top;
}
@keyframes dropdown-slide-in {
  from {
    opacity: 0;
    transform: translateY(-8px) scale(0.95);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}
.dropdown.align-right { left: auto; right: 0; }
.menu-item {
  display: flex; align-items: center; gap: 9px;
  width: 100%; padding: 9px 11px; border: none; background: transparent;
  color: var(--text-primary); font-size: 13px; text-align: left; cursor: pointer;
  border-radius: 6px; transition: background 0.15s ease, transform 0.1s ease;
}
.menu-item:hover:not(:disabled) {
  background: var(--bg-elevated);
  transform: translateX(2px);
}
.menu-item:disabled {
  opacity: 0.5; cursor: not-allowed;
}
.menu-item.danger { color: var(--danger); }
.menu-item.danger:hover:not(:disabled) { background: var(--danger-dim); }
.mi-ico { width: 15px; height: 15px; flex-shrink: 0; }
.menu-sep { height: 1px; background: var(--border); margin: 4px 2px; }

.card-head { display: flex; align-items: flex-start; justify-content: space-between; }
.card-title { display: flex; flex-direction: column; gap: 6px; min-width: 0; flex: 1; }
.card-title > .badge { align-self: flex-start; }
.name { font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; min-width: 0; }
/* 名字太长单行截断成省略号，把有限宽度让给右侧「官方 / Vue」徽章，保持一行对齐 */
.name-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

.badge-official {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 10px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff; letter-spacing: 0.5px;
  flex-shrink: 0; white-space: nowrap;
}
.badge-official::before {
  content: '';
  display: inline-block;
  width: 12px; height: 12px;
  background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/><path d="m9 12 2 2 4-4"/></svg>') no-repeat center;
  background-size: contain;
}

/* vue 插件：标明该插件配置界面是插件自带的自定义界面，跟「官方」徽章同款圆角小标 */
.badge-vue {
  font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 10px;
  background: var(--accent-2); color: #fff; letter-spacing: 0.5px;
  flex-shrink: 0; white-space: nowrap;
}

.store-title { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
.store-icon { width: 38px; height: 38px; border-radius: 12px; object-fit: contain; flex-shrink: 0; }
.store-icon-fallback {
  object-fit: contain;
  padding: 6px;
  background-color: var(--bg-elevated);
}
.store-card:hover {
  border-color: var(--accent-dim);
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  transform: translateY(-2px);
}
.store-card.has-update {
  border-color: var(--accent-dim);
  background: rgba(255, 193, 7, 0.03);
}
.update-section {
  margin-bottom: 24px;
  padding: 16px;
  background: rgba(255, 193, 7, 0.05);
  border-radius: var(--radius-sm);
  border-left: 3px solid #ffc107;
}
.section-label {
  font-size: 14px; font-weight: 600; color: var(--text-secondary);
  margin: 0 0 12px; letter-spacing: .3px;
}
.badge-update {
  font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px;
  background: var(--accent-dim); color: var(--accent);
  animation: pulse-badge 2s ease-in-out infinite;
}
@keyframes pulse-badge {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

.desc {
  color: var(--text-secondary); font-size: 13px; min-height: 40px; line-height: 1.55;
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden;
}
.err-msg {
  background: var(--danger-dim); color: var(--danger);
  padding: 10px 12px; border-radius: var(--radius-sm);
  font-size: 12px; font-family: monospace;
  border-left: 3px solid var(--danger);
  display: flex; align-items: flex-start; gap: 8px;
}
.err-msg::before {
  content: '';
  display: inline-block;
  width: 16px; height: 16px; flex-shrink: 0;
  background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="%23ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>') no-repeat center;
  background-size: contain;
}

.card-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: auto; }
.meta-item {
  font-size: 11px; color: var(--text-muted);
  background: var(--bg-elevated); padding: 2px 8px; border-radius: 4px;
}

.card-actions { display: flex; gap: 8px; margin-top: 4px; }
.btn.sm {
  padding: 7px 14px; font-size: 12px;
  transition: all 0.2s ease;
}
.btn.sm:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0,0,0,0.15);
}
.btn.sm:active {
  transform: translateY(0) scale(0.98);
}
.btn-ico { width: 14px; height: 14px; flex-shrink: 0; }

.drag-overlay {
  position: fixed; inset: 0;
  background: rgba(48, 128, 240, 0.12);
  border: 2px dashed var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; color: var(--accent);
  z-index: 100; pointer-events: none;
}

/* 插件搜索：右下角入口 + 居中命令面板 */
.plugin-search-fab {
  position: fixed; right: 28px; bottom: 28px; z-index: 120;
  width: 56px; height: 56px; border: 1px solid rgba(255,255,255,0.14); border-radius: 18px;
  display: grid; place-items: center; cursor: pointer;
  color: #fff; background: linear-gradient(145deg, #3a8cff, #1767d8);
  box-shadow: 0 12px 32px rgba(20, 93, 194, 0.42), inset 0 1px 0 rgba(255,255,255,0.18);
  transition: transform .18s ease, box-shadow .18s ease, border-radius .18s ease;
  animation: search-fab-in .35s ease both;
}
.plugin-search-fab::after {
  content: '搜索插件 (Ctrl+K)';
  position: absolute;
  right: 100%;
  margin-right: 12px;
  padding: 8px 12px;
  background: var(--bg-card);
  color: var(--text-primary);
  font-size: 12px;
  border-radius: 8px;
  border: 1px solid var(--border-light);
  box-shadow: 0 4px 12px rgba(0,0,0,0.25);
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}
.plugin-search-fab:hover::after {
  opacity: 1;
}
.plugin-search-fab:hover {
  transform: translateY(-3px); border-radius: 15px;
  box-shadow: 0 16px 38px rgba(20, 93, 194, 0.52), inset 0 1px 0 rgba(255,255,255,0.2);
}
.plugin-search-fab:active { transform: translateY(0) scale(.96); }
.plugin-search-fab:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.plugin-search-fab svg { width: 24px; height: 24px; }
@keyframes search-fab-in { from { opacity: 0; transform: translateY(12px) scale(.88); } }

.search-mask { backdrop-filter: blur(4px); background: rgba(3, 6, 12, 0.74); }
.search-modal {
  width: 720px; max-width: calc(100vw - 40px); max-height: min(760px, 82vh); padding: 0;
  display: flex; flex-direction: column; overflow: hidden;
  border-color: var(--border-light); background: #151820;
  box-shadow: 0 26px 80px rgba(0,0,0,.58);
  animation: search-panel-in .2s ease both;
}
@keyframes search-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } }
.search-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 20px 12px;
}
.search-title { display: flex; align-items: center; gap: 9px; font-size: 15px; font-weight: 650; }
.search-title svg { width: 19px; height: 19px; color: var(--accent); }
.search-close {
  width: 32px; height: 32px; border: 0; border-radius: 8px; display: grid; place-items: center;
  color: var(--text-muted); background: transparent; cursor: pointer;
}
.search-close:hover { color: var(--text-primary); background: var(--bg-hover); }
.search-close .x-ico { width: 19px; height: 19px; }
.search-input-wrap {
  margin: 0 20px 12px; min-height: 48px; display: flex; align-items: center; gap: 10px;
  padding: 0 14px; border: 1px solid var(--border-light); border-radius: 12px;
  color: var(--text-muted); background: #0d1017;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.search-input-wrap:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.search-input-wrap > svg { width: 19px; height: 19px; flex: 0 0 auto; }
.search-input-wrap input {
  flex: 1; min-width: 0; border: 0; outline: 0; background: transparent;
  color: var(--text-primary); font: inherit; font-size: 14px;
}
.search-input-wrap input::placeholder { color: var(--text-muted); }
.search-input-wrap input::-webkit-search-cancel-button { display: none; }
.search-count {
  min-width: 26px; padding: 2px 7px; border-radius: 8px; text-align: center;
  color: var(--text-secondary); background: var(--bg-elevated); font-size: 11px;
}
.search-filters {
  flex: 0 0 auto;
  min-height: 32px;
  margin: 0 20px 10px;
  padding: 1px 0 3px;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none;
}
.search-filters::-webkit-scrollbar { display: none; }
.search-filters button { flex: 0 0 auto; }

.search-sort-section {
  margin: 0 20px 12px;
  padding: 12px 14px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-light);
  border-radius: 10px;
}
.search-sort-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 8px;
}
.search-sort-options {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.search-sort-options button {
  padding: 8px 12px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.search-sort-options button:hover {
  background: var(--bg-hover);
}
.search-sort-options button.active {
  background: var(--accent);
  color: white;
  font-weight: 600;
}

.search-filter-section {
  margin: 0 20px 12px;
  padding: 12px 14px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-light);
  border-radius: 10px;
}
.search-filter-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 8px;
}
.search-filter-dropdowns {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.search-select {
  width: 100%;
  padding: 8px 32px 8px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-card) url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="%23999" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>') no-repeat right 8px center / 16px;
  color: var(--text-primary);
  font-size: 13px;
  cursor: pointer;
  appearance: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.search-select:hover {
  border-color: var(--accent-dim);
}
.search-select:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}

.search-list { flex: 1; min-height: 160px; overflow-y: auto; padding: 2px 10px 8px; }
.search-item {
  display: grid; grid-template-columns: 46px minmax(0, 1fr) auto; align-items: center; gap: 12px;
  padding: 11px 12px; border: 1px solid transparent; border-radius: 11px;
  transition: background .14s ease, border-color .14s ease;
}
.search-item:hover { background: rgba(255,255,255,.035); border-color: var(--border); }
.search-item.active { background: var(--accent-dim); border-color: rgba(48,128,240,.24); }
.search-item.actionable { cursor: pointer; }
.search-icon {
  width: 42px; height: 42px; border-radius: 12px; object-fit: contain;
  background: var(--bg-elevated); border: 1px solid var(--border);
}
.search-icon.fallback { object-fit: contain; padding: 7px; }
.search-info { min-width: 0; }
.search-name-row { display: flex; align-items: center; gap: 7px; min-width: 0; }
.search-name { font-size: 14px; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-version { color: var(--text-muted); font-size: 11px; flex: 0 0 auto; }
.search-state { padding: 1px 7px; border-radius: 999px; font-size: 10px; font-weight: 650; flex: 0 0 auto; }
.search-state.installed { color: var(--accent-2); background: var(--accent-2-dim); }
.search-state.available { color: var(--accent); background: var(--accent-dim); }
.search-state.update { color: var(--warning); background: rgba(224,160,32,.14); }
.search-state.error { color: var(--danger); background: var(--danger-dim); }
.search-desc {
  margin-top: 3px; color: var(--text-secondary); font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.search-meta { display: flex; gap: 8px; margin-top: 4px; color: var(--text-muted); font-size: 10px; }
.search-meta span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-action {
  min-width: 62px; padding: 7px 11px; border: 1px solid var(--border-light); border-radius: 8px;
  color: var(--text-secondary); background: var(--bg-elevated); font-size: 12px; cursor: pointer;
}
.search-action:hover { color: #fff; border-color: var(--accent); background: var(--accent); }
.search-action:disabled { opacity: .5; cursor: wait; }
.search-empty { padding: 56px 20px; text-align: center; color: var(--text-muted); }
.search-foot {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 20px; border-top: 1px solid var(--border);
  color: var(--text-muted); background: rgba(0,0,0,.12); font-size: 11px;
}
.search-foot kbd {
  padding: 2px 6px; border: 1px solid var(--border-light); border-radius: 5px;
  color: var(--text-secondary); background: var(--bg-elevated); font-family: inherit;
}

.modal-mask {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
.modal { --modal-pad: var(--gap-lg); width: 540px; max-width: 90vw; max-height: 85vh; overflow-y: auto; box-shadow: var(--shadow-float); }
/* 配置弹窗（vue 模式 + schema 模式）：参考 MoviePilot 给一块大而响应式的画布，
   vue 由插件自己布局，schema 由平台表单栅格铺开
   （用固定大宽度而非 fit-content：vue 插件多用 100%/栅格布局，fit-content 会坍缩） */
.modal-wide { width: 1000px; max-width: 92vw; }
.modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.modal-head h2 { font-size: 16px; }
.modal-head .close { cursor: pointer; font-size: 22px; color: var(--text-muted); display: inline-flex; align-items: center; }
.modal-head .close .x-ico { width: 20px; height: 20px; }
.modal-foot {
  position: sticky; bottom: calc(0px - var(--modal-pad)); z-index: 2;
  display: flex; justify-content: flex-end; gap: 10px;
  margin: 24px calc(0px - var(--modal-pad)) calc(0px - var(--modal-pad)); padding: 14px var(--modal-pad);
  border-top: 1px solid var(--border); background: rgba(17,19,26,.95); backdrop-filter: blur(16px);
}
.form { display: flex; flex-direction: column; gap: 16px; }
.form .field { display: flex; flex-direction: column; gap: 8px; }
.form .field label { font-size: 13px; color: var(--text-secondary); }
.row.between { display: flex; align-items: center; justify-content: space-between; }
.hint { font-size: 12px; }
.config-routing-box {
  display: flex; flex-direction: column; gap: 10px;
  margin-top: 18px; padding: 14px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--bg-elevated);
}
.config-routing-title { margin-bottom: 4px; font-size: 13px; font-weight: 600; color: var(--text-primary); }
.config-bot-checks { display: flex; flex-direction: column; gap: 8px; }
.config-bot-item {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--text-primary); cursor: pointer;
  padding: 6px 8px; border-radius: 6px;
  transition: background 0.15s;
}
.config-bot-item:hover { background: var(--bg-hover); }
.config-bot-item input[type="checkbox"] { width: 15px; height: 15px; cursor: pointer; flex-shrink: 0; }
.repo-row { display: flex; gap: 8px; margin-bottom: 8px; }
.repo-row .input { flex: 1; }
.acct-row { display: flex; align-items: center; gap: 8px; font-size: 14px; cursor: pointer; }
.acct-list { display: flex; flex-direction: column; gap: 8px; max-height: 240px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px; }
.acct-item { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }
.acct-item .small { margin-left: auto; }

/* Webhook 区（配置弹窗内） */
.webhook-box {
  margin-top: 18px; padding: 14px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--bg-elevated);
  display: flex; flex-direction: column; gap: 10px;
}
.webhook-title { font-size: 13px; font-weight: 600; color: var(--accent); }
.webhook-url {
  font-size: 12px; word-break: break-all; padding: 8px 10px;
  background: #07090f; border-radius: var(--radius-sm); color: var(--text-primary);
}
.webhook-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }
.btn.sm.danger { color: var(--danger); }

/* 插件日志弹窗 */
.logs-modal { width: 720px; display: flex; flex-direction: column; }
.logs-modal .modal-head h2 { display: flex; align-items: center; gap: 12px; }
.conn { display: flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 500; color: var(--text-muted); }
.conn .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); }
.conn.on { color: var(--accent-2); }
.conn.on .dot { background: var(--accent-2); box-shadow: 0 0 7px var(--accent-2); }
.logs-modal .log-box {
  flex: 1; min-height: 280px; max-height: 56vh; overflow-y: auto;
  padding: 12px 14px; border-radius: var(--radius-sm); background: #07090f;
  font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; line-height: 1.65;
}
.logs-modal .log-line { display: flex; gap: 10px; white-space: pre-wrap; word-break: break-all; }
.logs-modal .log-line:hover { background: rgba(255,255,255,0.03); }
.logs-modal .time { color: var(--text-muted); flex-shrink: 0; }

/* Changelog 弹窗 */
.modal-changelog { width: 600px; max-width: 90vw; }
.changelog-header { padding-bottom: 16px; border-bottom: 1px solid var(--border); }
.changelog-plugin-info { display: flex; align-items: center; gap: 12px; }
.changelog-icon { width: 48px; height: 48px; border-radius: 10px; object-fit: contain; }
.changelog-icon-fallback { filter: brightness(0.7); }
.changelog-plugin-name { font-size: 16px; font-weight: 600; color: var(--text-primary); }
.changelog-plugin-version { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
.changelog-content { margin-top: 16px; }
.changelog-content h3 { font-size: 14px; font-weight: 600; color: var(--accent); margin-bottom: 12px; }
.changelog-text {
  white-space: pre-wrap; word-break: break-word; line-height: 1.7;
  font-family: 'SFMono-Regular', Consolas, monospace; font-size: 13px;
  color: var(--text-secondary); background: var(--bg-elevated);
  padding: 14px; border-radius: var(--radius-sm); max-height: 400px; overflow-y: auto;
}

/* 弹窗基础样式 */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  padding: 20px;
}

.modal-box {
  background: var(--bg-card);
  border-radius: var(--radius);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 24px;
  border-bottom: 1px solid var(--border);
}

.modal-head h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.close-btn {
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}

.close-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.close-btn svg {
  width: 18px;
  height: 18px;
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding: 16px 24px;
  border-top: 1px solid var(--border);
}

/* 筛选下拉菜单 */
.filter-dropdown-wrapper {
  position: relative;
  display: inline-block;
}

.filter-dropdown {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 1000;
  background: var(--bg-surface); /* 确保有背景色 */
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
  min-width: 300px;
  max-width: 400px;
  padding: 16px;

  /* 自适应位置：如果右侧空间不足，改为左对齐 */
  left: auto;
}

/* 当下拉菜单超出右边界时，改为右对齐 */
@media (max-width: 768px) {
  .filter-dropdown {
    right: 0;
    left: auto;
    min-width: 280px;
  }
}

.filter-section {
  margin-bottom: 16px;
}

.filter-section:last-child {
  margin-bottom: 0;
}

.filter-label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.filter-options {
  display: flex;
  flex-direction: column; /* 改为竖向排列 */
  gap: 8px;
}

.filter-options button {
  padding: 8px 12px;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.2s;
  text-align: left; /* 文字左对齐 */
}

.filter-options button:hover {
  background: var(--bg-active);
  border-color: var(--accent);
}

.filter-options button.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.filter-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.btn-sm {
  padding: 6px 12px;
  font-size: 13px;
}

/* 筛选弹窗（模态框样式，保留兼容性） */
.filter-modal {
  width: 500px;
  max-width: 90vw;
}

.filter-section {
  margin-bottom: 20px;
}

.filter-label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 10px;
}

.filter-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}

.filter-options button {
  padding: 8px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}

.filter-options button:hover {
  border-color: var(--accent-dim);
  background: var(--bg-hover);
  color: var(--text-primary);
}

.filter-options button.active {
  border-color: var(--accent);
  background: var(--accent-dim);
  color: var(--accent);
  font-weight: 600;
}

.logs-modal .level { flex-shrink: 0; width: 60px; font-weight: 600; }
.logs-modal .msg { color: var(--text-primary); }
.logs-modal .center { padding: 40px; }
.lv-debug { color: var(--text-muted); }
.lv-info { color: var(--accent); }
.lv-warn { color: var(--warning); }
.lv-err { color: var(--danger); }

/* 键盘导航与可访问性 */
button:focus-visible, .btn:focus-visible, .tab:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: inherit;
}
.plugin-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

/* 手机适配 */
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; gap: 12px; }
  .toolbar > .row { flex-wrap: wrap; }
  .grid {
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 14px;
  }
  .plugin-search-fab {
    right: 20px;
    bottom: 20px;
    width: 52px;
    height: 52px;
  }
  .plugin-search-fab::after {
    display: none;
  }
  .modal, .modal-wide, .modal-changelog, .logs-modal {
    width: 95vw !important;
    max-width: 95vw !important;
  }
  .tabs { width: 100%; }
  .tab { flex: 1; justify-content: center; padding: 9px 8px; }
  .plugin-controls { align-items: flex-start; margin-top: -4px; overflow: hidden; }
  .filter-pills { max-width: calc(100vw - 118px); overflow-x: auto; padding-bottom: 3px; }
  .filter-pills button { flex: 0 0 auto; }
  .control-caption { display: none; }
  .grid { grid-template-columns: 1fr; }
  .grid.compact { grid-template-columns: 1fr; }
  .plugin-card, .grid.compact .plugin-card { min-height: 0; padding: 15px; }
  .repo-row { flex-wrap: wrap; }
  .modal { --modal-pad: 14px; }
  .plugin-search-fab { right: 16px; bottom: 82px; width: 52px; height: 52px; border-radius: 16px; }
  .search-modal {
    width: 100vw; max-width: 100vw; height: 100dvh; max-height: 100dvh;
    border: 0; border-radius: 0;
  }
  .search-head { padding: 15px 16px 10px; }
  .search-input-wrap { margin: 0 16px 10px; }
  .search-filters { margin: 0 16px 9px; }
  .search-list { padding: 2px 6px 8px; }
  .search-item { grid-template-columns: 42px minmax(0, 1fr) auto; gap: 10px; padding: 10px; }
  .search-icon { width: 38px; height: 38px; border-radius: 10px; }
  .search-action { min-width: 52px; padding: 7px 9px; }
  .search-meta span:nth-child(n+3) { display: none; }
  .search-foot { padding: 10px 16px; }
  .search-foot > span:first-child { display: none; }
  .config-routing-box { align-items: stretch; flex-direction: column; gap: 10px; }
  .config-bot-select { width: 100%; }
  /* 窄屏照 MoviePilot 直接铺满视口（fullscreen）。
     用 .modal.modal-wide 提特异性 + !important，压过 tokens.css 全局的 .modal.card{width:94vw!important} */
  .modal.modal-wide {
    width: 100vw !important; max-width: 100vw !important;
    height: 100dvh !important; max-height: 100dvh !important;
    border-radius: 0;
  }
}
</style>
