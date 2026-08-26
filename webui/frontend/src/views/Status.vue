<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, getToken } from '../api'
import AccountAvatar from '../components/AccountAvatar.vue'
import AccountPremiumBadge from '../components/AccountPremiumBadge.vue'
import {
  platformStatus,
  platformStatusError,
  platformStatusLoading,
  refreshPlatformStatus,
} from '../composables/platformStatus'

const st = ref(null)
const error = ref('')
const pageReady = ref(false)
const hoveredPoint = ref(null)
const changedAccounts = ref([])
const changedJobs = ref([])
const events = ref([])
const eventConnected = ref(false)
const activityRange = ref('24h')
const animated = ref({ user: 0, plugin: 0, uptime: 0, activity: 0 })
const currentTime = ref(Date.now())

const loading = computed(() => platformStatusLoading.value || !platformStatus.value)
const animationFrames = new Map()
let changeTimer = null
let firstPaintFrame = null
let eventSocket = null
let eventReconnectTimer = null
let clockTimer = null
let progressTimer = null

const reduceMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

function animateNumber(key, target, duration = 650) {
  const next = Number(target) || 0
  const previousFrame = animationFrames.get(key)
  if (previousFrame) cancelAnimationFrame(previousFrame)
  if (reduceMotion()) {
    animated.value[key] = next
    return
  }
  const from = Number(animated.value[key]) || 0
  const started = performance.now()
  const tick = (now) => {
    const progress = Math.min(1, (now - started) / duration)
    const eased = 1 - Math.pow(1 - progress, 3)
    animated.value[key] = Math.round(from + (next - from) * eased)
    if (progress < 1) animationFrames.set(key, requestAnimationFrame(tick))
    else animationFrames.delete(key)
  }
  animationFrames.set(key, requestAnimationFrame(tick))
}

function activityTotal(data) {
  return Object.values(data?.activity?.totals || {}).reduce((sum, count) => sum + count, 0)
}

function markChangedRows(previous, next) {
  if (!previous) return
  const oldAccounts = new Map((previous.accounts || []).map(account => [account.session, account]))
  changedAccounts.value = (next.accounts || [])
    .filter(account => {
      const old = oldAccounts.get(account.session)
      return !old || old.online !== account.online || old.name !== account.name
        || old.tgid !== account.tgid || old.is_premium !== account.is_premium
    })
    .map(account => account.session)

  const oldJobs = new Map((previous.scheduler_jobs || []).map(job => [job.id, job]))
  changedJobs.value = (next.scheduler_jobs || [])
    .filter(job => {
      const old = oldJobs.get(job.id)
      return !old || old.next !== job.next || old.running !== job.running || old.name !== job.name
    })
    .map(job => job.id)

  clearTimeout(changeTimer)
  changeTimer = setTimeout(() => {
    changedAccounts.value = []
    changedJobs.value = []
  }, 1400)
}

async function applyStatus(next) {
  if (!next) return
  const previous = st.value
  markChangedRows(previous, next)
  st.value = next
  error.value = ''
  animateNumber('user', next.user_count)
  animateNumber('plugin', next.plugins?.enabled)
  animateNumber('uptime', next.uptime_seconds, 500)
  animateNumber('activity', activityTotal(next))

  if (!previous) {
    await nextTick()
    firstPaintFrame = requestAnimationFrame(() => { pageReady.value = true })
  }
}

watch(platformStatus, applyStatus, { immediate: true })
watch(platformStatusError, message => { error.value = message || '' }, { immediate: true })
watch(
  () => Boolean(st.value?.scheduler_jobs?.some(job => job.running)),
  (running) => {
    clearInterval(progressTimer)
    progressTimer = running
      ? window.setInterval(() => refreshPlatformStatus(true).catch(() => {}), 2000)
      : null
  },
)

function formatUptime(seconds) {
  let value = Number(seconds) || 0
  const days = Math.floor(value / 86400)
  value %= 86400
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  const parts = []
  if (days) parts.push(`${days}天`)
  if (hours) parts.push(`${hours}时`)
  parts.push(`${minutes}分`)
  return parts.join('')
}

const uptime = computed(() => formatUptime(animated.value.uptime))
const activityData = computed(() => activityRange.value === '7d' ? st.value?.activity_7d : st.value?.activity)
const activityRangeLabel = computed(() => activityRange.value === '7d' ? '近 7 天' : '近 24 小时')
watch(activityData, data => animateNumber('activity', activityTotal(data)))

function formatMemory(value) {
  const amount = Number(value)
  if (!Number.isFinite(amount) || amount <= 0) return '—'
  if (amount >= 1024) return `${(amount / 1024).toFixed(1)} GB`
  return `${Math.round(amount)} MB`
}

function prettyTrigger(trigger) {
  if (!trigger) return '未设置'
  const match = /^(\w+)\[(.*)\]$/.exec(trigger)
  if (!match) return trigger
  const [, kind, body] = match
  if (kind === 'interval') {
    const [hours, minutes, seconds] = body.split(':').map(Number)
    const total = (hours || 0) * 3600 + (minutes || 0) * 60 + (seconds || 0)
    if (total && total % 86400 === 0) return `每 ${total / 86400} 天`
    if (total && total % 3600 === 0) return `每 ${total / 3600} 小时`
    if (total && total % 60 === 0) return `每 ${total / 60} 分钟`
    return `每 ${total} 秒`
  }
  if (kind === 'cron') return prettyCron(body)
  if (kind === 'date') return `单次 ${body.split(' ').slice(0, 2).join(' ')}`
  return trigger
}

const WEEKDAYS = {
  '0': '周一', '1': '周二', '2': '周三', '3': '周四', '4': '周五', '5': '周六', '6': '周日',
  mon: '周一', tue: '周二', wed: '周三', thu: '周四', fri: '周五', sat: '周六', sun: '周日',
}

