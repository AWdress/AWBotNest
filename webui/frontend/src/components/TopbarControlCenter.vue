<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, getToken } from '../api'
import { toast } from '../composables/toast'

const props = defineProps({
  online: Boolean,
  version: { type: String, default: '' },
  restarting: Boolean,
})
const emit = defineEmits(['restart', 'logout'])
const router = useRouter()

const panel = ref('')
const modal = ref('')
const profile = ref({ username: '管理员', avatar_url: '' })
const notices = ref([])
const unread = ref(0)
const expandedNotice = ref('')
const avatarInput = ref(null)
const avatarBusy = ref(false)
const logs = ref([])
const logLevel = ref('ALL')
const logSearch = ref('')
const logPaused = ref(false)
const logConnected = ref(false)
const health = ref([])
const healthBusy = ref(false)
const network = ref([])
const networkBusy = ref({})
const jobs = ref([])
let logsWs = null
let logsReconnect = null
let noticeTimer = null
let jobsTimer = null
const seenLogs = new Set()

const modalTitle = computed(() => ({
  logs: '实时日志',
  network: '网络测试',
  health: '系统健康检查',
  services: '定时服务',
}[modal.value] || ''))

const filteredLogs = computed(() => logs.value.filter(item => {
  if (logLevel.value !== 'ALL' && item.level !== logLevel.value) return false
  const term = logSearch.value.trim().toLowerCase()
  return !term || `${item.source || ''} ${item.msg || ''}`.toLowerCase().includes(term)
}))

function togglePanel(name) {
  panel.value = panel.value === name ? '' : name
  if (panel.value === 'notifications') loadNotifications(true)
}

function closePanels(event) {
  if (!event?.target?.closest?.('.control-center')) panel.value = ''
}

async function loadProfile() {
  try { profile.value = await api.getUiProfile() } catch {}
}

async function changeAvatar(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  avatarBusy.value = true
  try {
    const result = await api.uploadAvatar(file)
    profile.value.avatar_url = result.avatar_url
    toast.success('头像已更新')
  } catch (error) {
    toast.error(`头像更新失败：${error.message}`)
  } finally {
    avatarBusy.value = false
  }
}

async function loadNotifications(markRead = false) {
  try {
    const result = await api.getNotifications()
    notices.value = result.notifications || []
    unread.value = result.unread || 0
    if (markRead && unread.value) {
      await api.readNotifications()
      unread.value = 0
      notices.value.forEach(item => { item.unread = false })
    }
  } catch {}
}

async function markAllRead() {
  try {
    await api.readNotifications()
    unread.value = 0
    notices.value.forEach(item => { item.unread = false })
  } catch (error) {
    toast.error(error.message)
  }
}

async function clearNotifications() {
  try {
    await api.clearNotifications()
    notices.value = []
    unread.value = 0
  } catch (error) {
    toast.error(error.message)
  }
}

function relativeTime(timestamp) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - Number(timestamp || 0)))
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

function noticeKey(item) {
  return item.id || `notice-${item.t}`
}

function openModal(name) {
  panel.value = ''
  modal.value = name
  if (name === 'logs') {
    logPaused.value = false
    logs.value = []
    seenLogs.clear()
    connectLogs()
  }
  if (name === 'network') loadNetwork()
  if (name === 'health') loadHealth()
  if (name === 'services') {
    loadJobs()
    clearInterval(jobsTimer)
    jobsTimer = setInterval(() => loadJobs(true), 1500)
  }
}

function closeModal() {
  if (modal.value === 'logs') disconnectLogs()
  if (modal.value === 'services') {
    clearInterval(jobsTimer)
    jobsTimer = null
  }
  modal.value = ''
}

function logsWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/api/logs/ws?token=${encodeURIComponent(getToken())}`
}

function connectLogs() {
  disconnectLogs()
  logsWs = new WebSocket(logsWsUrl())
  logsWs.onopen = () => { logConnected.value = true }
  logsWs.onmessage = event => {
    if (logPaused.value) return
    try {
      const item = JSON.parse(event.data)
      const key = item.id || `${item.timestamp}|${item.level}|${item.source}|${item.msg}`
      if (seenLogs.has(key)) return
      seenLogs.add(key)
      logs.value.unshift(item)
      if (logs.value.length > 1000) logs.value.length = 1000
      if (seenLogs.size > 1500) {
        seenLogs.clear()
        logs.value.forEach(entry => seenLogs.add(
          entry.id || `${entry.timestamp}|${entry.level}|${entry.source}|${entry.msg}`,
        ))
      }
    } catch {}
  }
  logsWs.onclose = () => {
    logConnected.value = false
    if (modal.value === 'logs') logsReconnect = setTimeout(connectLogs, 3000)
  }
}

function disconnectLogs() {
  clearTimeout(logsReconnect)
  if (logsWs) {
    logsWs.onclose = null
    logsWs.close()
    logsWs = null
  }
  logConnected.value = false
}

function toggleLogPause() {
  logPaused.value = !logPaused.value
  if (logPaused.value) disconnectLogs()
  else connectLogs()
}

async function loadHealth() {
  healthBusy.value = true
  try {
    health.value = (await api.getHealth()).checks || []
  } catch (error) {
    toast.error(`健康检查失败：${error.message}`)
  } finally {
    healthBusy.value = false
  }
}

async function loadNetwork() {
  try {
    const result = await api.getNetworkTargets()
    network.value = (result.targets || []).map(item => ({ ...item, state: 'idle', detail: '等待测试' }))
    await Promise.all(network.value.map(item => testNetwork(item)))
  } catch (error) {
    toast.error(`网络测试加载失败：${error.message}`)
  }
}

async function testNetwork(item) {
  if (networkBusy.value[item.id]) return
  networkBusy.value = { ...networkBusy.value, [item.id]: true }
  item.state = 'testing'
  item.detail = '测试中…'
  try {
    const result = await api.testNetworkTarget(item.id)
    item.state = result.ok ? 'ok' : 'error'
    item.detail = result.ok ? `正常 · ${result.latency_ms} ms` : `失败 · ${result.detail}`
  } catch (error) {
    item.state = 'error'
    item.detail = `失败 · ${error.message}`
  } finally {
    networkBusy.value = { ...networkBusy.value, [item.id]: false }
  }
}

async function loadJobs(silent = false) {
  try {
    const result = await api.status(true)
    jobs.value = result.scheduler_jobs || []
  } catch (error) {
    if (!silent) toast.error(`定时任务加载失败：${error.message}`)
  }
}

async function runJob(job) {
  if (job.running) return
  job.running = true
  try {
    await api.runSchedulerJob(job.id)
    toast.success(`已开始执行：${job.name}`)
    setTimeout(() => loadJobs(true), 350)
  } catch (error) {
    job.running = false
    toast.error(error.message)
  }
}

function goSettings() {
  panel.value = ''
  router.push('/settings')
}

function onKeydown(event) {
  if (event.key === 'Escape') {
    panel.value = ''
    closeModal()
  }
}

onMounted(() => {
  loadProfile()
  loadNotifications()
  noticeTimer = setInterval(loadNotifications, 30000)
  document.addEventListener('click', closePanels)
  document.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  clearInterval(noticeTimer)
  clearInterval(jobsTimer)
  disconnectLogs()
  document.removeEventListener('click', closePanels)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="control-center" @click.stop>
    <button class="control-btn" :class="{ active: panel === 'shortcuts' }"
            title="快捷入口" @click="togglePanel('shortcuts')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="3" y="5" width="14" height="12" rx="2"/><path d="M7 20h12a2 2 0 0 0 2-2V9"/>
      </svg>
    </button>
    <button class="control-btn" :class="{ active: panel === 'notifications' }"
            title="通知中心" @click="togglePanel('notifications')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M6 10a6 6 0 0 1 12 0c0 5 2 5 2 7H4c0-2 2-2 2-7Z"/><path d="M10 21h4"/>
      </svg>
      <span v-if="unread" class="notice-badge">{{ unread > 99 ? '99+' : unread }}</span>
    </button>
    <button class="avatar-btn" :class="{ active: panel === 'user' }"
            title="管理员菜单" @click="togglePanel('user')">
      <img v-if="profile.avatar_url" :src="profile.avatar_url" alt="管理员头像"
           @error="profile.avatar_url = ''">
      <span v-else>{{ profile.username.slice(0, 2).toUpperCase() }}</span>
    </button>

    <div v-if="panel === 'shortcuts'" class="control-pop shortcuts-pop">
      <div class="pop-head"><strong>快捷入口</strong><button @click="panel=''">×</button></div>
      <div class="shortcut-grid">
        <button @click.stop="openModal('logs')"><i>▤</i><span><strong>实时日志</strong><small>查看最新运行记录</small></span></button>
        <button @click.stop="openModal('network')"><i>⌁</i><span><strong>网络测试</strong><small>检查外部服务连接</small></span></button>
        <button @click.stop="openModal('health')"><i>✦</i><span><strong>系统健康检查</strong><small>检查平台核心服务</small></span></button>
        <button @click.stop="openModal('services')"><i>◷</i><span><strong>定时服务</strong><small>查看和执行定时任务</small></span></button>
      </div>
    </div>

    <div v-if="panel === 'notifications'" class="control-pop notifications-pop">
      <div class="pop-head">
        <strong>通知中心</strong>
        <span><button title="全部已读" @click="markAllRead">✓</button><button title="清空" @click="clearNotifications">⌫</button></span>
      </div>
      <div class="notice-list">
        <div v-if="!notices.length" class="empty">暂无通知</div>
        <button v-for="item in notices" :key="noticeKey(item)"
                class="notice-item" :class="{ unread: item.unread, expanded: expandedNotice === noticeKey(item) }"
                @click="expandedNotice = expandedNotice === noticeKey(item) ? '' : noticeKey(item)">
          <span class="notice-icon">{{ item.level === 'error' ? '!' : item.level === 'success' ? '✓' : '·' }}</span>
          <span class="notice-body">
            <strong>{{ item.plugin_name || '平台通知' }}</strong>
            <span class="notice-text">{{ item.text }}</span>
            <small>{{ item.category || '系统消息' }} · {{ relativeTime(item.t) }}</small>
          </span>
        </button>
      </div>
    </div>

    <div v-if="panel === 'user'" class="control-pop user-pop">
      <div class="user-profile">
        <button class="avatar-large" :disabled="avatarBusy" title="修改头像" @click="avatarInput?.click()">
          <img v-if="profile.avatar_url" :src="profile.avatar_url" alt="管理员头像"
               @error="profile.avatar_url = ''">
          <span v-else>{{ profile.username.slice(0, 2).toUpperCase() }}</span>
          <i>{{ avatarBusy ? '上传中' : '修改' }}</i>
        </button>
        <div><small>管理员</small><strong>{{ profile.username }}</strong></div>
        <input ref="avatarInput" type="file" accept="image/png,image/jpeg,image/webp,image/gif"
               hidden @change="changeAvatar">
      </div>
      <div class="user-menu">
        <button @click="goSettings">系统设置</button>
        <button @click.stop="openModal('health')">系统健康检查</button>
        <button :disabled="restarting" @click="panel=''; emit('restart')">{{ restarting ? '重启中…' : '重启平台' }}</button>
        <button class="danger" @click="emit('logout')">退出登录</button>
      </div>
      <div class="user-status"><span :class="{ online }"></span>{{ online ? '平台在线' : '平台连接中' }}<b v-if="version">v{{ version }}</b></div>
    </div>
  </div>

  <Teleport to="body">
    <div v-if="modal" class="control-modal-mask" @click.self="closeModal">
      <section class="control-modal">
        <header><strong>{{ modalTitle }}</strong><button @click="closeModal">×</button></header>

        <div v-if="modal === 'logs'" class="modal-body logs-modal">
          <div class="log-toolbar">
            <span class="live-state" :class="{ on: logConnected }">
              {{ logPaused ? '已暂停' : (logConnected ? '实时' : '连接中') }}
            </span>
            <select v-model="logLevel" class="select">
              <option v-for="level in ['ALL','INFO','WARNING','ERROR']" :key="level">{{ level }}</option>
            </select>
            <input v-model="logSearch" class="input" placeholder="搜索日志内容">
            <button class="btn" :class="{ 'btn-primary': logPaused }" @click="toggleLogPause">{{ logPaused ? '继续' : '暂停' }}</button>
          </div>
          <div class="quick-log-list">
            <div v-if="!filteredLogs.length" class="empty">暂无日志</div>
            <div v-for="(item, index) in filteredLogs" :key="item.id || `${item.timestamp}-${index}`" class="quick-log-row">
              <time>{{ item.date?.slice(5) }} {{ item.time }}</time>
              <b :class="`level-${item.level?.toLowerCase()}`">{{ item.level }}</b>
              <span>{{ item.msg }}</span>
            </div>
          </div>
        </div>

        <div v-else-if="modal === 'network'" class="modal-body check-grid">
          <div v-for="item in network" :key="item.id" class="check-card">
            <span class="check-dot" :class="item.state"></span>
            <span><strong>{{ item.name }}</strong><small>{{ item.detail }}</small></span>
            <button class="btn" :disabled="networkBusy[item.id]" @click="testNetwork(item)">重测</button>
          </div>
        </div>

        <div v-else-if="modal === 'health'" class="modal-body">
          <div v-if="healthBusy" class="empty">正在检查…</div>
          <div v-else class="check-grid">
            <div v-for="item in health" :key="item.id" class="check-card">
              <span class="check-dot" :class="item.ok ? 'ok' : 'error'"></span>
              <span><strong>{{ item.name }}</strong><small>{{ item.detail }}</small></span>
            </div>
          </div>
          <button class="btn health-refresh" :disabled="healthBusy" @click="loadHealth">重新检查</button>
        </div>

        <div v-else-if="modal === 'services'" class="modal-body jobs-list">
          <div v-if="!jobs.length" class="empty">暂无定时任务</div>
          <div v-for="job in jobs" :key="job.id" class="job-row">
            <span><small>{{ job.plugin }}</small><strong>{{ job.name }}</strong></span>
            <code>{{ job.next || '暂无计划' }}</code>
            <button class="btn" :disabled="job.running" @click="runJob(job)">{{ job.running ? '运行中' : '执行' }}</button>
          </div>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.control-center { position: relative; display: flex; align-items: center; gap: 7px; margin-left: auto; }
.control-btn, .avatar-btn {
  width: 38px; height: 38px; display: grid; place-items: center; position: relative;
  border: 1px solid transparent; border-radius: 10px; background: transparent;
  color: var(--text-secondary); cursor: pointer; transition: .16s ease;
}
.control-btn:hover, .control-btn.active { color: var(--text-primary); border-color: var(--border-light); background: var(--bg-hover); }
.control-btn svg { width: 19px; height: 19px; }
.avatar-btn { border-radius: 50%; overflow: hidden; color: #fff; font-size: 11px; font-weight: 700; background: linear-gradient(145deg, var(--accent), var(--accent-2)); }
.avatar-btn.active { box-shadow: 0 0 0 3px var(--accent-dim); }
.avatar-btn img, .avatar-large img { width: 100%; height: 100%; object-fit: cover; }
.notice-badge {
  position: absolute; right: -4px; top: -4px; min-width: 17px; height: 17px; padding: 0 4px;
  border: 2px solid var(--bg-base); border-radius: 99px; background: var(--danger);
  color: #fff; font-size: 9px; line-height: 13px;
}
.control-pop {
  position: absolute; z-index: 120; top: calc(100% + 12px); right: 0; width: 380px;
  border: 1px solid var(--border-light); border-radius: 14px; background: rgba(17,19,26,.98);
  box-shadow: var(--shadow-float); overflow: hidden; animation: pop-in .16s ease-out;
}
@keyframes pop-in { from { opacity: 0; transform: translateY(-7px) scale(.98); } }
.pop-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 18px; border-bottom: 1px solid var(--border); }
.pop-head strong { font-size: 16px; }
.pop-head button { width: 30px; height: 30px; border: 0; background: transparent; color: var(--text-secondary); cursor: pointer; font-size: 18px; }
.shortcut-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 13px; }
.shortcut-grid > button {
  min-height: 86px; display: flex; align-items: center; gap: 11px; padding: 13px;
  border: 1px solid var(--border); border-radius: 11px; background: var(--bg-card);
  color: var(--text-primary); text-align: left; cursor: pointer;
}
.shortcut-grid > button:hover { border-color: var(--accent); background: var(--accent-dim); transform: translateY(-1px); }
.shortcut-grid i { width: 34px; height: 34px; display: grid; place-items: center; flex: 0 0 34px; border-radius: 9px; background: var(--accent-dim); color: var(--accent); font-style: normal; font-size: 18px; }
.shortcut-grid strong, .shortcut-grid small { display: block; }
.shortcut-grid small { margin-top: 3px; color: var(--text-muted); font-size: 11px; }
.notifications-pop { width: 420px; }
.notice-list { max-height: 520px; overflow-y: auto; padding: 8px; }
.notice-item {
  width: 100%; display: grid; grid-template-columns: 38px 1fr; gap: 11px; padding: 12px;
  border: 0; border-radius: 11px; background: transparent; color: var(--text-primary); text-align: left; cursor: pointer;
}
.notice-item:hover { background: var(--bg-hover); }
.notice-item.unread { background: var(--accent-dim); }
.notice-icon { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 10px; background: var(--bg-elevated); color: var(--accent); font-weight: 700; }
.notice-body strong, .notice-body small, .notice-text { display: block; }
.notice-text { margin: 4px 0; color: var(--text-secondary); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.notice-item.expanded .notice-text { white-space: pre-wrap; overflow: visible; word-break: break-word; }
.notice-body small { color: var(--text-muted); }
.user-pop { width: 280px; }
.user-profile { display: flex; align-items: center; gap: 12px; padding: 18px; border-bottom: 1px solid var(--border); }
.avatar-large { width: 58px; height: 58px; position: relative; padding: 0; overflow: hidden; flex: 0 0 58px; border: 1px solid var(--accent); border-radius: 13px; background: linear-gradient(145deg, var(--accent), var(--accent-2)); color: #fff; cursor: pointer; }
.avatar-large i { position: absolute; inset: auto 0 0; padding: 2px; background: rgba(0,0,0,.7); font-size: 9px; font-style: normal; }
.user-profile small, .user-profile strong { display: block; }
.user-profile small { color: var(--accent); }
.user-menu { display: grid; padding: 8px; }
.user-menu button { padding: 11px 12px; border: 0; border-radius: 9px; background: transparent; color: var(--text-primary); text-align: left; cursor: pointer; }
.user-menu button:hover { background: var(--bg-hover); }
.user-menu .danger { color: var(--danger); }
.user-status { display: flex; align-items: center; gap: 7px; padding: 12px 18px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: 11px; }
.user-status > span { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); }
.user-status > span.online { background: var(--success); box-shadow: 0 0 7px var(--success); }
.user-status b { margin-left: auto; font-family: monospace; font-weight: 400; }
.empty { padding: 30px; color: var(--text-muted); text-align: center; }
</style>

<style>
.control-modal-mask {
  position: fixed; z-index: 300; inset: 0; display: grid; place-items: center; padding: 24px;
  background: rgba(4,7,12,.76); backdrop-filter: blur(10px);
}
.control-modal {
  width: min(1040px, 94vw); max-height: 88vh; overflow: hidden; display: flex; flex-direction: column;
  border: 1px solid var(--border-light); border-radius: 16px; background: var(--bg-card); box-shadow: var(--shadow-float);
}
.control-modal > header { display: flex; align-items: center; justify-content: space-between; padding: 18px 22px; border-bottom: 1px solid var(--border); }
.control-modal > header strong { font-size: 18px; }
.control-modal > header button { width: 34px; height: 34px; border: 0; border-radius: 9px; background: transparent; color: var(--text-secondary); font-size: 23px; cursor: pointer; }
.control-modal > header button:hover { background: var(--bg-hover); color: var(--text-primary); }
.control-modal .modal-body { overflow-y: auto; padding: 18px 22px 22px; }
.log-toolbar { display: flex; align-items: center; gap: 9px; margin-bottom: 14px; }
.log-toolbar .select { width: 130px; }
.log-toolbar .input { flex: 1; }
.live-state { color: var(--text-muted); font-size: 12px; }
.live-state.on { color: var(--success); }
.quick-log-list { display: grid; gap: 7px; font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }
.quick-log-row { display: grid; grid-template-columns: 120px 72px 1fr; gap: 10px; padding: 11px 12px; border: 1px solid var(--border); border-radius: 9px; background: #080b12; }
.quick-log-row time { color: var(--text-muted); }
.quick-log-row b { color: var(--accent); }
.quick-log-row .level-warning { color: var(--warning); }
.quick-log-row .level-error, .quick-log-row .level-critical { color: var(--danger); }
.quick-log-row span { word-break: break-word; white-space: pre-wrap; }
.check-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.check-card { min-height: 72px; display: grid; grid-template-columns: 10px 1fr auto; align-items: center; gap: 12px; padding: 14px; border: 1px solid var(--border); border-radius: 11px; background: var(--bg-elevated); }
.check-card strong, .check-card small { display: block; }
.check-card small { margin-top: 3px; color: var(--text-secondary); }
.check-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--text-muted); }
.check-dot.testing { background: var(--warning); animation: quick-pulse 1s infinite; }
.check-dot.ok { background: var(--success); box-shadow: 0 0 8px var(--success); }
.check-dot.error { background: var(--danger); box-shadow: 0 0 8px var(--danger); }
@keyframes quick-pulse { 50% { opacity: .35; } }
.health-refresh { margin-top: 14px; }
.jobs-list { display: grid; gap: 8px; }
.job-row { display: grid; grid-template-columns: minmax(180px, 1fr) 170px auto; align-items: center; gap: 14px; padding: 13px 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-elevated); }
.job-row small, .job-row strong { display: block; }
.job-row small { color: var(--text-muted); }
.job-row code { color: var(--text-secondary); }
@media (max-width: 768px) {
  .control-center { gap: 3px; }
  .control-btn, .avatar-btn { width: 34px; height: 34px; }
  .control-pop { position: fixed; top: 56px; right: 8px; width: calc(100vw - 16px); max-height: calc(100dvh - 132px); }
  .notifications-pop { width: calc(100vw - 16px); }
  .control-modal-mask { padding: 0; }
  .control-modal { width: 100vw; max-height: 100dvh; height: 100dvh; border-radius: 0; border: 0; }
  .control-modal .modal-body { padding: 14px; }
  .log-toolbar { flex-wrap: wrap; }
  .log-toolbar .input { min-width: 100%; order: 3; }
  .quick-log-row { grid-template-columns: 1fr; gap: 3px; }
  .check-grid { grid-template-columns: 1fr; }
  .job-row { grid-template-columns: 1fr auto; }
  .job-row code { grid-column: 1; }
}
</style>
