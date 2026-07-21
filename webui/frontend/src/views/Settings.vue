<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { api } from '../api'
import { toast } from '../composables/toast'
import { confirm } from '../composables/confirm'

const tab = ref('login')   // login | telegram | bots | web | proxy | db | maint

const TABS = [
  { key: 'login',    label: '控制台登录' },
  { key: 'telegram', label: 'Telegram 凭据' },
  { key: 'bots',     label: '通知' },
  { key: 'web',      label: 'Web 控制台' },
  { key: 'proxy',    label: '运行代理' },
  { key: 'db',       label: '数据库' },
  { key: 'maint',    label: '维护' },
]

const s = ref(null)
const loading = ref(true)
const saving = ref(false)
const err = ref('')          // 仅用于加载失败（页面无数据时内联提示）

// 未保存改动检测：快照 vs 当前
const savedSnap = ref('')
const dirty = computed(() => !!s.value && JSON.stringify(s.value) !== savedSnap.value)
// 保存后需重启提示
const restartHint = ref(false)
const restarting = ref(false)
// 用户又开始改动时，隐藏“需重启”横幅（新改动得重新保存）
watch(dirty, (d) => { if (d) restartHint.value = false })

async function load(silent = false) {
  if (!silent) loading.value = true
  err.value = ''
  try {
    const d = await api.getSettings()
    s.value = d.settings
    // 保证嵌套结构存在
    s.value.proxy_set = s.value.proxy_set || { proxy_enable: false, proxy: {}, PROXY_URL: '' }
    s.value.proxy_set.proxy = s.value.proxy_set.proxy || {}
    if (s.value.PIP_INDEX_URL === undefined) s.value.PIP_INDEX_URL = ''
    s.value.DB_INFO = s.value.DB_INFO || {}
    s.value.ACCOUNTS = s.value.ACCOUNTS || []
    s.value.BOTS = Array.isArray(s.value.BOTS) ? s.value.BOTS : []
    if (s.value.WEBHOOK_SECRET === undefined) s.value.WEBHOOK_SECRET = ''
    if (s.value.DEFAULT_BOT_CHAT_ID === undefined) s.value.DEFAULT_BOT_CHAT_ID = ''
    if (s.value.BOT_NAME === undefined) s.value.BOT_NAME = '默认 Bot'
    if (s.value.DEFAULT_BOT_ID === undefined) s.value.DEFAULT_BOT_ID = 'default'
    // 初始化通知渠道配置（数组格式）
    s.value.NOTIFICATION_CHANNELS = Array.isArray(s.value.NOTIFICATION_CHANNELS) ? s.value.NOTIFICATION_CHANNELS : []
    s.value.LOG_CLEANER = s.value.LOG_CLEANER || { enabled: true, keep_lines: 100, hour: 3, minute: 0 }
    // 补齐旧数据里额外 Bot 缺失的 chat_id 字段，保证 v-model 响应
    s.value.BOTS.forEach((b) => { if (b.chat_id === undefined) b.chat_id = '' })
    savedSnap.value = JSON.stringify(s.value)   // 基线快照
  } catch (e) { err.value = e.message } finally { if (!silent) loading.value = false }
}

async function save() {
  saving.value = true
  try {
    const r = await api.saveSettings(s.value)
    const needRestart = !!r.restart_required
    // 静默重载：同步服务端清洗后的值（如剔除畸形 Bot）并重置基线快照
    await load(true)
    restartHint.value = needRestart
    const failedBots = r.bot_sync?.failed || []
    if (failedBots.length) {
      toast.error(`已保存，但这些 Bot 连接失败：${failedBots.map((bot) => bot.name).join('、')}`)
    } else {
      toast.success(needRestart
        ? '已保存。基础凭据、代理或数据库等改动需重启平台生效。'
        : (r.bot_sync ? '已保存，Bot 设置已立即生效。' : '已保存。'))
    }
    // Bot 列表可能变化 → 刷新推送路由的可选项
    if (tab.value === 'bots') loadRouting()
  } catch (e) { toast.error('保存失败：' + e.message) } finally { saving.value = false }
}

async function doRestart() {
  restarting.value = true
  try {
    await api.restartPlatform()
    toast.success('平台正在重启，十几秒后自动刷新')
    restartHint.value = false
    let tries = 0
    const timer = setInterval(async () => {
      tries++
      try { await api.status(); clearInterval(timer); location.reload() }
      catch { if (tries > 30) { clearInterval(timer); restarting.value = false } }
    }, 2000)
  } catch (e) { toast.error('重启请求失败：' + e.message); restarting.value = false }
}

// ── 连接测试（代理 / 数据库）──
const proxyTest = ref(null)     // { ok, message } | null
const proxyTesting = ref(false)
async function testProxy() {
  proxyTesting.value = true; proxyTest.value = null
  try { proxyTest.value = await api.testProxy(s.value.proxy_set) }
  catch (e) { proxyTest.value = { ok: false, message: e.message } }
  finally { proxyTesting.value = false }
}
const dbTest = ref(null)
const dbTesting = ref(false)
async function testDb() {
  dbTesting.value = true; dbTest.value = null
  try { dbTest.value = await api.testDb(s.value.DB_INFO) }
  catch (e) { dbTest.value = { ok: false, message: e.message } }
  finally { dbTesting.value = false }
}

// ── 备份 / 恢复 ──
const backupBusy = ref(false)
const restoreBusy = ref(false)
const restoreInput = ref(null)