function prettyCron(body) {
  const fields = {}
  for (const segment of body.split(',')) {
    const match = /(\w+)\s*=\s*'?([^',]+)'?/.exec(segment.trim())
    if (match) fields[match[1]] = match[2]
  }
  const { hour, minute, day_of_week: weekday, day, month } = fields
  const isNumber = value => value !== undefined && /^\d+$/.test(value)
  if (isNumber(hour) && isNumber(minute) && !weekday && !day && !month) {
    return `每天 ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  }
  if (isNumber(hour) && isNumber(minute) && weekday !== undefined) {
    return `每${WEEKDAYS[String(weekday).toLowerCase()] || `周${weekday}`} ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  }
  if (isNumber(minute) && hour === undefined && !weekday && !day) return `每小时 第${minute}分`
  const parts = []
  if (month) parts.push(`${month}月`)
  if (day) parts.push(`${day}日`)
  if (weekday !== undefined) parts.push(WEEKDAYS[String(weekday).toLowerCase()] || `周${weekday}`)
  if (hour !== undefined) parts.push(`${hour}时`)
  if (minute !== undefined) parts.push(`${minute}分`)
  return parts.length ? parts.join(' ') : '定时执行'
}

const JOB_NAMES = { log_cleaner: '日志清理', 插件仓库轮询: '插件仓库轮询' }
function jobName(job) { return JOB_NAMES[job.name] || JOB_NAMES[job.id] || job.name }

function isSameLocalDay(left, right) {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate()
}

