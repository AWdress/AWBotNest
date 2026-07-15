<script setup>
import { ref, onMounted, onUnmounted, computed, nextTick } from 'vue'
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
  } catch (e) {
    error.value = e.message
  }
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
        if (logsList.value.length > 500) logsList.value.splice(0, logsList.value.length - 500)
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

// ── 插件市场（多仓库聚合） ──
const store = ref([])
const officialIds = ref([])
const storeBusy = ref(false)
const storeErr = ref('')
const storeLastSync = ref(null)
const dlBusy = ref({})

const storeAvailable = computed(() => store.value.filter((p) => !p.installed))
// 已安装但仓库有新版本的插件：仅当平台记录过下载版本(local_version)且与远端不同才提示，
// 本地上传/手动导入(无 local_version)不误报更新，避免静默覆盖本地改动。
function hasUpdate(p) {
  return p.installed && p.from_manifest && p.local_version && p.version && p.local_version !== p.version
}
const storeUpdatable = computed(() => store.value.filter(hasUpdate))
const officialSet = computed(() => new Set(officialIds.value))
function isOfficial(p) { return p.official || officialSet.value.has(p.id) }

// 仓库来源只显示 owner/repo（去掉 https://github.com/、.git、/tree/分支/子目录 等）
function shortRepo(url) {
  if (!url) return ''
  let s = String(url).trim()
  s = s.replace(/^https?:\/\/(www\.)?github\.com\//i, '')  // 去协议+域名
  s = s.replace(/^https?:\/\/[^/]+\//i, '')                 // 其它主机也去掉
  s = s.replace(/\.git$/i, '')
  const parts = s.split('/').filter(Boolean)
  return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : s
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
const repoInterval = ref(20)
const repoList = ref([])
const repoSaving = ref(false)
const repoErr = ref('')

async function openRepos() {
  repoErr.value = ''
  try {
    const d = await api.getSettings()
    const s = d.settings || {}
    repoInterval.value = s.PLUGIN_REPO_INTERVAL || 20
    repoList.value = (s.PLUGIN_REPOS || []).map((r) => ({ url: r.url || '', token: r.token || '' }))
    repoOpen.value = true
  } catch (e) {
    repoErr.value = e.message
  }
}

function addRepo() { repoList.value.push({ url: '', token: '' }) }
function delRepo(i) { repoList.value.splice(i, 1) }

async function saveRepos() {
  repoSaving.value = true; repoErr.value = ''
  try {
    const repos = repoList.value
      .map((r) => ({ url: (r.url || '').trim(), token: (r.token || '').trim() }))
      .filter((r) => r.url)
    await api.saveSettings({
      PLUGIN_REPO_ENABLE: true,
      PLUGIN_REPO_INTERVAL: Number(repoInterval.value) || 20,
      PLUGIN_REPOS: repos,
    })
    repoOpen.value = false
    await loadStore(true)
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
})
onUnmounted(() => {
  logsDisconnect()
  document.removeEventListener('click', closeMenu)
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
          <button class="btn" @click="openRepos">设置仓库地址</button>
          <button class="btn btn-primary" @click="loadStore(true)" :disabled="storeBusy">
            {{ storeBusy ? '刷新中…' : '刷新市场' }}
          </button>
        </template>
      </div>
    </div>

    <div v-if="error" class="alert">{{ error }} <span @click="error=''" class="close"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span></div>

    <!-- ════ 我的插件 ════ -->
    <template v-if="tab === 'mine'">
      <div v-if="loading" class="muted center">加载中…</div>
      <div v-else-if="plugins.length === 0" class="empty card">
        <p>还没有插件。</p>
        <p class="muted">去「插件市场」安装，或把 .py 文件拖到这里 / 点「上传插件」。</p>
      </div>
      <div v-else class="grid">
        <div v-for="p in plugins" :key="p.id" class="card plugin-card clickable"
             :class="{ err: p.error, 'menu-open': menuFor === p.id }"
             @click="openConfig(p)">
          <div class="card-head">
            <div class="store-title">
              <img :src="p.icon || logo" class="store-icon" :class="{ 'store-icon-fallback': !p.icon }" alt="" />
              <div class="card-title">
                <span class="name">{{ p.name }}
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
                <span></span><span></span><span></span>
              </button>
              <div v-if="menuFor === p.id" class="dropdown" :class="{ 'align-right': menuAlignRight }" @click.stop>
                <button class="menu-item" @click.stop="openConfig(p)">
                  <svg class="mi-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg> 配置
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
          <div class="grid">
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
                <button class="btn sm btn-primary" @click="download(p)" :disabled="dlBusy[p.id]">
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
        <div v-else-if="storeAvailable.length" class="grid">
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
            <button class="btn sm btn-primary" @click="download(p)" :disabled="dlBusy[p.id]">
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
        <!-- vue 模式由插件组件自己管保存，这里只留关闭；schema 模式给平台保存按钮 -->
        <div class="modal-foot">
          <template v-if="configRenderMode === 'vue'">
            <button class="btn" @click="configOpen=false">关闭</button>
          </template>
          <template v-else>
            <button class="btn" @click="configOpen=false">取消</button>
            <button class="btn btn-primary" @click="saveConfig" :disabled="configSaving || !Object.keys(configSchema).length">
              {{ configSaving ? '保存中…' : '保存并应用' }}
            </button>
          </template>
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
            <span class="time">{{ l.time }}</span>
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
          <div class="muted small">自动轮询已常开：每隔下面的间隔自动刷新市场并更新已安装插件（仍不自动启用）。</div>
          <div class="field">
            <label>轮询间隔（分钟）</label>
            <input class="input" type="number" min="1" v-model.number="repoInterval" style="max-width:160px" />
          </div>
          <div class="field">
            <label>额外插件仓库（可加多个）</label>
            <div v-for="(r, i) in repoList" :key="i" class="repo-row">
              <input class="input" v-model="r.url"
                     placeholder="owner/repo 或 https://github.com/owner/repo（可带 /tree/分支/子目录）" />
              <input class="input repo-token" type="password" v-model="r.token" placeholder="token（私有库才填）" />
              <button class="btn sm btn-danger" @click="delRepo(i)"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
            </div>
            <button class="btn sm" @click="addRepo">+ 添加仓库</button>
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

.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; gap: 16px; }

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
  padding: 10px 14px; border-radius: var(--radius-sm);
  margin-bottom: 16px; font-size: 13px;
  display: flex; justify-content: space-between;
}
.alert .close { cursor: pointer; font-size: 16px; display: inline-flex; align-items: center; }
.x-ico { width: 16px; height: 16px; }

.store-hint { font-size: 12px; margin-bottom: 16px; }
.center { text-align: center; padding: 40px; }
.empty { text-align: center; padding: 48px; }
.empty p { margin-bottom: 8px; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--gap);
}
.plugin-card { display: flex; flex-direction: column; gap: 12px; transition: border-color 0.15s, transform 0.1s; }
.plugin-card:hover { border-color: var(--border-light); }
.plugin-card.clickable { cursor: pointer; }
.plugin-card.clickable:hover { border-color: var(--accent-dim); }
.plugin-card.clickable:active { transform: scale(0.995); }
.plugin-card.menu-open { position: relative; z-index: 50; }
.plugin-card.err { border-color: var(--danger-dim); }

.kebab-wrap { margin-left: auto; position: relative; }
.kebab {
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px;
  width: 30px; height: 30px; padding: 0;
  border: none; background: transparent; cursor: pointer; border-radius: 6px;
  transition: background 0.15s;
}
.kebab:hover, .kebab.active { background: var(--bg-elevated); }
.kebab span { width: 4px; height: 4px; border-radius: 50%; background: var(--text-muted); }
.kebab:hover span, .kebab.active span { background: var(--text-secondary); }

/* 三点下拉菜单 */
.dropdown {
  position: absolute; left: 0; top: calc(100% + 6px);
  min-width: 150px; z-index: 160;
  background: var(--bg-card); border: 1px solid var(--border-light);
  border-radius: var(--radius-sm); padding: 5px;
  box-shadow: 0 8px 28px rgba(0,0,0,0.45);
  display: flex; flex-direction: column; gap: 1px;
}
.dropdown.align-right { left: auto; right: 0; }
.menu-item {
  display: flex; align-items: center; gap: 9px;
  width: 100%; padding: 8px 10px; border: none; background: transparent;
  color: var(--text-primary); font-size: 13px; text-align: left; cursor: pointer;
  border-radius: 6px; transition: background 0.12s;
}
.menu-item:hover:not(:disabled) { background: var(--bg-elevated); }
.menu-item:disabled { opacity: 0.5; cursor: not-allowed; }
.menu-item.danger { color: var(--danger); }
.menu-item.danger:hover:not(:disabled) { background: var(--danger-dim); }
.mi-ico { width: 15px; height: 15px; flex-shrink: 0; }
.menu-sep { height: 1px; background: var(--border); margin: 4px 2px; }

.card-head { display: flex; align-items: flex-start; justify-content: space-between; }
.card-title { display: flex; flex-direction: column; gap: 6px; }
.name { font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; }

.badge-official {
  font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 10px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff; letter-spacing: 0.5px;
}

/* vue 插件：标明该插件配置界面是插件自带的自定义界面，跟「官方」徽章同款圆角小标 */
.badge-vue {
  font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 10px;
  background: var(--accent-2); color: #fff; letter-spacing: 0.5px;
}

.store-title { display: flex; align-items: center; gap: 10px; }
.store-icon { width: 38px; height: 38px; border-radius: 8px; object-fit: cover; flex-shrink: 0; }
.store-icon-fallback { object-fit: contain; padding: 4px; background: var(--bg-elevated); }
.store-card:hover { border-color: var(--accent-dim); }
.store-card.has-update { border-color: var(--accent-dim); }
.update-section { margin-bottom: 20px; }
.section-label {
  font-size: 12px; font-weight: 600; color: var(--text-secondary);
  margin: 0 0 10px; letter-spacing: .3px;
}
.badge-update {
  font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 10px;
  background: var(--accent-dim); color: var(--accent);
}

.desc { color: var(--text-secondary); font-size: 13px; min-height: 38px; }
.err-msg {
  background: var(--danger-dim); color: var(--danger);
  padding: 8px 10px; border-radius: var(--radius-sm);
  font-size: 12px; font-family: monospace;
}

.card-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: auto; }
.meta-item {
  font-size: 11px; color: var(--text-muted);
  background: var(--bg-elevated); padding: 2px 8px; border-radius: 4px;
}

.card-actions { display: flex; gap: 8px; margin-top: 4px; }
.btn.sm { padding: 6px 12px; font-size: 12px; }
.btn-ico { width: 14px; height: 14px; flex-shrink: 0; }

.drag-overlay {
  position: fixed; inset: 0;
  background: rgba(48, 128, 240, 0.12);
  border: 2px dashed var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; color: var(--accent);
  z-index: 100; pointer-events: none;
}

.modal-mask {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
.modal { width: 540px; max-width: 90vw; max-height: 85vh; overflow-y: auto; }
/* 配置弹窗（vue 模式 + schema 模式）：参考 MoviePilot 给一块大而响应式的画布，
   vue 由插件自己布局，schema 由平台表单栅格铺开
   （用固定大宽度而非 fit-content：vue 插件多用 100%/栅格布局，fit-content 会坍缩） */
.modal-wide { width: 1000px; max-width: 92vw; }
.modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.modal-head h2 { font-size: 16px; }
.modal-head .close { cursor: pointer; font-size: 22px; color: var(--text-muted); display: inline-flex; align-items: center; }
.modal-head .close .x-ico { width: 20px; height: 20px; }
.modal-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 24px; }
.form { display: flex; flex-direction: column; gap: 16px; }
.form .field { display: flex; flex-direction: column; gap: 8px; }
.form .field label { font-size: 13px; color: var(--text-secondary); }
.row.between { display: flex; align-items: center; justify-content: space-between; }
.hint { font-size: 12px; }
.repo-row { display: flex; gap: 8px; margin-bottom: 8px; }
.repo-row .input { flex: 1; }
.repo-row .repo-token { max-width: 180px; flex: 0 0 auto; }
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
.logs-modal .level { flex-shrink: 0; width: 60px; font-weight: 600; }
.logs-modal .msg { color: var(--text-primary); }
.logs-modal .center { padding: 40px; }
.lv-debug { color: var(--text-muted); }
.lv-info { color: var(--accent); }
.lv-warn { color: var(--warning); }
.lv-err { color: var(--danger); }

/* 手机适配 */
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; gap: 12px; }
  .tabs { width: 100%; }
  .tab { flex: 1; justify-content: center; padding: 9px 8px; }
  .grid { grid-template-columns: 1fr; }
  .repo-row { flex-wrap: wrap; }
  .repo-row .repo-token { max-width: none; flex: 1 1 100%; }
  /* 窄屏照 MoviePilot 直接铺满视口（fullscreen）。
     用 .modal.modal-wide 提特异性 + !important，压过 tokens.css 全局的 .modal.card{width:94vw!important} */
  .modal.modal-wide {
    width: 100vw !important; max-width: 100vw !important;
    height: 100dvh !important; max-height: 100dvh !important;
    border-radius: 0;
  }
}
</style>