function openRestorePicker() {
  restoreInput.value?.click()
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

async function downloadBackup() {
  backupBusy.value = true
  try {
    const { blob, filename } = await api.downloadBackup()
    saveBlob(blob, filename)
    toast.success('备份包已开始下载')
  } catch (e) {
    toast.error('导出备份失败：' + e.message)
  } finally {
    backupBusy.value = false
  }
}

async function onRestoreFile(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  const ok = await confirm({
    title: '导入恢复',
    message: '恢复会覆盖平台中的 data、sessions、db_file、plugins 目录内容。建议先确认备份包来源可信。继续恢复？',
    confirmText: '继续恢复',
    danger: true,
  })
  if (!ok) return

  restoreBusy.value = true
  try {
    const r = await api.restoreBackup(file)
    restartHint.value = !!r.restart_required
    if (r.pre_restore_backup) {
      try {
        const snapshot = await api.downloadStoredBackup(r.pre_restore_backup)
        saveBlob(snapshot.blob, snapshot.filename)
      } catch (downloadError) {
        toast.error('恢复包已暂存，但恢复前快照下载失败：' + downloadError.message)
      }
    }
    toast.success(`备份已校验，共 ${r.staged_files || 0} 个文件；重启平台后应用恢复`)
  } catch (err) {
    toast.error('恢复失败：' + err.message)
  } finally {
    restoreBusy.value = false
  }
}

// ── 多 Bot（额外 Bot 增删） ──
function addBot() {
  s.value.BOTS.push({ id: 'bot_' + Date.now().toString(36), name: '', token: '', chat_id: '' })
}
function removeBot(i) {
  const removed = s.value.BOTS[i]
  if (removed?.id === s.value.DEFAULT_BOT_ID) s.value.DEFAULT_BOT_ID = 'default'
  s.value.BOTS.splice(i, 1)
}
function setDefaultBot(id) { s.value.DEFAULT_BOT_ID = id || 'default' }

function configuredDefaultBotName() {
  if (s.value?.DEFAULT_BOT_ID === 'default') return s.value.BOT_NAME || '默认 Bot'
  return s.value?.BOTS?.find((bot) => bot.id === s.value.DEFAULT_BOT_ID)?.name || '默认 Bot'
}

// ── 平台 Webhook（密钥 + 随机按钮 + 地址展示） ──
function randomHex(bytesLen = 24) {
  const bytes = new Uint8Array(bytesLen)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}
function genWebhookSecret() { s.value.WEBHOOK_SECRET = randomHex(24) }
const platformWebhookUrl = computed(() => {
  if (!s.value?.WEBHOOK_SECRET) return ''
  return `${location.origin}/api/v1/webhook?apikey=${s.value.WEBHOOK_SECRET}`
})
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
async function copyPlatformWebhook() {
  if (!platformWebhookUrl.value) return
  if (await copyText(platformWebhookUrl.value)) toast.success('已复制平台 webhook 地址')
  else toast.error('复制失败，请手动选择复制')
}

// ── 通知渠道管理 ──
const channelModalOpen = ref(false)
const channelModalMode = ref('add')  // 'add' | 'edit'
const channelEditIndex = ref(-1)
const channelForm = ref({
  id: '',
  name: '',
  type: 'telegram',  // telegram | wechat | bark
  enabled: true,
  config: {}
})

const channelTypes = [
  { value: 'telegram', label: 'Telegram', icon: 'telegram' },
  { value: 'wechat', label: '企业微信', icon: 'wechat' },
  { value: 'bark', label: 'Bark', icon: 'bark' }
]

function openAddChannel() {
  channelModalMode.value = 'add'
  channelEditIndex.value = -1
  channelForm.value = {
    id: `ch_${Date.now()}`,
    name: '',
    type: 'telegram',
    enabled: true,
    config: {}
  }
  channelModalOpen.value = true
}

function openEditChannel(index) {
  channelModalMode.value = 'edit'
  channelEditIndex.value = index
  const ch = s.value.NOTIFICATION_CHANNELS[index]
  channelForm.value = JSON.parse(JSON.stringify(ch))
  channelModalOpen.value = true
}

function saveChannel() {
  if (!channelForm.value.name.trim()) {
    toast.error('请输入名称')
    return
  }

  if (channelModalMode.value === 'add') {
    s.value.NOTIFICATION_CHANNELS.push(JSON.parse(JSON.stringify(channelForm.value)))
  } else {
    s.value.NOTIFICATION_CHANNELS[channelEditIndex.value] = JSON.parse(JSON.stringify(channelForm.value))
  }

  channelModalOpen.value = false
  toast.success(channelModalMode.value === 'add' ? '已添加通知渠道' : '已更新通知渠道')
}

function deleteChannel(index) {
  if (!confirm(`确定删除通知渠道「${s.value.NOTIFICATION_CHANNELS[index].name}」？`)) return
  s.value.NOTIFICATION_CHANNELS.splice(index, 1)
  toast.success('已删除')
}

function toggleChannel(index) {
  s.value.NOTIFICATION_CHANNELS[index].enabled = !s.value.NOTIFICATION_CHANNELS[index].enabled
}

function getChannelIcon(type) {
  return type || 'telegram'
}

function getChannelTypeName(type) {
  const found = channelTypes.find(t => t.value === type)
  return found ? found.label : type
}

// ── 通知推送路由（哪个插件推到哪个 Bot） ──
const routing = ref({ bots: [], plugins: [] })
const routingLoading = ref(false)

async function loadRouting() {
  routingLoading.value = true
  try { routing.value = await api.getBotsRouting() }
  catch (e) { toast.error('加载推送路由失败：' + e.message) }
  finally { routingLoading.value = false }
}

async function saveRouting(p) {
  try {
    await api.setBotRouting(p.id, p.bot || '')
    toast.success(`「${p.name}」推送 Bot 已更新`)
  } catch (e) { toast.error('保存失败：' + e.message); loadRouting() }
}

// Bot 在线状态/用户名：取自 routing.bots（后端 list_bots，含 online/username）。
// 新加的额外 Bot 尚未保存重启，查不到状态，返回 null（UI 显示「未连接」）。
function botStatus(id) {
  return (routing.value.bots || []).find((b) => b.id === id) || null
}

// 推送路由搜索：按插件名/id 过滤
const routeSearch = ref('')
const filteredRoutePlugins = computed(() => {
  const q = routeSearch.value.trim().toLowerCase()
  const list = routing.value.plugins || []
  if (!q) return list
  return list.filter((p) => (p.name || '').toLowerCase().includes(q) || (p.id || '').toLowerCase().includes(q))
})

function goTab(k) {
  tab.value = k
  if (k === 'bots' && routing.value.plugins.length === 0) loadRouting()
}

// ── 登录凭据修改 ──
const cred = ref({ old_password: '', new_username: '', new_password: '' })
const credBusy = ref(false)
const credMsg = ref('')
const credErr = ref('')

async function loadUsername() {
  try { const st = await api.authStatus(); cred.value.new_username = st.username || '' } catch {}
}

async function saveCred() {
  credBusy.value = true; credMsg.value = ''; credErr.value = ''
  if (!cred.value.old_password) { credErr.value = '请输入当前密码'; credBusy.value = false; return }
  try {
    await api.changeCredentials(cred.value.old_password, cred.value.new_username, cred.value.new_password)
    credMsg.value = '登录凭据已更新。下次登录用新账号密码。'
    cred.value.old_password = ''; cred.value.new_password = ''
  } catch (e) { credErr.value = e.message } finally { credBusy.value = false }
}

onMounted(() => { load(); loadUsername() })

// 未保存改动保护：刷新/关页 + 站内切换路由时提醒
function beforeUnload(e) { if (dirty.value) { e.preventDefault(); e.returnValue = '' } }
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onUnmounted(() => window.removeEventListener('beforeunload', beforeUnload))
onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  return await confirm({
    title: '离开系统设置',
    message: '有未保存的改动，离开将丢失。确定离开？',
    confirmText: '离开', danger: true,
  })
})
</script>