function clockLabel(date) {
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function nextRunLabel(job) {
  if (job.running) {
    const progress = job.progress || {}
    return progress.status === 'running' && progress.step
      ? `${progress.step}${progress.progress != null ? ` · ${progress.progress}%` : ''}`
      : '运行中'
  }
  if (!job.next_run_at) return '等待安排'
  const nextRun = new Date(job.next_run_at)
  const remaining = nextRun.getTime() - currentTime.value
  if (!Number.isFinite(remaining)) return '等待安排'
  if (remaining <= 0) return '即将执行'
  if (remaining < 3600000) return `${Math.max(1, Math.ceil(remaining / 60000))}分钟后`

  const now = new Date(currentTime.value)
  if (isSameLocalDay(now, nextRun)) return `今天 ${clockLabel(nextRun)}`
  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  if (isSameLocalDay(tomorrow, nextRun)) return `明天 ${clockLabel(nextRun)}`
  return `${String(nextRun.getMonth() + 1).padStart(2, '0')}-${String(nextRun.getDate()).padStart(2, '0')} ${clockLabel(nextRun)}`
}

const cards = computed(() => {
  if (!st.value) return []
  const status = st.value
  return [
    {
      key: 'bot', label: 'Bot 账号', value: status.bot_connected ? '在线' : '离线',
      sub: status.bot_connected ? '连接稳定' : '等待连接', tone: status.bot_connected ? 'green' : 'gray', icon: 'bot',
    },
    {
      key: 'user', label: '在线用户账号', value: animated.value.user, sub: `共 ${(status.accounts || []).length} 个`,
      tone: status.user_count ? 'blue' : 'gray', icon: 'user',
    },
    {
      key: 'plugin', label: '已启用插件', value: animated.value.plugin, sub: `共 ${status.plugins?.total || 0} 个`,
      tone: status.plugins?.error ? 'amber' : 'green', icon: 'plug',
    },
    { key: 'uptime', label: '运行时长', value: uptime.value, sub: '本次启动', tone: 'teal', icon: 'clock', small: true },
  ]
})

const icons = {
  bot: 'M12 8V4H8M4 8h16v12H4zM2 14h2M20 14h2M9 13v2M15 13v2',
  user: 'M12 12a5 5 0 100-10 5 5 0 000 10zM4 21a8 8 0 0116 0',
  plug: 'M9 2v6M15 2v6M6 8h12v3a6 6 0 01-12 0zM12 17v5',
  clock: 'M12 22a10 10 0 100-20 10 10 0 000 20zM12 6v6l4 2',
}

const PALETTE = ['#3080f0', '#10b080', '#20b0d0', '#f0a020', '#e05070', '#8090f0', '#50c070']
const activePlugins = computed(() => {
  const totals = activityData.value?.totals || {}
  return Object.keys(totals).sort((a, b) => totals[b] - totals[a])
})
const nameOf = pluginId => st.value?.plugin_names?.[pluginId] || pluginId
const colorOf = pluginId => PALETTE[Math.max(0, activePlugins.value.indexOf(pluginId)) % PALETTE.length]

const timeline = computed(() => {
  const buckets = activityData.value?.buckets || []
  const totals = buckets.map(bucket => Object.values(bucket.counts || {}).reduce((sum, count) => sum + count, 0))
  const successes = buckets.map(bucket => Object.values(bucket.success_counts || {}).reduce((sum, count) => sum + count, 0))
  const max = Math.max(1, ...totals, ...successes)
  return buckets.map((bucket, index) => {
    const date = new Date(bucket.t * 1000)
    const x = buckets.length > 1 ? 28 + (index / (buckets.length - 1)) * 944 : 500
    const step = buckets.length > 1 ? 944 / (buckets.length - 1) : 1000
    const hitLeft = buckets.length === 1 || index === 0 ? 0 : x - step / 2
    const hitRight = buckets.length === 1 || index === buckets.length - 1 ? 1000 : x + step / 2
    const y = 18 + (1 - totals[index] / max) * 154
    const successY = 18 + (1 - successes[index] / max) * 154
    return {
      total: totals[index],
      success: successes[index],
      label: activityRange.value === '7d'
        ? `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`
        : `${String(date.getHours()).padStart(2, '0')}:00`,
      hour: date.getHours(),
      x,
      y,
      successY,
      xPct: `${(x / 1000) * 100}%`,
      hitLeftPct: `${(hitLeft / 1000) * 100}%`,
      hitWidthPct: `${((hitRight - hitLeft) / 1000) * 100}%`,
    }
  })
})

const linePath = computed(() => timeline.value.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' '))
const successLinePath = computed(() => timeline.value.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.successY}`).join(' '))
const areaPath = computed(() => {
  if (!timeline.value.length) return ''
  return `${linePath.value} L ${timeline.value.at(-1).x} 176 L ${timeline.value[0].x} 176 Z`
})
const chartMax = computed(() => Math.max(1, ...timeline.value.flatMap(point => [point.total, point.success])))
const chartTicks = computed(() => [1, .75, .5, .25, 0].map(ratio => Math.round(chartMax.value * ratio)))
const visibleAxisLabels = computed(() => activityRange.value === '7d'
  ? timeline.value
  : timeline.value.filter((point, index) => index === 0 || index === timeline.value.length - 1 || point.hour % 6 === 0))
const peakPoint = computed(() => timeline.value.reduce((peak, point) => point.total > (peak?.total || -1) ? point : peak, null))
const hoveredChartPoint = computed(() => hoveredPoint.value === null ? null : timeline.value[hoveredPoint.value])
const hoveredChartPointPosition = computed(() => {
  const point = hoveredChartPoint.value
  if (!point) return {}
  return {
    left: point.xPct,
    top: `${point.y}px`,
    '--chart-guide-top': `${point.y}px`,
  }
})
const hasActivity = computed(() => activePlugins.value.length > 0)
const topPlugins = computed(() => {
  const totals = activityData.value?.totals || {}
  const max = Math.max(1, ...Object.values(totals))
  const total = Math.max(1, Object.values(totals).reduce((sum, count) => sum + count, 0))
  return activePlugins.value.slice(0, 5).map((pluginId, index) => ({
    pluginId,
    rank: String(index + 1).padStart(2, '0'),
    name: nameOf(pluginId),
    count: totals[pluginId],
    ratio: Math.max(4, Math.round((totals[pluginId] / max) * 100)),
    share: (totals[pluginId] / total) * 100,
    color: colorOf(pluginId),
  }))
})

function eventKey(item) {
  return item.id || `${item.timestamp || ''}|${item.level || ''}|${item.source || ''}|${item.msg || ''}`
}

const EVENT_SOURCE_NAMES = {
  main: '平台',
  repo_sync: '插件仓库',
  scheduler: '定时服务',
  plugin: '插件管理',
  plugin_runtime: '插件运行',
  account: '账号管理',
  account_manager: '账号管理',
  notification: '通知服务',
  notification_channels: '通知渠道',
  notifier: '通知服务',
  custom_client: 'Telegram 客户端',
  api: '平台接口',
  registry: '插件注册',
  context: '插件上下文',
  deps: '依赖管理',
  browser: '浏览器服务',
  ai: 'AI 服务',
  backup: '备份服务',
  github_import: 'GitHub 导入',
  log_stream: '日志服务',
}

function eventTitle(source) {
  if (!source) return '平台运行'
  const knownName = EVENT_SOURCE_NAMES[source] || st.value?.plugin_names?.[source]
  if (knownName) return knownName
  return /^[a-z][a-z0-9_.-]*$/i.test(source) ? '平台模块' : source
}

function normalizeEvent(item) {
  const level = String(item.level || 'INFO').toUpperCase()
  const timestamp = item.timestamp ? new Date(item.timestamp) : new Date()
  const source = String(item.source || '').trim()
  return {
    key: eventKey(item),
    source,
    detail: String(item.msg || '').replace(/\s+/g, ' ').trim() || '收到一条运行记录',
    time: Number.isNaN(timestamp.getTime()) ? '--:--:--' : timestamp.toLocaleTimeString('zh-CN', { hour12: false }),
    tone: level === 'ERROR' ? 'danger' : level === 'WARNING' ? 'warning' : 'success',
  }
}

function addEvent(item) {
  if (!item || item.type === 'history') return
  const normalized = normalizeEvent(item)
  if (events.value.some(event => event.key === normalized.key)) return
  events.value.unshift(normalized)
  if (events.value.length > 6) events.value.length = 6
}

function connectEvents() {
  disconnectEvents()
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
  eventSocket = new WebSocket(`${protocol}://${location.host}/api/logs/ws?token=${encodeURIComponent(getToken())}&batch_history=1`)
  eventSocket.onopen = () => { eventConnected.value = true }
  eventSocket.onmessage = message => {
    try {
      const item = JSON.parse(message.data)
      if (item.type === 'history' && Array.isArray(item.logs)) {
        events.value = item.logs.slice(0, 6).map(normalizeEvent)
        return
      }
      addEvent(item)
    } catch {}
  }
  eventSocket.onclose = () => {
    eventConnected.value = false
    eventReconnectTimer = setTimeout(connectEvents, 3000)
  }
}

function disconnectEvents() {
  clearTimeout(eventReconnectTimer)
  eventReconnectTimer = null
  if (eventSocket) {
    eventSocket.onclose = null
    eventSocket.close()
    eventSocket = null
  }
  eventConnected.value = false
}

async function refresh() {
  try { await refreshPlatformStatus(true) } catch {}
}

onMounted(() => {
  if (!platformStatus.value) refreshPlatformStatus(true).catch(() => {})
  connectEvents()
  clockTimer = window.setInterval(() => { currentTime.value = Date.now() }, 30000)
})

onUnmounted(() => {
  disconnectEvents()
  clearTimeout(changeTimer)
  clearInterval(clockTimer)
  clearInterval(progressTimer)
  if (firstPaintFrame) cancelAnimationFrame(firstPaintFrame)
  for (const frame of animationFrames.values()) cancelAnimationFrame(frame)
  animationFrames.clear()
})
</script>

