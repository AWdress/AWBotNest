<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, getToken } from '../api'
import { toast } from '../composables/toast'
import { uiProfile } from '../composables/uiProfile'
import logoWhite from '../assets/logo-white.png'

const props = defineProps({
  online: Boolean,
  version: { type: String, default: '' },
  latestVersion: { type: String, default: '' },
  checkReleases: { type: Function, default: null },
  restarting: Boolean,
})
const emit = defineEmits(['restart', 'logout'])
const router = useRouter()

const panel = ref('')
const modal = ref('')
const profile = uiProfile
const notices = ref([])
const unread = ref(0)
const expandedNotice = ref('')
const notificationAction = ref('')
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
const about = ref(null)
const aboutBusy = ref(false)
const selectedVersion = ref(null)
const releaseNoteHtml = ref('')
let logsWs = null
let logsReconnect = null
let noticeTimer = null
let jobsTimer = null
let notificationLoadVersion = 0
const seenLogs = new Set()
const quickLogsBox = ref(null)
const modalDialog = ref(null)

const modalTitle = computed(() => ({
  logs: '实时日志',
  network: '网络测试',
  health: '系统健康检查',
  services: '定时服务',
  about: '关于 AWBotNest',
}[modal.value] || ''))

const filteredLogs = computed(() => logs.value.filter(item => {
  if (logLevel.value !== 'ALL' && item.level !== logLevel.value) return false
  const term = logSearch.value.trim().toLowerCase()
  return !term || `${item.source || ''} ${item.msg || ''}`.toLowerCase().includes(term)
}))

function togglePanel(name) {
  panel.value = panel.value === name ? '' : name
  if (panel.value === 'notifications') loadNotifications()
}

function closePanels(event) {
  if (!event?.target?.closest?.('.control-center')) panel.value = ''
}

async function loadNotifications() {
  const loadVersion = ++notificationLoadVersion
  try {
    const result = await api.getNotifications()
    if (loadVersion !== notificationLoadVersion || notificationAction.value) return
    notices.value = result.notifications || []
    unread.value = result.unread || 0
  } catch {}
}

async function markAllRead() {
  if (notificationAction.value) return
  if (!unread.value) {
    toast.info('当前没有未读通知')
    return
  }
  const count = unread.value
  notificationAction.value = 'read'
  notificationLoadVersion += 1
  try {
    await api.readNotifications()
    unread.value = 0
    notices.value.forEach(item => { item.unread = false })
    toast.success(`已将 ${count} 条通知标记为已读`)
  } catch (error) {
    toast.error(`标记已读失败：${error.message}`)
  } finally {
    notificationAction.value = ''
  }
}