<template>
  <div class="settings-page">
    <!-- 顶部 Tab 切换：一个分类一个菜单 -->
    <div class="toolbar">
      <div class="tabs">
        <button v-for="t in TABS" :key="t.key" class="tab" :class="{ active: tab === t.key }"
                @click="goTab(t.key)">{{ t.label }}</button>
      </div>
      <div class="row gap" v-if="s && tab !== 'login'">
        <button class="btn" @click="load" :disabled="!dirty" title="撤销未保存的改动，从服务器重新加载">撤销更改</button>
        <button class="btn btn-primary" @click="save" :disabled="saving || !dirty">
          <span v-if="dirty" class="dirty-dot"></span>{{ saving ? '保存中…' : (dirty ? '保存设置' : '已保存') }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="muted center">加载中…</div>
    <div v-else-if="s" class="panel">
      <div v-if="err" class="alert err">{{ err }}</div>
      <!-- 保存后需重启：给一键重启入口 -->
      <div v-if="restartHint" class="restart-banner">
        <span>部分改动需重启平台才生效。</span>
        <div class="row gap">
          <button class="btn sm" @click="restartHint = false">稍后</button>
          <button class="btn sm btn-primary" @click="doRestart" :disabled="restarting">
            {{ restarting ? '重启中…' : '立即重启' }}
          </button>
        </div>
      </div>

      <!-- 控制台登录 -->
      <div v-show="tab === 'login'" class="card">
        <div class="card-title">控制台登录</div>
        <div class="hint muted">修改登录本控制台的用户名和密码（默认 admin / password）。</div>
        <div v-if="credErr" class="alert err">{{ credErr }}</div>
        <div v-if="credMsg" class="alert ok">{{ credMsg }}</div>
        <div class="grid2">
          <div class="field"><label>用户名</label>
            <input class="input" v-model="cred.new_username" placeholder="登录用户名" /></div>
          <div class="field"><label>当前密码（验证身份）</label>
            <input class="input" type="password" v-model="cred.old_password" placeholder="输入当前密码" /></div>
          <div class="field"><label>新密码（不改留空）</label>
            <input class="input" type="password" v-model="cred.new_password" placeholder="至少 4 位" /></div>
        </div>
        <div class="actions">
          <button class="btn btn-primary" @click="saveCred" :disabled="credBusy">
            {{ credBusy ? '保存中…' : '更新登录凭据' }}
          </button>
        </div>
      </div>

      <!-- Telegram 凭据 -->
      <div v-show="tab === 'telegram'" class="card">
        <div class="card-title">Telegram 凭据</div>
        <div class="hint muted">从 my.telegram.org 获取 API_ID / API_HASH。Bot Token 在「通知」页配置。敏感值显示为打码，不改就留着。</div>
        <div class="grid2">
          <div class="field"><label>API ID</label>
            <input class="input" type="number" v-model.number="s.API_ID" /></div>
          <div class="field"><label>API HASH</label>
            <input class="input" v-model="s.API_HASH" /></div>
        </div>
      </div>

      <!-- 通知（通知 Bot 卡片 + 推送路由 + 平台 Webhook） -->
      <div v-show="tab === 'bots'" class="card">
        <div class="card-title">通知</div>
        <div class="hint muted">
          平台的插件通知（ctx.notify）与 bot 类插件都通过 Bot 发送。可配置多个 Bot，
          再在下方指定「每个插件推送到哪个 Bot」。Token 从 @BotFather 获取；Bot 改动保存后立即生效。
        </div>

        <!-- 通知 Bot 卡片网格 -->
        <div class="field">
          <label>通知 Bot</label>
          <div class="bot-grid">
            <!-- 内置 Bot -->
            <div class="bot-card">
              <div class="bot-card-head">
                <span class="bot-ava">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                       stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
                </span>
                <div class="bot-id">
                  <div class="bot-name-row">
                    <input class="input bot-name-input" v-model="s.BOT_NAME" placeholder="Bot 名称" />
                    <span v-if="s.DEFAULT_BOT_ID === 'default'" class="badge badge-default">默认</span>
                    <button v-else class="set-default-btn" type="button" @click="setDefaultBot('default')">设为默认</button>
                  </div>
                  <div class="bot-status">
                    <span class="dot" :class="{ on: botStatus('default')?.online }"></span>
                    <span class="muted">{{ botStatus('default')?.online ? '在线' : (botStatus('default') ? '离线' : '未连接') }}</span>
                    <span v-if="botStatus('default')?.username" class="muted mono">@{{ botStatus('default').username }}</span>
                  </div>
                </div>
              </div>
              <input class="input" v-model="s.BOT_TOKEN" placeholder="从 @BotFather 获取 Token" />
              <input class="input bot-chat" v-model="s.DEFAULT_BOT_CHAT_ID" placeholder="通知 Chat ID（留空=发给管理员）" />
            </div>

            <!-- 额外 Bot -->
            <div v-for="(b, i) in s.BOTS" :key="b.id || i" class="bot-card">
              <button class="bot-del" type="button" title="删除此 Bot" @click="removeBot(i)"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
              <div class="bot-card-head">
                <span class="bot-ava">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                       stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
                </span>
                <div class="bot-id">
                  <div class="bot-name-row">
                    <input class="input bot-name-input" v-model="b.name" placeholder="名称（如 订单Bot）" />
                    <span v-if="s.DEFAULT_BOT_ID === b.id" class="badge badge-default">默认</span>
                    <button v-else class="set-default-btn" type="button" @click="setDefaultBot(b.id)">设为默认</button>
                  </div>
                  <div class="bot-status">
                    <span class="dot" :class="{ on: botStatus(b.id)?.online }"></span>
                    <span class="muted">{{ botStatus(b.id)?.online ? '在线' : (botStatus(b.id) ? '离线' : '未连接（需保存）') }}</span>
                    <span v-if="botStatus(b.id)?.username" class="muted mono">@{{ botStatus(b.id).username }}</span>
                  </div>
                </div>
              </div>
              <input class="input" v-model="b.token" placeholder="Bot Token" />
              <input class="input bot-chat" v-model="b.chat_id" placeholder="通知 Chat ID（留空=发给管理员）" />
            </div>

            <!-- 添加 Bot -->
            <button class="bot-card bot-add" type="button" @click="addBot">
              <span class="bot-add-plus">+</span>
              <span>添加 Bot</span>
            </button>
          </div>
          <div class="hint muted small" style="margin-top:2px">所有 Bot 都可以改名或设为默认，修改后点右上“保存设置”立即生效。</div>
        </div>

        <!-- 推送路由 -->
        <div class="field" style="margin-top:10px">
          <label>推送路由（哪个插件推到哪个 Bot）</label>
          <div class="hint muted small" style="margin-bottom:8px">选择每个插件的通知发到哪个 Bot；选择立即生效（无需保存设置）。未单独选择时使用当前默认 Bot。</div>
          <div v-if="routingLoading" class="muted small">加载中…</div>
          <div v-else-if="routing.plugins.length === 0" class="muted small">还没有插件。</div>
          <template v-else>
            <input class="input route-search" v-model="routeSearch" placeholder="搜索插件名称 / id…" />
            <div v-if="filteredRoutePlugins.length === 0" class="muted small" style="margin-top:8px">没有匹配的插件。</div>
            <div v-else class="route-table">
              <div v-for="p in filteredRoutePlugins" :key="p.id" class="route-row">
                <span class="route-name" :title="p.id">{{ p.name }}</span>
                <select class="select route-sel" v-model="p.bot" @change="saveRouting(p)">
                  <option value="">默认（{{ configuredDefaultBotName() }}）</option>
                  <option v-for="b in routing.bots.filter(x => x.id !== s.DEFAULT_BOT_ID)" :key="b.id" :value="b.id">
                    {{ b.name }}{{ b.online ? '' : '（离线）' }}
                  </option>
                </select>
              </div>
            </div>
          </template>
        </div>

        <!-- 平台 Webhook -->
        <div class="field" style="margin-top:10px">
          <label>平台 Webhook</label>
          <div class="hint muted small" style="margin-bottom:8px">
            外部服务 POST 到下面的地址（JSON 可含 text/title/category 字段，或直接发文本），
            平台会把内容作为通知推送给管理员。留空密钥=关闭。改动随「保存设置」生效。
          </div>
          <div class="row gap">
            <input class="input" style="flex:1" v-model="s.WEBHOOK_SECRET" placeholder="点右侧随机生成，或自定义密钥" />
            <button class="btn sm" @click="genWebhookSecret" title="随机生成密钥">
              <svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
              </svg>随机
            </button>
          </div>
          <div v-if="platformWebhookUrl" class="webhook-url mono">{{ platformWebhookUrl }}</div>
          <button v-if="platformWebhookUrl" class="btn sm" style="align-self:flex-start" @click="copyPlatformWebhook">复制地址</button>
        </div>
      </div>

      <!-- 通知渠道 -->
      <div v-show="tab === 'bots'" class="card" style="margin-top:20px">
        <div class="card-title">通知渠道</div>
        <div class="hint muted small" style="margin-bottom:16px">
          配置消息发送渠道参数。启用后插件的 ctx.notify 会同时推送到这些渠道。
        </div>

        <!-- 通知渠道卡片网格 -->
        <div class="channel-grid-mp">
          <div v-for="(ch, idx) in s.NOTIFICATION_CHANNELS" :key="ch.id" class="channel-card-mp">
            <div class="channel-left">
              <span class="status-dot" :class="{ on: ch.enabled }"></span>
              <div class="channel-name-mp">{{ ch.name }}</div>
            </div>
            <div class="channel-center">
              <div class="channel-icon-mp">
                <!-- Telegram 图标 -->
                <svg v-if="ch.type === 'telegram'" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/>
                </svg>
                <!-- 企业微信图标 -->
                <svg v-else-if="ch.type === 'wechat'" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 0 1 .213.665l-.39 1.48c-.019.07-.048.141-.048.213 0 .163.13.295.29.295a.326.326 0 0 0 .167-.054l1.903-1.114a.864.864 0 0 1 .717-.098 10.16 10.16 0 0 0 2.837.403c.276 0 .543-.027.811-.05-.857-2.578.157-4.972 1.932-6.446 1.703-1.415 3.882-1.98 5.853-1.838-.576-3.583-4.196-6.348-8.596-6.348zM5.785 5.991c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178A1.17 1.17 0 0 1 4.623 7.17c0-.651.52-1.18 1.162-1.18zm5.813 0c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178 1.17 1.17 0 0 1-1.162-1.178c0-.651.52-1.18 1.162-1.18zm5.34 2.867c-1.797-.052-3.746.512-5.28 1.786-1.72 1.428-2.687 3.72-1.78 6.22.942 2.453 3.666 4.229 6.884 4.229.826 0 1.622-.12 2.361-.336a.722.722 0 0 1 .598.082l1.584.926a.272.272 0 0 0 .14.045c.134 0 .24-.111.24-.247 0-.06-.023-.12-.038-.177l-.327-1.233a.582.582 0 0 1-.023-.156.49.49 0 0 1 .201-.398C23.024 18.48 24 16.82 24 14.98c0-3.21-2.931-5.837-6.656-6.088V8.89c-.135-.01-.27-.027-.407-.03zm-2.53 3.274c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982zm4.844 0c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982z"/>
                </svg>
                <!-- Bark 图标 -->
                <svg v-else-if="ch.type === 'bark'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                  <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
                </svg>
              </div>
              <div class="channel-type-mp">{{ getChannelTypeName(ch.type) }}</div>
            </div>
            <div class="channel-right">
              <button class="channel-btn-icon" @click="openEditChannel(idx)" title="编辑">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>
                </svg>
              </button>
              <button class="channel-btn-icon" @click="deleteChannel(idx)" title="删除">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M18 6 6 18M6 6l12 12"/>
                </svg>
              </button>
            </div>
          </div>
        </div>

        <!-- 底部按钮 -->
        <div class="channel-footer">
          <button class="btn-add-mp" @click="openAddChannel">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 5v14m-7-7h14"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Web 控制台 -->
      <div v-show="tab === 'web'" class="card">
        <div class="card-title">Web 控制台</div>
        <div class="grid2">
          <div class="field"><label>监听端口 (WEB_UI_PORT)</label>
            <input class="input" type="number" v-model.number="s.WEB_UI_PORT" /></div>
          <div class="field"><label>外部地址 (WEB_UI_URL，可空)</label>
            <input class="input" v-model="s.WEB_UI_URL" /></div>
        </div>
        <div class="hint muted small">需要公网访问请自行用 Nginx / Caddy 等反向代理到本机端口。</div>
      </div>

      <!-- 运行代理 -->
      <div v-show="tab === 'proxy'" class="card">
        <div class="card-title">运行代理</div>
        <div class="row between">
          <span>启用代理</span>
          <div class="toggle" :class="{ on: s.proxy_set.proxy_enable }"
               @click="s.proxy_set.proxy_enable = !s.proxy_set.proxy_enable"></div>
        </div>
        <template v-if="s.proxy_set.proxy_enable">
          <div class="grid2">
            <div class="field"><label>协议</label>
              <select class="select" v-model="s.proxy_set.proxy.scheme">
                <option value="http">http</option><option value="socks4">socks4</option><option value="socks5">socks5</option>
              </select></div>
            <div class="field"><label>主机</label>
              <input class="input" v-model="s.proxy_set.proxy.hostname" /></div>
            <div class="field"><label>端口</label>
              <input class="input" type="number" v-model.number="s.proxy_set.proxy.port" /></div>
            <div class="field"><label>代理 URL (网页访问用)</label>
              <input class="input" v-model="s.proxy_set.PROXY_URL" /></div>
            <div class="field"><label>用户名 (可空)</label>
              <input class="input" v-model="s.proxy_set.proxy.username" /></div>
            <div class="field"><label>密码 (可空)</label>
              <input class="input" type="password" v-model="s.proxy_set.proxy.password" /></div>
          </div>
          <div class="test-row">
            <button class="btn sm" @click="testProxy" :disabled="proxyTesting">
              {{ proxyTesting ? '测试中…' : '测试代理' }}
            </button>
            <span v-if="proxyTest" class="test-result" :class="proxyTest.ok ? 'ok' : 'bad'">{{ proxyTest.message }}</span>
          </div>
        </template>
        <div class="field" style="margin-top:14px">
          <label>pip 镜像源 (插件装依赖用)</label>
          <input class="input" v-model="s.PIP_INDEX_URL"
                 placeholder="https://pypi.tuna.tsinghua.edu.cn/simple" />
          <div class="hint muted small">墙内建议填国内镜像（清华/阿里），境内直连不经墙。留空则走官方 pypi（此时若启用了上面的代理会自动用代理出墙）。</div>
        </div>
      </div>

      <!-- 数据库 -->
      <div v-show="tab === 'db'" class="card">
        <div class="card-title">数据库</div>
        <div class="grid2">
          <div class="field"><label>类型</label>
            <select class="select" v-model="s.DB_INFO.dbset">
              <option value="SQLite">SQLite</option><option value="mySQL">mySQL</option><option value="PostgreSQL">PostgreSQL</option>
            </select></div>
          <div class="field"><label>库名</label>
            <input class="input" v-model="s.DB_INFO.db_name" /></div>
        </div>
        <template v-if="s.DB_INFO.dbset !== 'SQLite'">
          <div class="grid2">
            <div class="field"><label>地址</label><input class="input" v-model="s.DB_INFO.address" /></div>
            <div class="field"><label>端口</label><input class="input" type="number" v-model.number="s.DB_INFO.port" /></div>
            <div class="field"><label>用户</label><input class="input" v-model="s.DB_INFO.user" /></div>
            <div class="field"><label>密码</label>
              <input class="input" type="password" v-model="s.DB_INFO.password" /></div>
          </div>
        </template>
        <div class="test-row">
          <button class="btn sm" @click="testDb" :disabled="dbTesting">
            {{ dbTesting ? '测试中…' : '测试连接' }}
          </button>
          <span v-if="dbTest" class="test-result" :class="dbTest.ok ? 'ok' : 'bad'">{{ dbTest.message }}</span>
        </div>
      </div>

      <!-- 维护 -->
      <div v-show="tab === 'maint'" class="card">
        <div class="card-title">维护</div>
        <div class="hint muted">
          这里可以设置日志自动清理、导出当前平台快照，或从已有备份包恢复。备份会包含 data/、sessions/、db_file/、plugins/。
          导入时会先校验并下载当前快照，重启平台后再应用恢复，避免损坏运行中的数据库。
        </div>

        <div class="maint-box">
          <div class="maint-item maint-settings">
            <div class="maint-heading">
              <div>
                <div class="maint-name">日志清理</div>
                <div class="maint-desc muted">定时清理平台运行日志和插件历史日志，避免长期占用磁盘空间。</div>
              </div>
              <button type="button" class="toggle" :class="{ on: s.LOG_CLEANER.enabled }"
                      :aria-pressed="s.LOG_CLEANER.enabled" aria-label="启用日志清理"
                      @click="s.LOG_CLEANER.enabled = !s.LOG_CLEANER.enabled"></button>
            </div>
            <div class="grid3" :class="{ disabled: !s.LOG_CLEANER.enabled }">
              <div class="field">
                <label>每天执行时间</label>
                <div class="time-fields">
                  <input class="input" type="number" min="0" max="23" aria-label="执行小时"
                         v-model.number="s.LOG_CLEANER.hour" />
                  <span>:</span>
                  <input class="input" type="number" min="0" max="59" aria-label="执行分钟"
                         v-model.number="s.LOG_CLEANER.minute" />
                </div>
              </div>
              <div class="field">
                <label>每个日志保留条数</label>
                <input class="input" type="number" min="1" max="1000" v-model.number="s.LOG_CLEANER.keep_lines" />
              </div>
            </div>
          </div>

          <div class="maint-item">
            <div>
              <div class="maint-name">导出备份</div>
              <div class="maint-desc muted">生成 zip 备份包，便于迁移、回滚或手动归档。</div>
            </div>
            <button class="btn btn-primary" @click="downloadBackup" :disabled="backupBusy">
              {{ backupBusy ? '导出中…' : '下载备份' }}
            </button>
          </div>

          <div class="maint-item">
            <div>
              <div class="maint-name">导入恢复</div>
              <div class="maint-desc muted">导入平台生成的 zip 备份包；重启后完整替换平台运行数据。</div>
            </div>
            <button class="btn" @click="openRestorePicker" :disabled="restoreBusy">
              {{ restoreBusy ? '恢复中…' : '选择备份包' }}
            </button>
            <input ref="restoreInput" type="file" accept=".zip,application/zip" style="display:none" @change="onRestoreFile" />
          </div>
        </div>
      </div>

      <div class="hint muted foot" v-if="tab === 'telegram'">提示：账号登录在「账号管理」页完成，账号列表会随登录自动写入。</div>
    </div>
  </div>

  <!-- 通知渠道配置弹窗 -->
  <div v-if="channelModalOpen" class="modal-mask" @click.self="channelModalOpen = false">
    <div class="modal-dialog channel-modal">
      <div class="modal-header">
        <h3>{{ channelModalMode === 'add' ? '添加通知渠道' : '编辑通知渠道' }}</h3>
        <button class="modal-close" @click="channelModalOpen = false">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 6 6 18M6 6l12 12"/>
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <!-- 启用状态 -->
        <div class="field">
          <label class="checkbox-label">
            <input type="checkbox" v-model="channelForm.enabled" />
            <span>启用</span>
          </label>
        </div>

        <!-- 类型选择 -->
        <div class="field">
          <label>类型</label>
          <select class="select" v-model="channelForm.type" :disabled="channelModalMode === 'edit'">
            <option v-for="type in channelTypes" :key="type.value" :value="type.value">
              {{ type.label }}
            </option>
          </select>
          <div class="hint muted small">通知渠道类型</div>
        </div>

        <!-- 名称 -->
        <div class="field">
          <label>名称</label>
          <input class="input" v-model="channelForm.name" placeholder="如：通知1、订单通知" />
          <div class="hint muted small">通知渠道名称</div>
        </div>

        <!-- Telegram 配置 -->
        <template v-if="channelForm.type === 'telegram'">
          <div class="field">
            <label>Bot Token</label>
            <input class="input" v-model="channelForm.config.token"
                   placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" />
            <div class="hint muted small">Telegram机器人token，格式：123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11</div>
          </div>
          <div class="field">
            <label>Chat ID（可选）</label>
            <input class="input" v-model="channelForm.config.chat_id"
                   placeholder="接收消息的用户、群组或频道 Chat ID" />
            <div class="hint muted small">接收消息通知的用户、群组或频道Chat ID</div>
          </div>
          <div class="field">
            <label>用户白名单（可选）</label>
            <input class="input" v-model="channelForm.config.user_whitelist"
                   placeholder="可使用Telegram机器人的用户ID列表，多个用,分隔" />
            <div class="hint muted small">可使用Telegram机器人的用户ID清单，多个用户ID用,分隔，不填表示所有用户可用</div>
          </div>
          <div class="field">
            <label>管理员白名单（可选）</label>
            <input class="input" v-model="channelForm.config.admin_whitelist"
                   placeholder="可管理菜单及命令的管理员用户ID列表，多个用,分隔" />
            <div class="hint muted small">可使用管理菜单及命令的管理员用户ID列表，多个ID用,分隔</div>
          </div>
          <div class="field">
            <label>代理API地址（可选）</label>
            <input class="input" v-model="channelForm.config.api_proxy"
                   placeholder="https://api.telegram.org" />
            <div class="hint muted small">自定义代理API地址，格式：https://api.telegram.org</div>
          </div>
        </template>

        <!-- 企业微信配置 -->
        <template v-if="channelForm.type === 'wechat'">
          <div class="field">
            <label>企业ID</label>
            <input class="input" v-model="channelForm.config.corpid"
                   placeholder="企业微信后台企业信息中的企业ID" />
            <div class="hint muted small">企业微信后台企业信息中的企业ID</div>
          </div>
          <div class="field">
            <label>应用AgentId</label>
            <input class="input" v-model="channelForm.config.agentid"
                   placeholder="企业微信自建应用的AgentId" />
            <div class="hint muted small">企业微信自建应用的AgentId</div>
          </div>
          <div class="field">
            <label>应用Secret</label>
            <input class="input" v-model="channelForm.config.secret"
                   placeholder="企业微信自建应用的Secret" />
            <div class="hint muted small">企业微信自建应用的Secret</div>
          </div>
          <div class="field">
            <label>代理地址（可选）</label>
            <input class="input" v-model="channelForm.config.proxy"
                   placeholder="https://qyapi.weixin.qq.com" />
            <div class="hint muted small">微信消息发送代理地址，2022年6月20日后创建的应用才需要，不填代理则无法发送消息</div>
          </div>
          <div class="field">
            <label>Token（可选）</label>
            <input class="input" v-model="channelForm.config.token"
                   placeholder="微信企业自建应用->API接收消息配置中的Token" />
            <div class="hint muted small">微信企业自建应用->API接收消息配置中的Token</div>
          </div>
          <div class="field">
            <label>EncodingAESKey（可选）</label>
            <input class="input" v-model="channelForm.config.aes_key"
                   placeholder="微信企业自建应用->API接收消息配置中的EncodingAESKey" />
            <div class="hint muted small">微信企业自建应用->API接收消息配置中的EncodingAESKey</div>
          </div>
          <div class="field">
            <label>管理员白名单（可选）</label>
            <input class="input" v-model="channelForm.config.admin_whitelist"
                   placeholder="可使用管理菜单及命令的用户ID列表，多个用,分隔" />
            <div class="hint muted small">可使用管理菜单及命令的用户ID列表，多个ID用,分隔</div>
          </div>
        </template>

        <!-- Bark 配置 -->
        <template v-if="channelForm.type === 'bark'">
          <div class="field">
            <label>服务器地址</label>
            <input class="input" v-model="channelForm.config.server"
                   placeholder="https://api.day.app" />
            <div class="hint muted small">Bark 服务器地址，如 https://api.day.app</div>
          </div>
          <div class="field">
            <label>设备密钥</label>
            <input class="input" v-model="channelForm.config.device_key"
                   placeholder="从 Bark App 中获取" />
            <div class="hint muted small">从 Bark App 中获取的设备密钥</div>
          </div>
        </template>
      </div>
      <div class="modal-footer">
        <button class="btn" @click="channelModalOpen = false">取消</button>
        <button class="btn primary" @click="saveChannel">确认</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page { position: relative; min-height: 100%; }
.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; gap: 16px; flex-wrap: wrap; }