<template>
  <div v-if="error" class="status-alert" role="alert">
    <span>{{ error }}</span>
    <button type="button" @click="refresh">重新读取</button>
  </div>

  <div v-if="loading" class="status status-loading" aria-label="正在加载运行状态" aria-busy="true">
    <div class="metric-strip skeleton-surface">
      <div v-for="index in 4" :key="index" class="metric skeleton-metric"><i></i><span><b></b><em></em><small></small></span></div>
    </div>
    <div class="primary-grid">
      <div class="surface skeleton-chart"><b></b><i></i></div>
      <div class="surface skeleton-events"><b></b><i v-for="index in 4" :key="index"></i></div>
    </div>
    <div class="secondary-grid">
      <div v-for="index in 3" :key="index" class="surface skeleton-list"><b></b><i v-for="row in 3" :key="row"></i></div>
    </div>
  </div>

  <div v-else-if="st" class="status" :class="{ ready: pageReady }">
    <section class="metric-strip reveal section-0" aria-label="平台指标">
      <article v-for="(card, index) in cards" :key="card.key" class="metric" :class="card.tone" :style="{ '--delay': `${index * 55}ms` }">
        <span class="metric-icon" :class="{ pulsing: card.key === 'bot' && st.bot_connected }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="icons[card.icon]" /></svg>
        </span>
        <span class="metric-copy">
          <small>{{ card.label }}</small>
          <strong :class="{ compact: card.small }">{{ card.value }}</strong>
          <em>{{ card.sub }}</em>
        </span>
      </article>
    </section>

    <section class="primary-grid reveal section-1">
      <article class="surface activity-panel">
        <header class="panel-heading">
          <div><span class="eyebrow">实时运行</span><h2>插件活动时间线</h2></div>
          <div class="chart-state">
            <div class="range-switch" aria-label="活动时间范围">
              <button type="button" :class="{ active: activityRange === '24h' }" @click="activityRange = '24h'">24 小时</button>
              <button type="button" :class="{ active: activityRange === '7d' }" @click="activityRange = '7d'">7 天</button>
            </div>
          </div>
        </header>

        <div v-if="!hasActivity" class="chart-empty">
          <span class="empty-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 19V9m5 10V5m5 14v-7m5 7V3"/></svg></span>
          <div><strong>还没有插件活动</strong><p>插件处理消息后，这里会自动绘制{{ activityRangeLabel }}趋势。</p></div>
        </div>
        <template v-else>
          <div class="chart-legend">
            <span><i class="trigger"></i>插件触发次数</span>
            <span><i class="success"></i>成功次数</span>
          </div>
          <div class="line-chart">
            <div class="y-axis" aria-hidden="true"><span v-for="tick in chartTicks" :key="tick">{{ tick }}</span></div>
            <div class="chart-canvas">
              <svg viewBox="0 0 1000 208" preserveAspectRatio="none" role="img" :aria-label="`${activityRangeLabel}插件活动趋势`">
                <defs><linearGradient id="activity-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#3080f0" stop-opacity=".26"/><stop offset="100%" stop-color="#3080f0" stop-opacity="0"/></linearGradient></defs>
                <g class="chart-grid"><line v-for="index in 5" :key="index" x1="28" x2="972" :y1="18 + (index - 1) * 39.5" :y2="18 + (index - 1) * 39.5" /></g>
                <path class="chart-area" :d="areaPath" />
                <path class="chart-line trigger" :d="linePath" />
                <path class="chart-line success" :d="successLinePath" />
                <circle v-if="hoveredChartPoint" class="chart-point trigger" :cx="hoveredChartPoint.x" :cy="hoveredChartPoint.y" r="3" />
                <circle v-if="hoveredChartPoint" class="chart-point success" :cx="hoveredChartPoint.x" :cy="hoveredChartPoint.successY" r="3" />
              </svg>
              <button v-for="(point, index) in timeline" :key="`hit-${index}`" type="button" class="chart-hit" :style="{ left: point.hitLeftPct, width: point.hitWidthPct }" :aria-label="`${point.label}，触发 ${point.total} 次，成功 ${point.success} 次`" @mouseenter="hoveredPoint = index" @mouseleave="hoveredPoint = null" @focus="hoveredPoint = index" @blur="hoveredPoint = null"></button>
              <div
                v-if="hoveredChartPoint"
                class="chart-hover-layer"
                :class="{
                  'align-right': hoveredChartPoint.x > 760,
                  'place-above': hoveredChartPoint.y > 112,
                }"
                :style="hoveredChartPointPosition"
                aria-hidden="true"
              >
                <i class="chart-guide"></i>
                <div class="chart-tooltip">
                  <strong>{{ hoveredChartPoint.label }}</strong>
                  <span class="trigger-value">触发次数: <b>{{ hoveredChartPoint.total }}</b></span>
                  <span class="success-value">成功次数: <b>{{ hoveredChartPoint.success }}</b></span>
                </div>
              </div>
              <div class="x-axis" aria-hidden="true"><span v-for="point in visibleAxisLabels" :key="`${point.label}-${point.x}`" :style="{ left: point.xPct }">{{ point.label }}</span></div>
            </div>
          </div>
          <div class="chart-summary">
            <span><small>触发总数</small><strong>{{ animated.activity }}</strong></span>
            <span><small>活跃插件</small><strong>{{ activePlugins.length }}</strong></span>
            <span><small>峰值时段</small><strong>{{ peakPoint?.label || '—' }}</strong></span>
            <span><small>峰值次数</small><strong>{{ peakPoint?.total || 0 }}</strong></span>
          </div>
        </template>
      </article>

      <aside class="surface event-panel">
        <header class="panel-heading compact">
          <div><span class="eyebrow"><i :class="{ online: eventConnected }"></i>{{ eventConnected ? '实时连接' : '正在连接' }}</span><h2>运行事件</h2></div>
        </header>
        <div v-if="!events.length" class="event-empty">
          <span class="empty-mark success"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg></span>
          <div><strong>暂时没有新的事件</strong><p>新的插件记录出现后会自动显示在这里。</p></div>
        </div>
        <div v-else class="event-list">
          <article v-for="event in events" :key="event.key" class="event-row">
            <i class="event-dot" :class="event.tone"></i>
            <div><strong>{{ eventTitle(event.source) }}</strong><p>{{ event.detail }}</p><time>{{ event.time }}</time></div>
          </article>
        </div>
      </aside>
    </section>

    <section class="secondary-grid reveal section-2">
      <article class="surface ranking-panel">
        <header class="panel-heading compact"><div><span class="eyebrow">{{ activityRangeLabel }}</span><h2>活跃插件</h2></div><RouterLink class="panel-link" to="/plugins">查看全部</RouterLink></header>
        <div v-if="!topPlugins.length" class="small-empty">暂无插件活跃记录</div>
        <div v-else class="ranking-list">
          <div v-for="plugin in topPlugins" :key="plugin.pluginId" class="ranking-row">
            <span class="rank">{{ plugin.rank }}</span>
            <div class="ranking-data"><div><strong>{{ plugin.name }}</strong><span class="ranking-metrics"><b>{{ plugin.count }} 次</b><em>{{ plugin.share.toFixed(1) }}%</em></span></div><div class="progress"><i :style="{ width: `${plugin.ratio}%`, background: plugin.color }"></i></div></div>
          </div>
        </div>
      </article>

      <article class="surface account-panel">
        <header class="panel-heading compact"><div><span class="eyebrow">连接状态</span><h2>账号</h2></div><span class="healthy-badge">{{ st.user_count }}/{{ st.accounts.length }} 在线</span></header>
        <div v-if="!st.accounts.length" class="small-empty">暂无账号，请先到账号管理中登录。</div>
        <div v-else class="account-list">
          <div v-for="account in st.accounts" :key="account.session" class="account-row" :class="{ changed: changedAccounts.includes(account.session) }">
            <AccountAvatar :account="account" />
            <div class="account-copy">
              <div class="account-name-row">
                <strong>{{ account.name || account.session }}</strong>
                <AccountPremiumBadge v-if="account.is_premium" compact />
              </div>
              <small>{{ account.session }}</small>
            </div>
            <span class="account-tgid">{{ account.tgid || '未绑定 TGID' }}</span>
            <span class="account-state" :class="{ offline: !account.online }">{{ account.online ? '在线' : '离线' }}</span>
          </div>
        </div>
      </article>

      <article class="surface jobs-panel">
        <header class="panel-heading compact jobs-heading"><div><span class="eyebrow">计划任务</span><h2>即将执行</h2></div><span class="jobs-header-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3v3m10-3v3M4 9h16M5 5h14v15H5z"/><circle cx="16.5" cy="16.5" r="3.5"/><path d="M16.5 14.8v1.9l1.2.7"/></svg></span></header>
        <div v-if="!st.scheduler_jobs.length" class="small-empty">当前没有定时任务。</div>
        <div v-else class="job-list">
          <div v-for="job in st.scheduler_jobs" :key="job.id" class="job-row" :class="{ changed: changedJobs.includes(job.id) }">
            <span class="job-mark" :class="{ running: job.running }"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg></span>
            <div class="job-main"><strong>{{ jobName(job) }}</strong><small>{{ job.plugin }}</small><div v-if="job.running && job.progress?.status === 'running'" class="job-progress"><i :style="{ width: `${job.progress.progress || 0}%` }"></i></div></div>
            <time :title="job.next || ''">{{ nextRunLabel(job) }}</time>
          </div>
        </div>
      </article>
    </section>

    <footer class="runtime-strip reveal section-3">
      <span class="runtime-resource runtime-cpu">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/></svg>
        <b>CPU {{ st.resources?.cpu_percent ?? 0 }}%</b>
      </span>
      <span class="runtime-resource"><b>内存 {{ formatMemory(st.resources?.memory_used_mb) }} / {{ formatMemory(st.resources?.memory_limit_mb) }}</b></span>
      <span class="runtime-health" :class="{ warning: st.core_services && !st.core_services.healthy }"><i></i><b>{{ st.core_services?.message || (st.plugins.error ? `${st.plugins.error} 个插件异常` : '所有核心服务正常') }}</b></span>
    </footer>
  </div>