async function clearNotifications() {
  if (notificationAction.value) return
  if (!notices.value.length) {
    toast.info('通知中心已经是空的')
    return
  }
  notificationAction.value = 'clear'
  notificationLoadVersion += 1
  try {
    await api.clearNotifications()
    notices.value = []
    unread.value = 0
    expandedNotice.value = ''
    toast.success('通知已全部清空')
  } catch (error) {
    toast.error(`清空通知失败：${error.message}`)
  } finally {
    notificationAction.value = ''
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

function useFallbackNoticeIcon(event) {
  const image = event?.currentTarget
  if (!image || image.dataset.fallbackApplied === '1') return
  image.dataset.fallbackApplied = '1'
  image.src = logoWhite
  image.closest('.notice-icon')?.classList.add('fallback')
}

function openModal(name) {
  panel.value = ''
  modal.value = name
  nextTick(() => modalDialog.value?.focus())
  if (name === 'logs') {
    logPaused.value = false
    logs.value = []
    seenLogs.clear()
    connectLogs()
  }
  if (name === 'network') loadNetwork()
  if (name === 'health') loadHealth()
  if (name === 'about') loadAbout()
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
  selectedVersion.value = null
  modal.value = ''
}

function logsWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/api/logs/ws?token=${encodeURIComponent(getToken())}&batch_history=1`
}

function scrollQuickLogsToLatest() {
  if (quickLogsBox.value) quickLogsBox.value.scrollTop = 0
}

function connectLogs() {
  disconnectLogs()
  logsWs = new WebSocket(logsWsUrl())
  logsWs.onopen = () => { logConnected.value = true }
  logsWs.onmessage = event => {
    if (logPaused.value) return
    try {
      const item = JSON.parse(event.data)
      if (item.type === 'history' && Array.isArray(item.logs)) {
        logs.value = item.logs.slice(0, 1000)
        seenLogs.clear()
        logs.value.forEach(entry => seenLogs.add(
          entry.id || `${entry.timestamp}|${entry.level}|${entry.source}|${entry.msg}`,
        ))
        nextTick(scrollQuickLogsToLatest)
        return
      }
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
      nextTick(scrollQuickLogsToLatest)
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

async function loadAbout() {
  aboutBusy.value = true
  selectedVersion.value = null
  try {
    const [info, releases] = await Promise.all([
      api.getAbout(),
      props.checkReleases?.(true),
    ])
    if (Array.isArray(releases) && releases.length) {
      info.versions = releases.map(item => ({
        ...item,
        current: item.version === String(info.version || '').replace(/^v/i, ''),
      }))
      info.latest_version = releases[0].version
      info.version_source = 'github'
    }
    about.value = info
  } catch (error) {
    toast.error(`关于信息加载失败：${error.message}`)
  } finally {
    aboutBusy.value = false
  }
}

function formatUptime(seconds) {
  const total = Math.max(0, Number(seconds) || 0)
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  return [days ? `${days}天` : '', hours ? `${hours}小时` : '', `${minutes}分钟`]
    .filter(Boolean).join(' ')
}

async function openVersion(item) {
  releaseNoteHtml.value = ''
  selectedVersion.value = { ...item, notes: '', loading: true }
  try {
    const result = Object.prototype.hasOwnProperty.call(item, 'notes')
      ? item
      : await api.getAboutVersion(item.version)
    const [{ marked }, { default: DOMPurify }] = await Promise.all([
      import('marked'),
      import('dompurify'),
    ])
    releaseNoteHtml.value = DOMPurify.sanitize(
      marked.parse(result.notes || '', { gfm: true, breaks: false }),
    )
    selectedVersion.value = { ...result, loading: false }
  } catch (error) {
    selectedVersion.value = { ...item, notes: `读取失败：${error.message}`, loading: false }
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
    <button class="profile-trigger" :class="{ active: panel === 'user' }"
            title="管理员菜单" @click="togglePanel('user')">
      <span class="avatar-btn">
        <img v-if="profile.avatar_url" :src="profile.avatar_url" alt="管理员头像">
        <span v-else>{{ profile.username.slice(0, 2).toUpperCase() }}</span>
      </span>
      <span class="profile-trigger-copy"><strong>{{ profile.username }}</strong><small>管理员</small></span>
      <svg class="profile-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="m7 10 5 5 5-5"/>
      </svg>
    </button>

    <div v-if="panel === 'shortcuts'" class="control-pop shortcuts-pop">
      <div class="pop-head"><strong>快捷入口</strong><button @click="panel=''">×</button></div>
      <div class="shortcut-grid">
        <button @click.stop="openModal('logs')"><i>▤</i><span><strong>实时日志</strong><small>查看最新运行记录</small></span></button>
        <button @click.stop="openModal('network')"><i>⌁</i><span><strong>网络测试</strong><small>检查外部服务连接</small></span></button>
        <button @click.stop="openModal('health')"><i>✦</i><span><strong>系统健康检查</strong><small>检查核心服务</small></span></button>
        <button @click.stop="openModal('services')"><i>◷</i><span><strong>定时服务</strong><small>查看和执行定时任务</small></span></button>
      </div>
    </div>

    <div v-if="panel === 'notifications'" class="control-pop notifications-pop">
      <div class="pop-head">
        <strong>通知中心</strong>
        <span class="notice-actions">
          <button class="read-action" :class="{ busy: notificationAction === 'read' }"
                  :disabled="Boolean(notificationAction)" title="全部标记为已读"
                  aria-label="全部标记为已读" @click.stop="markAllRead">
            <span v-if="notificationAction === 'read'">…</span>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m5 12 4 4L19 6"/>
            </svg>
          </button>
          <button class="clear-action" :class="{ busy: notificationAction === 'clear' }"
                  :disabled="Boolean(notificationAction)" title="清空全部通知"
                  aria-label="清空全部通知" @click.stop="clearNotifications">
            <span v-if="notificationAction === 'clear'">…</span>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M4 7h16M9 7V4h6v3m-9 0 1 13h10l1-13M10 11v5m4-5v5"/>
            </svg>
          </button>
        </span>
      </div>
      <div class="notice-list">
        <div v-if="!notices.length" class="empty">暂无通知</div>
        <button v-for="item in notices" :key="noticeKey(item)"
                class="notice-item" :class="{ unread: item.unread, expanded: expandedNotice === noticeKey(item) }"
                @click="expandedNotice = expandedNotice === noticeKey(item) ? '' : noticeKey(item)">
          <span class="notice-icon" :class="[`level-${item.level}`, { fallback: !item.plugin_icon }]">
            <img :src="item.plugin_icon || logoWhite" :alt="`${item.plugin_name || '系统'}图标`"
                 @error="useFallbackNoticeIcon">
          </span>
          <span class="notice-body">
            <strong>{{ item.plugin_name || '系统通知' }}</strong>
            <span class="notice-text">{{ item.text }}</span>
            <small>{{ item.category || '系统消息' }} · {{ relativeTime(item.t) }}</small>
          </span>
        </button>
      </div>
    </div>

    <div v-if="panel === 'user'" class="control-pop user-pop">
      <div class="user-profile">
        <div class="avatar-large">
          <img v-if="profile.avatar_url" :src="profile.avatar_url" alt="管理员头像">
          <span v-else>{{ profile.username.slice(0, 2).toUpperCase() }}</span>
        </div>
        <div><small>管理员</small><strong>{{ profile.username }}</strong></div>
      </div>
      <div class="user-menu">
        <button @click="goSettings">个人信息</button>
        <button @click.stop="openModal('about')">关于 AWBotNest</button>
        <button :disabled="restarting" @click="panel=''; emit('restart')">{{ restarting ? '重启中…' : '重启' }}</button>
        <button class="danger" @click="emit('logout')">退出登录</button>
      </div>
      <div class="user-status"><span :class="{ online }"></span>{{ online ? '连接正常' : '正在连接' }}<b v-if="version">v{{ version }}</b></div>
    </div>
  </div>

  <Teleport to="body">
    <div v-if="modal" class="control-modal-mask" @click.self="closeModal">
      <section ref="modalDialog" class="control-modal" role="dialog" aria-modal="true"
               :aria-label="modalTitle" tabindex="-1">
        <header><strong>{{ modalTitle }}</strong><button aria-label="关闭" @click="closeModal">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button></header>

        <div v-if="modal === 'logs'" class="modal-body logs-modal" ref="quickLogsBox">
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

        <div v-else-if="modal === 'about'" class="modal-body about-modal">
          <div v-if="aboutBusy" class="empty">正在读取信息…</div>
          <template v-else-if="about">
            <section class="about-hero">
              <div class="about-mark">
                <img :src="logoWhite" alt="AWBotNest Logo">
              </div>
              <div>
                <h2>{{ about.name }}</h2>
                <p>插件化机器人</p>
              </div>
              <span class="about-current">v{{ about.version }}</span>
            </section>

            <section class="about-info-grid">
              <div><span>版本</span><strong>v{{ about.version }}</strong></div>
              <div><span>Python</span><strong>{{ about.python }}</strong></div>
              <div><span>运行系统</span><strong>{{ about.platform }}</strong></div>
              <div><span>已运行</span><strong>{{ formatUptime(about.uptime_seconds) }}</strong></div>
            </section>

            <section class="about-section">
              <h3>支持</h3>
              <div class="about-links">
                <a :href="about.repository" target="_blank" rel="noopener noreferrer">项目仓库</a>
                <a :href="about.issues" target="_blank" rel="noopener noreferrer">问题反馈</a>
                <a :href="about.docs" target="_blank" rel="noopener noreferrer">使用文档</a>
              </div>
            </section>

            <section class="about-section">
              <div class="version-heading">
                <h3>版本历史</h3>
                <span :class="['version-source', about.version_source]">
                  {{ about.version_source === 'github' ? 'GitHub Releases' : '本地记录' }}
                </span>
              </div>
              <div class="version-list">
                <div v-for="item in about.versions" :key="item.version" class="version-row">
                  <div>
                    <strong>v{{ item.version }}</strong>
                    <span v-if="item.version === (latestVersion || about.latest_version)" class="latest-badge">最新版本</span>
                    <span v-if="item.current" class="current-badge">当前版本</span>
                  </div>
                  <button class="btn" @click="openVersion(item)">查看更新内容</button>
                </div>
              </div>
            </section>
          </template>
        </div>
      </section>

      <div v-if="selectedVersion" class="release-note-mask" @click.self="selectedVersion = null">
        <article class="release-note">
          <header>
            <strong>v{{ selectedVersion.version }} 更新内容</strong>
            <button @click="selectedVersion = null">×</button>
          </header>
          <div v-if="selectedVersion.loading" class="release-note-state">正在读取更新内容…</div>
          <div v-else-if="selectedVersion.notes" class="release-note-content" v-html="releaseNoteHtml"></div>
          <div v-else class="release-note-state">这个版本暂时没有更新说明。</div>
        </article>
      </div>
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
.profile-trigger {
  min-width: 118px; min-height: 42px; display: flex; align-items: center; gap: 9px; padding: 2px 5px 2px 2px;
  border: 1px solid transparent; border-radius: 12px; background: transparent; color: var(--text-primary);
  font: inherit; text-align: left; cursor: pointer; transition: background .16s ease, border-color .16s ease;
}
.profile-trigger:hover, .profile-trigger.active { border-color: var(--border-light); background: var(--bg-hover); }
.profile-trigger .avatar-btn { width: 38px; height: 38px; flex: 0 0 38px; pointer-events: none; }
.profile-trigger-copy { min-width: 0; display: flex; flex: 1; flex-direction: column; }
.profile-trigger-copy strong { max-width: 94px; overflow: hidden; color: var(--text-primary); font-size: 12px; line-height: 1.25; text-overflow: ellipsis; white-space: nowrap; }
.profile-trigger-copy small { color: var(--text-muted); font-size: 9px; line-height: 1.25; }
.profile-chevron { width: 14px; height: 14px; flex: 0 0 14px; color: var(--text-muted); transition: transform .16s ease; }
.profile-trigger.active .profile-chevron { transform: rotate(180deg); }
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
.pop-head button {
  width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid transparent;
  border-radius: 9px; background: transparent; color: var(--text-secondary); cursor: pointer;
  font-size: 18px; transition: color .16s ease, background .16s ease, border-color .16s ease, transform .1s ease;
}
.pop-head button:hover { color: var(--text-primary); border-color: var(--border-light); background: var(--bg-hover); }
.pop-head button:active { transform: scale(.9); }
.pop-head button:disabled { cursor: wait; opacity: .7; }
.pop-head button svg { width: 18px; height: 18px; }
.notice-actions { display: flex; align-items: center; gap: 5px; }
.notice-actions .read-action:hover, .notice-actions .read-action.busy { color: var(--success); border-color: var(--success); background: rgba(16,185,129,.13); }
.notice-actions .clear-action:hover, .notice-actions .clear-action.busy { color: var(--danger); border-color: var(--danger); background: rgba(239,68,68,.13); }
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
.notice-list { max-height: 520px; overflow-y: auto; padding: 10px 14px 14px; scrollbar-gutter: stable; }
.notice-item {
  width: 100%; display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 11px; padding: 12px;
  border: 0; border-radius: 11px; background: transparent; color: var(--text-primary); text-align: left; cursor: pointer;
}
.notice-item:hover { background: var(--bg-hover); }
.notice-item.unread { background: var(--accent-dim); }
.notice-icon { width: 42px; height: 42px; flex: 0 0 42px; display: grid; place-items: center; overflow: hidden;
  border: 1px solid rgba(82,142,255,.18); border-radius: 12px; background: var(--bg-elevated); }
.notice-icon img { width: 100%; height: 100%; object-fit: contain; }
.notice-icon.fallback img { width: 28px; height: 28px; }
.notice-icon.level-success { border-color: rgba(20,184,135,.28); }
.notice-icon.level-warning { border-color: rgba(245,166,35,.3); }
.notice-icon.level-error { border-color: rgba(245,82,99,.32); }
.notice-body { min-width: 0; padding-right: 4px; }
.notice-body strong, .notice-body small, .notice-text { display: block; }
.notice-text { margin: 4px 0; color: var(--text-secondary); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.notice-item.expanded .notice-text { white-space: pre-wrap; overflow: visible; overflow-wrap: anywhere; word-break: break-word; }
.notice-body small { color: var(--text-muted); }
.user-pop { width: 280px; }
.user-profile { display: flex; align-items: center; gap: 12px; padding: 18px; border-bottom: 1px solid var(--border); }
.avatar-large { width: 58px; height: 58px; display: grid; place-items: center; overflow: hidden; flex: 0 0 58px; border: 1px solid var(--accent); border-radius: 13px; background: linear-gradient(145deg, var(--accent), var(--accent-2)); color: #fff; font-weight: 700; }
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
  position: relative;
  border: 1px solid var(--border-light); border-radius: 16px; background: var(--bg-card); box-shadow: var(--shadow-float);
}
.control-modal > header { display: flex; align-items: center; justify-content: space-between; padding: 18px 22px; border-bottom: 1px solid var(--border); }
.control-modal > header strong { font-size: 18px; }
.control-modal > header button { width: 34px; height: 34px; border: 0; border-radius: 9px; background: transparent; color: var(--text-secondary); font-size: 23px; cursor: pointer; }
.control-modal > header button svg { width: 20px; height: 20px; }
.control-modal > header button:hover { background: var(--bg-hover); color: var(--text-primary); }
.control-modal .modal-body { overflow-y: auto; padding: 18px 22px 22px; }
.control-modal .logs-modal { overflow-anchor: none; }
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
.about-modal { display: flex; flex-direction: column; gap: 24px; }
.about-hero {
  display: flex; align-items: center; gap: 14px; padding: 18px;
  border: 1px solid var(--border); border-radius: 14px;
  background: linear-gradient(135deg, var(--accent-dim), var(--bg-elevated) 58%, rgba(16,176,128,.08));
}
.about-mark {
  width: 58px; height: 58px; flex: 0 0 58px; display: grid; place-items: center;
  border-radius: 16px; background: linear-gradient(145deg, var(--accent), var(--accent-2));
  overflow: hidden; box-shadow: 0 10px 24px rgba(47,128,237,.22);
}
.about-mark img { width: 46px; height: 46px; object-fit: contain; }
.about-hero h2 { margin: 0; font-size: 22px; }
.about-hero p { margin: 4px 0 0; color: var(--text-secondary); }
.about-current { margin-left: auto; padding: 6px 11px; border-radius: 99px; background: var(--accent-dim); color: var(--accent); font-weight: 700; }
.about-info-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.about-info-grid > div { padding: 14px; border: 1px solid var(--border); border-radius: 11px; background: var(--bg-elevated); }
.about-info-grid span, .about-info-grid strong { display: block; }
.about-info-grid span { color: var(--text-muted); font-size: 11px; }
.about-info-grid strong { margin-top: 6px; overflow: hidden; color: var(--text-primary); text-overflow: ellipsis; white-space: nowrap; }
.about-section h3 { margin: 0 0 12px; padding-bottom: 10px; border-bottom: 1px solid var(--border); font-size: 17px; }
.about-links { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.about-links a { padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-elevated); color: var(--accent); text-align: center; }
.about-links a:hover { border-color: var(--accent); background: var(--accent-dim); }
.version-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid var(--border); }
.version-heading h3 { margin: 0; padding: 0; border: 0; }
.version-source { padding: 3px 8px; border-radius: 99px; background: var(--bg-elevated); color: var(--text-muted); font-size: 10px; }
.version-source.github { background: var(--accent-dim); color: var(--accent); }
.version-list { display: grid; gap: 8px; max-height: 330px; overflow-y: auto; }
.version-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 13px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-elevated); }
.version-row > div { display: flex; align-items: center; gap: 9px; }
.latest-badge { padding: 3px 7px; border-radius: 99px; background: var(--accent-dim); color: var(--accent); font-size: 10px; }
.current-badge { padding: 3px 7px; border-radius: 99px; background: rgba(16,176,128,.14); color: var(--success); font-size: 10px; }
.release-note-mask { position: fixed; z-index: 340; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(2,4,8,.72); }
.release-note { width: min(680px, 92vw); max-height: 76vh; overflow: hidden; border: 1px solid var(--border-light); border-radius: 15px; background: var(--bg-card); box-shadow: var(--shadow-float); }
.release-note header { display: flex; align-items: center; justify-content: space-between; padding: 17px 20px; border-bottom: 1px solid var(--border); }
.release-note header button { width: 32px; height: 32px; border: 0; background: transparent; color: var(--text-secondary); font-size: 22px; cursor: pointer; }
.release-note-state, .release-note-content { max-height: calc(76vh - 68px); padding: 20px 22px; overflow: auto; color: var(--text-secondary); line-height: 1.75; }
.release-note-state { min-height: 110px; }
.release-note-content { overflow-wrap: anywhere; }
.release-note-content :deep(> :first-child) { margin-top: 0; }
.release-note-content :deep(> :last-child) { margin-bottom: 0; }
.release-note-content :deep(h1),
.release-note-content :deep(h2),
.release-note-content :deep(h3) { margin: 22px 0 10px; color: var(--text-primary); line-height: 1.35; }
.release-note-content :deep(h1) { padding-bottom: 9px; border-bottom: 1px solid var(--border); font-size: 22px; }
.release-note-content :deep(h2) { font-size: 18px; }
.release-note-content :deep(h3) { font-size: 16px; }
.release-note-content :deep(p) { margin: 10px 0; }
.release-note-content :deep(ul),
.release-note-content :deep(ol) { margin: 8px 0 16px; padding-left: 25px; }
.release-note-content :deep(li) { margin: 5px 0; padding-left: 2px; }
.release-note-content :deep(strong) { color: var(--text-primary); }
.release-note-content :deep(a) { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }
.release-note-content :deep(code) { padding: 2px 6px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-elevated); color: var(--text-primary); font-family: var(--font-mono); font-size: .9em; }
.release-note-content :deep(pre) { margin: 12px 0; padding: 13px 15px; overflow-x: auto; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-base); }
.release-note-content :deep(pre code) { padding: 0; border: 0; background: transparent; }
.release-note-content :deep(blockquote) { margin: 12px 0; padding: 10px 14px; border: 1px solid var(--border-light); border-radius: 9px; background: var(--bg-elevated); color: var(--text-muted); }
.release-note-content :deep(hr) { margin: 20px 0; border: 0; border-top: 1px solid var(--border); }
.release-note-content :deep(table) { width: 100%; margin: 12px 0; border-collapse: collapse; }
.release-note-content :deep(th),
.release-note-content :deep(td) { padding: 8px 10px; border: 1px solid var(--border); text-align: left; }
@media (max-width: 768px) {
  .control-center { gap: 3px; }
  .control-btn, .avatar-btn { width: 34px; height: 34px; }
  .control-center > .profile-trigger {
    width: 34px; min-width: 34px; max-width: 34px; min-height: 34px; padding: 0;
    flex: 0 0 34px; border-radius: 50%;
  }
  .control-center > .profile-trigger .avatar-btn { width: 34px; height: 34px; flex-basis: 34px; }
  .profile-trigger .profile-trigger-copy, .profile-trigger .profile-chevron { display: none; }
  .control-pop { position: fixed; top: 56px; right: 8px; width: calc(100vw - 16px); max-width: calc(100vw - 16px); max-height: calc(100dvh - 132px); box-sizing: border-box; }
  .notifications-pop { width: calc(100vw - 16px); max-width: calc(100vw - 16px); box-sizing: border-box; }
  .notice-content { word-break: break-word; overflow-wrap: anywhere; }
  .control-modal-mask { padding: 0; }
  .control-modal { width: 100vw; max-height: 100dvh; height: 100dvh; border-radius: 0; border: 0; }
  .control-modal .modal-body { padding: 14px; }
  .log-toolbar { flex-wrap: wrap; }
  .log-toolbar .input { min-width: 100%; order: 3; }
  .quick-log-row { grid-template-columns: 1fr; gap: 3px; }
  .check-grid { grid-template-columns: 1fr; }
  .job-row { grid-template-columns: 1fr auto; }
  .job-row code { grid-column: 1; }
  .about-info-grid { grid-template-columns: 1fr 1fr; }
  .about-links { grid-template-columns: 1fr; }
  .about-hero { align-items: flex-start; flex-wrap: wrap; }
  .about-current { margin-left: 0; }
  .version-row { align-items: flex-start; flex-direction: column; }
  .version-row .btn { width: 100%; }
}
</style>