/* Tab 切换（与插件管理一致） */
.tabs { display: flex; gap: 4px; background: var(--bg-elevated); padding: 4px; border-radius: 10px; flex-wrap: wrap; }
.tab {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 18px; border: none; background: transparent; cursor: pointer;
  color: var(--text-secondary); font-size: 14px; font-weight: 600; border-radius: 7px;
  transition: all 0.15s;
}
.tab:hover { color: var(--text-primary); }
.tab.active { background: var(--bg-card); color: var(--text-primary); box-shadow: 0 1px 3px rgba(0,0,0,0.25); }

.panel { max-width: 760px; }
.center { text-align: center; padding: 40px; }

/* 未保存改动小圆点 */
.dirty-dot { width: 7px; height: 7px; border-radius: 50%; background: #fff; margin-right: 2px; flex-shrink: 0; }

/* 保存后需重启横幅 */
.restart-banner {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  background: var(--accent-dim); color: var(--text-primary);
  border: 1px solid var(--accent); border-radius: var(--radius-sm);
  padding: 10px 14px; font-size: 13px; margin-bottom: 14px;
}

/* 连接测试行 */
.test-row { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.test-result { font-size: 12px; }
.test-result.ok { color: var(--accent-2); }
.test-result.bad { color: var(--danger); }
.alert { padding: 10px 14px; border-radius: var(--radius-sm); font-size: 13px; margin-bottom: 14px; }
.alert.err { background: var(--danger-dim); color: var(--danger); }
.alert.ok { background: var(--accent-2-dim); color: var(--accent-2); }
.card { display: flex; flex-direction: column; gap: 14px; }
.card-title { font-size: 14px; font-weight: 600; color: var(--accent); }
.hint { font-size: 12px; }
.foot { margin-top: 16px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 12px; color: var(--text-secondary); }
.actions { display: flex; justify-content: flex-end; gap: 10px; }
.row.between { display: flex; align-items: center; justify-content: space-between; }

/* 通知：Bot 卡片网格 + 推送路由表 */
.btn.sm { padding: 6px 12px; font-size: 13px; }
.btn.sm.danger { color: var(--danger); }

.bot-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.bot-card {
  position: relative;
  background: var(--bg-elevated); border: 1px solid var(--border-light);
  border-radius: var(--radius); padding: 14px;
  display: flex; flex-direction: column; gap: 12px;
}
.bot-card-head { display: flex; align-items: flex-start; gap: 10px; }
.bot-ava {
  flex-shrink: 0; width: 38px; height: 38px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-dim); color: var(--accent);
}
.bot-ava svg { width: 20px; height: 20px; }
.bot-id { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.bot-name-row { width: 100%; display: flex; align-items: center; gap: 8px; }
.bot-name-text { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.badge-default { background: var(--accent-dim); color: var(--accent); font-size: 10px; padding: 1px 8px; }
.bot-name-input { min-width: 0; flex: 1; padding: 6px 10px; font-size: 13px; }
.set-default-btn {
  flex: 0 0 auto; padding: 4px 8px; border: 1px solid var(--border-light); border-radius: 7px;
  color: var(--text-secondary); background: transparent; font-size: 11px; cursor: pointer;
}
.set-default-btn:hover { color: var(--accent); border-color: var(--accent); background: var(--accent-dim); }
.bot-chat { padding: 6px 10px; font-size: 12px; }
.bot-status { display: flex; align-items: center; gap: 6px; font-size: 11px; }
.bot-status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; }
.bot-status .dot.on { background: var(--success); box-shadow: 0 0 6px var(--success); }
.bot-status .mono { font-size: 11px; }
.bot-del {
  position: absolute; top: 8px; right: 8px;
  width: 22px; height: 22px; border-radius: 6px; border: none;
  background: transparent; color: var(--text-muted); cursor: pointer; font-size: 13px;
  display: flex; align-items: center; justify-content: center; transition: all 0.15s;
}
.bot-del:hover { background: var(--danger-dim); color: var(--danger); }
.bot-del .x-ico { width: 14px; height: 14px; }
.btn-ico { width: 14px; height: 14px; flex-shrink: 0; }
.bot-add {
  align-items: center; justify-content: center; gap: 6px;
  border-style: dashed; color: var(--text-secondary); cursor: pointer;
  min-height: 96px; font-size: 13px; font-weight: 600;
}
.bot-add:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.bot-add-plus { font-size: 22px; line-height: 1; }

.route-search { margin-bottom: 8px; }
.route-table { display: flex; flex-direction: column; gap: 8px; max-height: 360px; overflow-y: auto; }
.route-row { display: flex; align-items: center; gap: 10px; }
.route-name { flex: 1; font-size: 13px; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.route-sel { max-width: 220px; flex: 0 0 auto; }
.small { font-size: 12px; }
.maint-box { display: flex; flex-direction: column; gap: 12px; }
.maint-item {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-elevated);
}
.maint-name { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.maint-desc { font-size: 12px; margin-top: 4px; max-width: 520px; }
.maint-settings { align-items: stretch; flex-direction: column; }
.maint-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.grid3 { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr); gap: 14px; }
.grid3.disabled { opacity: 0.45; pointer-events: none; }
.time-fields { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 8px; }
.time-fields span { color: var(--text-muted); font-weight: 700; }

/* 平台 webhook 地址展示 */
.row.gap { display: flex; align-items: center; gap: 8px; }
.row.gap .input { flex: 1; }
.webhook-url {
  margin-top: 8px; font-size: 12px; word-break: break-all; padding: 8px 10px;
  background: #07090f; border-radius: var(--radius-sm); color: var(--text-primary);
}
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }

/* 通知渠道卡片（MP风格） */
.channel-grid-mp {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.channel-card-mp {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  transition: all 0.15s;
}

.channel-card-mp:hover {
  border-color: var(--accent-dim);
  background: var(--bg-hover);
}

.channel-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1;
  min-width: 0;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}

.status-dot.on {
  background: var(--accent-2);
  box-shadow: 0 0 8px var(--accent-2);
}

.channel-name-mp {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.channel-center {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 16px;
}

.channel-icon-mp {
  width: 56px;
  height: 56px;
  border-radius: 12px;
  background: var(--bg-card);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.channel-icon-mp svg {
  width: 32px;
  height: 32px;
  color: var(--accent);
}

.channel-type-mp {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
}

.channel-right {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.channel-btn-icon {
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}

.channel-btn-icon svg {
  width: 18px;
  height: 18px;
}

.channel-btn-icon:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.channel-footer {
  display: flex;
  gap: 10px;
  margin-top: 12px;
}

.btn-add-mp {
  width: 48px;
  height: 48px;
  border: 2px dashed var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}

.btn-add-mp svg {
  width: 24px;
  height: 24px;
}

.btn-add-mp:hover {
  border-color: var(--accent);
  background: var(--accent-dim);
  color: var(--accent);
}

/* 通知渠道配置弹窗 */
.modal-mask {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.7);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  padding: 20px;
}

.modal-dialog {
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: 12px;
  max-width: 600px;
  width: 100%;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px;
  border-bottom: 1px solid var(--border);
}

.modal-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.modal-close {
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

.modal-close svg {
  width: 20px;
  height: 20px;
}

.modal-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 20px;
  border-top: 1px solid var(--border);
}

@media (max-width: 600px) {
  .grid2 { grid-template-columns: 1fr; }
  .bot-grid { grid-template-columns: 1fr; }
  .channel-grid { grid-template-columns: 1fr; }
}

/* 手机适配 */
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; gap: 12px; }
  /* tab 全部换行展示，避免靠右的「数据库」被裁掉看不全 */
  .tabs { flex-wrap: wrap; }
  .tab { white-space: nowrap; flex: 1 1 auto; justify-content: center; padding: 8px 12px; }
  .panel { max-width: 100%; }
  .maint-item { flex-direction: column; align-items: stretch; }
  .grid3 { grid-template-columns: 1fr; }
}
</style>