</template>

<style scoped>
.status { display: flex; flex-direction: column; gap: 14px; }
.reveal { opacity: 0; transform: translateY(9px); transition: opacity .42s ease, transform .42s cubic-bezier(.2,.8,.2,1); }
.status.ready .reveal { opacity: 1; transform: translateY(0); }
.status.ready .section-1 { transition-delay: 70ms; }
.status.ready .section-2 { transition-delay: 130ms; }
.status.ready .section-3 { transition-delay: 190ms; }

.status-alert { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; padding: 11px 14px; border: 1px solid rgba(224,72,79,.45); border-radius: 10px; background: var(--danger-dim); color: var(--danger); font-size: 13px; }
.status-alert button { border: 1px solid currentColor; border-radius: 7px; padding: 5px 9px; background: transparent; color: inherit; cursor: pointer; }
.surface, .metric-strip { border: 1px solid var(--border); background: rgba(14,19,29,.94); box-shadow: 0 10px 34px rgba(0,0,0,.12); }
.surface { min-width: 0; border-radius: 13px; overflow: hidden; transition: border-color .2s ease, box-shadow .2s ease, translate .2s ease; }

@media (hover: hover) and (pointer: fine) {
  .surface:hover { translate: 0 -2px; border-color: rgba(48,128,240,.32); box-shadow: 0 12px 28px rgba(0,0,0,.2), 0 0 22px rgba(48,128,240,.06); }
}

.metric-strip { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); border-radius: 13px; overflow: hidden; }
.metric { min-width: 0; display: flex; align-items: center; gap: 13px; padding: 16px 18px; border-right: 1px solid var(--border); animation: metric-enter .4s ease both; animation-delay: var(--delay); }
.metric:last-child { border-right: 0; }
.metric-icon { position: relative; width: 42px; height: 42px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 11px; }
.metric-icon svg { width: 22px; height: 22px; }
.metric.green .metric-icon, .metric.teal .metric-icon { color: var(--success); background: rgba(16,176,128,.11); }
.metric.blue .metric-icon { color: var(--accent); background: var(--accent-dim); }
.metric.amber .metric-icon { color: var(--warning); background: rgba(224,160,32,.12); }
.metric.gray .metric-icon { color: var(--text-muted); background: var(--bg-elevated); }
.metric-icon.pulsing::after { content: ''; position: absolute; inset: 0; border: 1px solid currentColor; border-radius: inherit; animation: status-pulse 2.6s ease-out infinite; }
.metric-copy { min-width: 0; display: grid; grid-template-columns: auto 1fr; align-items: baseline; column-gap: 8px; }
.metric-copy small { grid-column: 1 / -1; color: var(--text-muted); font-size: 11px; }
.metric-copy strong { margin-top: 4px; color: var(--text-primary); font-size: 23px; line-height: 1; font-variant-numeric: tabular-nums; }
.metric-copy strong.compact { font-size: 18px; }
.metric-copy em { overflow: hidden; color: var(--text-muted); font-size: 11px; font-style: normal; white-space: nowrap; text-overflow: ellipsis; }

.primary-grid { display: grid; grid-template-columns: minmax(0,2.1fr) minmax(290px,.9fr); gap: 14px; }
.secondary-grid { display: grid; grid-template-columns: minmax(0,1.15fr) minmax(0,.9fr) minmax(0,1fr); gap: 14px; }
.panel-heading { min-height: 66px; display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 15px 17px 11px; }
.panel-heading.compact { min-height: 62px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.panel-heading h2 { margin: 4px 0 0; font-size: 16px; line-height: 1.2; }
.eyebrow { display: flex; align-items: center; gap: 6px; color: var(--text-muted); font-size: 10px; letter-spacing: .05em; }
.eyebrow i { width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted); }
.eyebrow i.online { background: var(--success); box-shadow: 0 0 8px rgba(16,176,128,.65); }
.chart-state { display: flex; align-items: center; gap: 8px; color: var(--text-muted); font-size: 11px; }
.chart-state span { display: flex; gap: 5px; }
.chart-state b { color: var(--text-primary); font-weight: 600; }
.range-switch { display: flex; gap: 3px; padding: 3px; border: 1px solid var(--border); border-radius: 9px; background: rgba(8,13,22,.7); }
.range-switch button { min-width: 52px; border: 0; border-radius: 6px; padding: 6px 9px; background: transparent; color: var(--text-muted); font-size: 10px; cursor: pointer; transition: color .18s ease, background .18s ease; }
.range-switch button:hover { color: var(--text-primary); }
.range-switch button.active { background: var(--bg-elevated); color: var(--text-primary); box-shadow: 0 2px 8px rgba(0,0,0,.18); }

.chart-legend { display: flex; gap: 18px; padding: 0 17px 4px; color: var(--text-muted); font-size: 11px; }
.chart-legend span { display: flex; align-items: center; gap: 7px; }
.chart-legend i { width: 16px; height: 2px; border-radius: 3px; }
.chart-legend i.trigger { background: var(--accent); }
.chart-legend i.success { background: var(--success); }
.line-chart { height: 226px; display: grid; grid-template-columns: 28px minmax(0,1fr); padding: 2px 15px 0 11px; }
.y-axis { height: 208px; display: flex; flex-direction: column; justify-content: space-between; padding: 11px 5px 19px 0; color: var(--text-muted); font-size: 9px; text-align: right; font-variant-numeric: tabular-nums; }
.chart-canvas { position: relative; min-width: 0; height: 226px; }
.chart-canvas > svg { width: 100%; height: 208px; overflow: visible; }
.chart-grid line { stroke: var(--border); stroke-width: 1; stroke-dasharray: 3 4; vector-effect: non-scaling-stroke; }
.chart-area { fill: url(#activity-area); animation: chart-fade .55s ease both; }
.chart-line { fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
.chart-line.trigger { stroke: var(--accent); filter: drop-shadow(0 4px 7px rgba(48,128,240,.2)); }
.chart-line.success { stroke: var(--success); filter: drop-shadow(0 4px 7px rgba(16,176,128,.16)); }
.chart-point { fill: var(--bg-card); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.chart-point.trigger { stroke: var(--accent); }
.chart-point.success { stroke: var(--success); }
.chart-hit { position: absolute; top: 0; height: 208px; padding: 0; border: 0; background: transparent; cursor: crosshair; }
.chart-hit:focus-visible { outline: 1px solid rgba(48,128,240,.38); outline-offset: -1px; }
.chart-hover-layer { position: absolute; z-index: 4; width: 0; height: 0; pointer-events: none; }
.chart-guide { position: absolute; left: 0; top: calc(-1 * var(--chart-guide-top, 208px)); width: 1px; height: 208px; background: rgba(188,201,220,.58); transform: translateX(-.5px); }
.chart-tooltip { position: absolute; top: 14px; left: 12px; min-width: 144px; padding: 12px 13px; border: 1px solid var(--border-light); border-radius: 10px; background: rgba(15,24,37,.98); box-shadow: 0 12px 30px rgba(0,0,0,.32); }
.chart-hover-layer.align-right .chart-tooltip { left: auto; right: 12px; }
.chart-hover-layer.place-above .chart-tooltip { top: auto; bottom: 14px; }
.chart-tooltip strong, .chart-tooltip span { display: block; white-space: nowrap; }
.chart-tooltip strong { margin-bottom: 8px; color: var(--text-primary); font-size: 14px; font-weight: 600; }
.chart-tooltip span { font-size: 12px; }
.chart-tooltip .trigger-value { color: var(--accent); }
.chart-tooltip .success-value { margin-top: 5px; color: var(--success); }
.chart-tooltip b { font-weight: 600; font-variant-numeric: tabular-nums; }
.x-axis { position: absolute; left: 0; right: 0; bottom: 0; height: 18px; color: var(--text-muted); font-size: 9px; }
.x-axis span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.chart-summary { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); border-top: 1px solid var(--border); }
.chart-summary span { min-width: 0; padding: 11px 16px; border-right: 1px solid var(--border); }
.chart-summary span:last-child { border-right: 0; }
.chart-summary small, .chart-summary strong { display: block; }
.chart-summary small { color: var(--text-muted); font-size: 10px; }
.chart-summary strong { margin-top: 4px; font-size: 15px; font-variant-numeric: tabular-nums; }
.chart-empty { min-height: 276px; margin: 0 15px 15px; display: flex; align-items: center; justify-content: center; gap: 12px; border: 1px dashed var(--border-light); border-radius: 10px; background: rgba(8,12,19,.34); }
.chart-empty strong, .event-empty strong { display: block; font-size: 13px; }
.chart-empty p, .event-empty p { margin: 5px 0 0; color: var(--text-muted); font-size: 11px; line-height: 1.45; }
.empty-mark { width: 40px; height: 40px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 10px; color: var(--accent); background: var(--accent-dim); }
.empty-mark.success { color: var(--success); background: rgba(16,176,128,.1); }
.empty-mark svg { width: 20px; height: 20px; }

.event-panel { min-height: 360px; }
.event-list { max-height: 313px; overflow-y: auto; padding: 2px 16px 10px; }
.event-row { display: grid; grid-template-columns: 9px minmax(0,1fr); gap: 10px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.event-row:last-child { border-bottom: 0; }
.event-dot { width: 7px; height: 7px; margin-top: 5px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 4px rgba(48,128,240,.08); }
.event-dot.success { background: var(--success); box-shadow: 0 0 0 4px rgba(16,176,128,.08); }
.event-dot.warning { background: var(--warning); box-shadow: 0 0 0 4px rgba(224,160,32,.08); }
.event-dot.danger { background: var(--danger); box-shadow: 0 0 0 4px rgba(224,72,79,.08); }
.event-row strong { display: block; overflow: hidden; font-size: 12px; white-space: nowrap; text-overflow: ellipsis; }
.event-row p { display: -webkit-box; margin: 4px 0 0; overflow: hidden; color: var(--text-secondary); font-size: 11px; line-height: 1.45; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.event-row time { display: block; margin-top: 6px; color: var(--text-muted); font-family: var(--font-mono); font-size: 9px; }
.event-empty { min-height: 214px; margin: 14px; padding: 20px; display: flex; align-items: center; justify-content: center; gap: 12px; border: 1px dashed var(--border-light); border-radius: 10px; background: rgba(8,12,19,.34); }

.panel-count, .healthy-badge { border-radius: 20px; padding: 4px 8px; background: var(--bg-elevated); color: var(--text-muted); font-size: 9px; }
.healthy-badge { color: var(--success); background: rgba(16,176,128,.09); }
.panel-link { color: var(--accent); font-size: 11px; font-weight: 500; transition: color .16s ease; }
.panel-link:hover { color: var(--accent-hover); }
.panel-link:focus-visible { border-radius: 4px; outline: 2px solid var(--accent); outline-offset: 3px; }
.ranking-list, .account-list { padding: 3px 15px 10px; }
.job-list { padding: 3px 22px 14px; }
.ranking-row { display: grid; grid-template-columns: 24px minmax(0,1fr); gap: 11px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border); }
.ranking-row:last-child, .account-row:last-child, .job-row:last-child { border-bottom: 0; }
.rank { color: var(--text-muted); font-family: var(--font-mono); font-size: 10px; }
.ranking-data > div:first-child { display: flex; justify-content: space-between; gap: 10px; }
.ranking-data strong { overflow: hidden; font-size: 12px; white-space: nowrap; text-overflow: ellipsis; }
.ranking-metrics { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 9px; font-size: 10px; }
.ranking-metrics b { color: var(--text-muted); font-weight: 500; }
.ranking-metrics em { color: var(--success); font-style: normal; font-variant-numeric: tabular-nums; }
.progress { height: 3px; margin-top: 8px; overflow: hidden; border-radius: 4px; background: var(--bg-elevated); }
.progress i { display: block; height: 100%; border-radius: inherit; }

.account-list { max-height: 249px; overflow-y: auto; }
.job-list { max-height: 320px; overflow-y: auto; }
.account-row { display: grid; grid-template-columns: 32px minmax(0,1fr) auto auto; gap: 9px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border); }
.account-avatar { width: 31px; height: 31px; border-radius: 8px; font-size: 9px; }
.account-copy { min-width: 0; }
.account-name-row { display: flex; min-width: 0; align-items: center; gap: 5px; }
.account-row strong, .account-row small { display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.account-row strong { min-width: 0; font-size: 11px; }
.account-row small { margin-top: 3px; color: var(--text-muted); font-size: 9px; }
.account-tgid { color: var(--text-muted); font-family: var(--font-mono); font-size: 9px; }
.account-state { color: var(--success); font-size: 10px; }
.account-state.offline { color: var(--text-muted); }

.jobs-panel { min-height: 0; align-self: start; border-color: rgba(48,128,240,.38); }
.jobs-heading { min-height: 80px; padding-top: 18px; }
.jobs-header-icon { width: 28px; height: 28px; display: grid; place-items: center; color: var(--text-secondary); }
.jobs-header-icon svg { width: 27px; height: 27px; }
.job-row { display: grid; grid-template-columns: 40px minmax(0,1fr) auto; gap: 12px; align-items: center; min-height: 55px; margin: 0; padding: 10px 0; border: 0; border-bottom: 1px solid var(--border); border-radius: 0; background: transparent; }
.job-row:last-child { border-bottom: 0; }
.job-mark { width: 40px; height: 40px; display: grid; place-items: center; border-radius: 10px; background: rgba(48,128,240,.12); color: var(--accent); }
.job-mark.running { color: var(--success); background: rgba(16,176,128,.1); }
.job-mark svg { width: 19px; height: 19px; }
.job-row strong, .job-row small { display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.job-row strong { font-size: 12px; }
.job-row small { margin-top: 4px; color: var(--text-muted); font-size: 10px; }
.job-row time { max-width: 110px; overflow: hidden; color: var(--text-secondary); font-family: var(--font-mono); font-size: 10px; white-space: nowrap; text-overflow: ellipsis; }
.job-main { min-width: 0; }
.job-progress { height: 3px; margin-top: 7px; overflow: hidden; border-radius: 4px; background: var(--bg-elevated); }
.job-progress i { display: block; height: 100%; border-radius: inherit; background: var(--success); transition: width .25s ease; }
.small-empty { min-height: 160px; display: grid; place-items: center; padding: 20px; color: var(--text-muted); font-size: 11px; text-align: center; }

.runtime-strip { min-height: 34px; display: flex; align-items: center; gap: 24px; padding: 0 22px; overflow: hidden; }
.runtime-strip > span { min-width: 0; display: flex; align-items: center; gap: 7px; color: var(--text-secondary); }
.runtime-strip b { overflow: hidden; font-size: 10px; font-weight: 500; white-space: nowrap; text-overflow: ellipsis; }
.runtime-resource svg { width: 14px; height: 14px; color: var(--text-muted); }
.runtime-strip .runtime-health { color: var(--success); }
.runtime-strip .runtime-health.warning { color: var(--warning); }
.runtime-health i { width: 7px; height: 7px; flex: 0 0 auto; border-radius: 50%; background: currentColor; box-shadow: 0 0 8px rgba(16,176,128,.55); }

.changed { animation: row-changed 1.35s ease both; }
@keyframes row-changed { 18% { background: rgba(48,128,240,.12); box-shadow: inset 0 0 0 1px rgba(48,128,240,.25); } }
@keyframes metric-enter { from { opacity: 0; transform: translateY(7px); } }
@keyframes chart-fade { from { opacity: 0; } }
@keyframes status-pulse { 0%,45% { opacity: 0; transform: scale(.96); } 62% { opacity: .28; } 100% { opacity: 0; transform: scale(1.2); } }

.skeleton-metric i, .skeleton-metric b, .skeleton-metric em, .skeleton-metric small, .skeleton-chart b, .skeleton-chart i, .skeleton-events b, .skeleton-events i, .skeleton-list b, .skeleton-list i { display: block; border-radius: 7px; background: linear-gradient(100deg,var(--bg-elevated) 20%,rgba(60,69,89,.5) 45%,var(--bg-elevated) 70%); background-size: 220% 100%; animation: skeleton-flow 1.35s ease-in-out infinite; }
.skeleton-metric i { width: 42px; height: 42px; flex: 0 0 auto; }
.skeleton-metric span { flex: 1; display: grid; gap: 6px; }
.skeleton-metric b { width: 55%; height: 8px; }
.skeleton-metric em { width: 72%; height: 18px; }
.skeleton-metric small { width: 40%; height: 7px; }
.skeleton-chart, .skeleton-events, .skeleton-list { min-height: 310px; padding: 18px; }
.skeleton-chart b, .skeleton-events b, .skeleton-list b { width: 120px; height: 10px; }
.skeleton-chart i { width: 92%; height: 165px; margin: 56px auto 0; }
.skeleton-events i, .skeleton-list i { height: 38px; margin-top: 18px; }
@keyframes skeleton-flow { to { background-position: -120% 0; } }

@media (max-width: 1120px) {
  .metric-strip { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .metric:nth-child(2) { border-right: 0; }
  .metric:nth-child(-n+2) { border-bottom: 1px solid var(--border); }
  .primary-grid { grid-template-columns: 1fr; }
  .secondary-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .jobs-panel { grid-column: 1 / -1; }
}

@media (max-width: 720px) {
  .status { gap: 10px; }
  .metric { padding: 13px 12px; gap: 10px; }
  .metric-icon { width: 38px; height: 38px; }
  .metric-copy strong { font-size: 19px; }
  .metric-copy em { grid-column: 1 / -1; margin-top: 4px; }
  .secondary-grid { grid-template-columns: 1fr; }
  .jobs-panel { grid-column: auto; }
  .line-chart { height: 214px; padding-inline: 7px; }
  .chart-canvas { height: 214px; }
  .chart-summary { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .chart-summary span:nth-child(2) { border-right: 0; }
  .chart-summary span:nth-child(-n+2) { border-bottom: 1px solid var(--border); }
  .account-row { grid-template-columns: 32px minmax(0,1fr) auto; }
  .account-tgid { display: none; }
  .runtime-strip { flex-wrap: wrap; gap: 8px 18px; padding-block: 11px; }
  .runtime-strip .runtime-health { width: 100%; }
}

@media (max-width: 460px) {
  .metric { padding: 12px 10px; gap: 8px; }
  .metric-icon { width: 34px; height: 34px; }
  .metric-icon svg { width: 19px; height: 19px; }
  .metric-copy strong { font-size: 17px; }
  .metric-copy strong.compact { font-size: 15px; }
  .metric-copy small, .metric-copy em { font-size: 9px; }
  .panel-heading { padding-inline: 13px; }
  .line-chart { grid-template-columns: 22px minmax(0,1fr); }
  .chart-summary span { padding-inline: 12px; }
  .job-row { grid-template-columns: 40px minmax(0,1fr); }
  .job-row time { grid-column: 2; }
}

@media (prefers-reduced-motion: reduce) {
  .reveal, .status.ready .reveal { opacity: 1; transform: none; transition: none; }
  .metric, .metric-icon.pulsing::after, .chart-area, .changed, .skeleton-metric i, .skeleton-metric b, .skeleton-metric em, .skeleton-metric small, .skeleton-chart b, .skeleton-chart i, .skeleton-events b, .skeleton-events i, .skeleton-list b, .skeleton-list i { animation: none; }
  .surface { transition: border-color .2s ease; }
  .surface:hover { translate: none; }
}
</style>
