<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { api } from '../api'

const st = ref(null)
const error = ref('')
let timer = null

async function load() {
  try { st.value = await api.status(); error.value = '' }
  catch (e) { error.value = e.message }
}
onMounted(() => { load(); timer = setInterval(load, 8000) })
onUnmounted(() => clearInterval(timer))

const uptime = computed(() => {
  let s = st.value?.uptime_seconds || 0
  const d = Math.floor(s / 86400); s %= 86400
  const h = Math.floor(s / 3600); s %= 3600
  const m = Math.floor(s / 60)
  const parts = []
  if (d) parts.push(`${d}天`)
  if (h) parts.push(`${h}时`)
  parts.push(`${m}分`)
  return parts.join('')
})

// APScheduler trigger 字符串转人类可读
// interval[0:01:00] / cron[hour='9',minute='0'] / date[2026-06-27 14:30:00 CST]
function prettyTrigger(t) {
  if (!t) return '—'
  const m = /^(\w+)\[(.*)\]$/.exec(t)
  if (!m) return t
  const [, kind, body] = m
  if (kind === 'interval') {
    const [hh, mm, ss] = body.split(':').map(Number)
    const secs = (hh || 0) * 3600 + (mm || 0) * 60 + (ss || 0)
    if (secs % 86400 === 0 && secs) return `每 ${secs / 86400} 天`
    if (secs % 3600 === 0 && secs) return `每 ${secs / 3600} 小时`
    if (secs % 60 === 0 && secs) return `每 ${secs / 60} 分钟`
    return `每 ${secs} 秒`
  }
  if (kind === 'cron') return prettyCron(body)
  if (kind === 'date') return `单次 ${body.split(' ').slice(0, 2).join(' ')}`
  return t
}

// cron 字段转中文，如 hour='3', minute='0' → 每天 03:00
const WEEKDAYS = { '0': '周一', '1': '周二', '2': '周三', '3': '周四', '4': '周五', '5': '周六', '6': '周日',
  mon: '周一', tue: '周二', wed: '周三', thu: '周四', fri: '周五', sat: '周六', sun: '周日' }
function prettyCron(body) {
  // 解析 key='value' 片段
  const f = {}
  for (const seg of body.split(',')) {
    const mm = /(\w+)\s*=\s*'?([^',]+)'?/.exec(seg.trim())
    if (mm) f[mm[1]] = mm[2]
  }
  const h = f.hour, mi = f.minute, dow = f.day_of_week, dom = f.day, mon = f.month
  // 纯「每天 时:分」
  const isNum = (v) => v !== undefined && /^\d+$/.test(v)
  if (isNum(h) && isNum(mi) && !dow && !dom && !mon) {
    return `每天 ${String(h).padStart(2, '0')}:${String(mi).padStart(2, '0')}`
  }
  // 每周某天 时:分
  if (isNum(h) && isNum(mi) && dow !== undefined) {
    const day = WEEKDAYS[String(dow).toLowerCase()] || `周${dow}`
    return `每${day} ${String(h).padStart(2, '0')}:${String(mi).padStart(2, '0')}`
  }
  // 每小时（只指定了分钟）
  if (isNum(mi) && h === undefined && !dow && !dom) {
    return `每小时 第${mi}分`
  }
  // 兜底：拼中文字段
  const parts = []
  if (mon) parts.push(`${mon}月`)
  if (dom) parts.push(`${dom}日`)
  if (dow !== undefined) parts.push(WEEKDAYS[String(dow).toLowerCase()] || `周${dow}`)
  if (h !== undefined) parts.push(`${h}时`)
  if (mi !== undefined) parts.push(`${mi}分`)
  return parts.length ? parts.join(' ') : 'cron'
}

// 平台内置任务的中文名（id → 显示名）；插件任务用后端给的 name
const JOB_NAMES = { log_cleaner: '日志清理', 插件仓库轮询: '插件仓库轮询' }
function jobName(j) { return JOB_NAMES[j.name] || JOB_NAMES[j.id] || j.name }

// 概览卡片配置
const cards = computed(() => {
  if (!st.value) return []
  const s = st.value
  return [
    { key: 'bot', label: 'Bot 账号', value: s.bot_connected ? '在线' : '离线',
      tone: s.bot_connected ? 'green' : 'gray', icon: 'bot' },
    { key: 'user', label: '在线用户账号', value: s.user_count, sub: `共 ${s.accounts.length} 个`,
      tone: s.user_count ? 'blue' : 'gray', icon: 'user' },
    { key: 'plugin', label: '已启用插件', value: s.plugins.enabled, sub: `共 ${s.plugins.total} 个${s.plugins.error ? ' · ' + s.plugins.error + ' 异常' : ''}`,
      tone: 'purple', icon: 'plug' },
    { key: 'uptime', label: '运行时长', value: uptime.value,
      tone: 'teal', icon: 'clock', small: true },
  ]
})

const icons = {
  bot: 'M12 8V4H8M4 8h16v12H4zM2 14h2M20 14h2M9 13v2M15 13v2',
  user: 'M12 12a5 5 0 100-10 5 5 0 000 10zM4 21a8 8 0 0116 0',
  plug: 'M9 2v6M15 2v6M6 8h12v3a6 6 0 01-12 0zM12 17v5',
  clock: 'M12 22a10 10 0 100-20 10 10 0 000 20zM12 6v6l4 2',
}

// ── 插件活跃时间线 ──
const PALETTE = ['#3080f0', '#10b080', '#a050f0', '#f0a020', '#e05070',
                 '#20b0d0', '#8090f0', '#50c070', '#f06040', '#c0a040']

// 出现过的插件 id（按总活跃量降序，决定配色与图例顺序）
const activePlugins = computed(() => {
  const totals = st.value?.activity?.totals || {}
  return Object.keys(totals).sort((a, b) => totals[b] - totals[a])
})

const colorOf = (pid) => {
  const i = activePlugins.value.indexOf(pid)
  return PALETTE[(i < 0 ? 0 : i) % PALETTE.length]
}
const nameOf = (pid) => (st.value?.plugin_names?.[pid]) || pid

// 时间线柱子：每个桶一根堆叠柱，高度按桶内总活跃归一化
const timeline = computed(() => {
  const buckets = st.value?.activity?.buckets || []
  const sums = buckets.map(b => Object.values(b.counts).reduce((a, c) => a + c, 0))
  const max = Math.max(1, ...sums)
  return buckets.map((b, idx) => {
    const total = sums[idx]
    const segs = activePlugins.value
      .filter(pid => b.counts[pid])
      .map(pid => ({ pid, count: b.counts[pid], frac: b.counts[pid] / total }))
    const d = new Date(b.t * 1000)
    const label = `${String(d.getHours()).padStart(2, '0')}:00`
    return { total, heightPct: (total / max) * 100, segs, label, hour: d.getHours() }
  })
})

const hasActivity = computed(() => (activePlugins.value.length > 0))

// ── 活跃占比环形图 ──
const donut = computed(() => {
  const totals = st.value?.activity?.totals || {}
  const sum = Object.values(totals).reduce((a, c) => a + c, 0)
  if (!sum) return { sum: 0, stops: [], segs: [] }
  let acc = 0
  const stops = []
  const segs = []
  for (const pid of activePlugins.value) {
    const frac = totals[pid] / sum
    const from = acc * 360
    const to = (acc + frac) * 360
    const col = colorOf(pid)
    stops.push(`${col} ${from}deg ${to}deg`)
    segs.push({ pid, count: totals[pid], pct: Math.round(frac * 100), color: col })
    acc += frac
  }
  return { sum, gradient: `conic-gradient(${stops.join(',')})`, segs }
})
</script>

<template>
  <div v-if="error" class="alert">{{ error }}</div>
  <div v-if="st" class="status">
    <!-- 概览卡片：固定 4 列均分 -->
    <div class="grid">
      <div v-for="c in cards" :key="c.key" class="card stat" :class="c.tone">
        <div class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
               stroke-linecap="round" stroke-linejoin="round"><path :d="icons[c.icon]" /></svg>
        </div>
        <div class="stat-body">
          <div class="stat-label">{{ c.label }}</div>
          <div class="stat-value" :class="{ sm: c.small }">{{ c.value }}</div>
          <div class="stat-sub" v-if="c.sub">{{ c.sub }}</div>
        </div>
      </div>
    </div>

    <!-- 活跃时间线 + 占比环形 -->
    <div class="cols cols-activity">
      <!-- 时间线 -->
      <div class="card info chart-card">
        <div class="card-title">插件活跃时间线 <span class="muted sub">近 24 小时</span></div>
        <div v-if="!hasActivity" class="muted empty-chart">暂无插件活跃记录，插件处理消息后这里会出现时间线。</div>
        <template v-else>
          <div class="bars">
            <div v-for="(bar, i) in timeline" :key="i" class="bar-col" :title="`${bar.label} · ${bar.total} 次`">
              <div class="bar-stack" :style="{ height: bar.heightPct + '%' }">
                <div v-for="seg in bar.segs" :key="seg.pid" class="bar-seg"
                     :style="{ height: (seg.frac * 100) + '%', background: colorOf(seg.pid) }"></div>
              </div>
            </div>
          </div>
          <div class="bars-axis">
            <span v-for="(bar, i) in timeline" :key="i" class="axis-tick">
              <template v-if="bar.hour % 6 === 0">{{ bar.label }}</template>
            </span>
          </div>
          <div class="legend">
            <span v-for="pid in activePlugins" :key="pid" class="legend-item">
              <i class="dot" :style="{ background: colorOf(pid) }"></i>{{ nameOf(pid) }}
            </span>
          </div>
        </template>
      </div>

      <!-- 占比环形 -->
      <div class="card info chart-card">
        <div class="card-title">活跃占比</div>
        <div v-if="!donut.sum" class="muted empty-chart">暂无数据</div>
        <div v-else class="donut-wrap">
          <div class="donut" :style="{ background: donut.gradient }">
            <div class="donut-hole">
              <div class="donut-num">{{ donut.sum }}</div>
              <div class="donut-cap">总触发</div>
            </div>
          </div>
          <div class="donut-legend">
            <div v-for="seg in donut.segs" :key="seg.pid" class="dl-row">
              <i class="dot" :style="{ background: seg.color }"></i>
              <span class="dl-name">{{ nameOf(seg.pid) }}</span>
              <span class="dl-pct mono">{{ seg.pct }}%</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="cols">
      <!-- 账号 -->
      <div class="card info">
        <div class="card-title">账号 ({{ st.accounts.length }})</div>
        <div v-if="st.accounts.length === 0" class="muted empty">暂无账号，去「账号管理」登录。</div>
        <table v-else class="tbl">
          <thead><tr><th>名称</th><th>session</th><th>TGID</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="a in st.accounts" :key="a.session">
              <td>{{ a.name }}</td>
              <td class="mono">{{ a.session }}</td>
              <td class="mono">{{ a.tgid || '—' }}</td>
              <td><span class="badge" :class="a.online ? 'badge-on' : 'badge-off'">{{ a.online ? '在线' : '离线' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 定时任务 -->
      <div class="card info">
        <div class="card-title">定时任务 ({{ st.scheduler_jobs.length }})</div>
        <div v-if="st.scheduler_jobs.length === 0" class="muted empty">无定时任务</div>
        <div v-else class="job-list">
          <div v-for="j in st.scheduler_jobs" :key="j.id" class="job">
            <div class="job-main">
              <span class="job-name">{{ jobName(j) }}</span>
              <span class="job-plugin">{{ j.plugin }}</span>
            </div>
            <div class="job-meta">
              <span class="job-trigger mono">{{ prettyTrigger(j.trigger) }}</span>
              <span class="job-next mono">下次 {{ j.next || '—' }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 平台信息：全宽长条 -->
    <div class="card info">
      <div class="card-title">平台信息</div>
      <div class="info-bar">
        <div class="ib"><span class="ib-k">版本</span><span class="ib-v mono">v{{ st.version }}</span></div>
        <div class="ib"><span class="ib-k">Python</span><span class="ib-v mono">{{ st.python }}</span></div>
        <div class="ib"><span class="ib-k">系统</span><span class="ib-v">{{ st.platform }}</span></div>
        <div class="ib"><span class="ib-k">Web 端口</span><span class="ib-v mono">{{ st.web_port }}</span></div>
        <div class="ib"><span class="ib-k">已加载插件</span><span class="ib-v">{{ st.plugins.loaded }}</span></div>
        <div class="ib"><span class="ib-k">定时任务</span><span class="ib-v">{{ st.scheduler_jobs.length }}</span></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.status { display: flex; flex-direction: column; gap: var(--gap); }

/* 概览卡片：固定 4 列均分，和下方列严格对齐 */
.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap); }
@media (max-width: 1100px) { .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 560px)  { .grid { grid-template-columns: 1fr; } }
.stat { display: flex; align-items: center; gap: 16px; }
.stat-icon {
  width: 48px; height: 48px; border-radius: 12px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
}
.stat-icon svg { width: 24px; height: 24px; }
.stat.green .stat-icon { background: var(--accent-2-dim); color: var(--accent-2); }
.stat.blue  .stat-icon { background: var(--accent-dim); color: var(--accent); }
.stat.purple .stat-icon { background: rgba(160,80,240,0.15); color: #a050f0; }
.stat.teal  .stat-icon { background: rgba(16,176,128,0.15); color: #10b0a0; }
.stat.gray  .stat-icon { background: var(--bg-elevated); color: var(--text-muted); }
.stat-body { min-width: 0; }
.stat-label { color: var(--text-secondary); font-size: 13px; }
.stat-value { font-size: 26px; font-weight: 700; margin-top: 2px; }
.stat-value.sm { font-size: 19px; }
.stat-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

.cols { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap); }
/* 时间线宽、环形窄 */
.cols-activity { grid-template-columns: 2fr 1fr; }
@media (max-width: 860px) { .cols, .cols-activity { grid-template-columns: 1fr; } }

.info { display: flex; flex-direction: column; gap: 14px; }
.card-title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.card-title .sub { font-size: 12px; font-weight: 400; margin-left: 6px; }

/* 时间线柱状图 */
.chart-card { min-height: 240px; }
.empty-chart { padding: 40px 0; text-align: center; font-size: 13px; }
.bars {
  display: flex; align-items: flex-end; gap: 3px;
  height: 150px; padding-top: 8px;
}
.bar-col { flex: 1; height: 100%; display: flex; align-items: flex-end; }
.bar-stack {
  width: 100%; min-height: 2px; border-radius: 3px 3px 0 0; overflow: hidden;
  display: flex; flex-direction: column-reverse;
  background: var(--bg-elevated);
  transition: height 0.3s ease;
}
.bar-seg { width: 100%; }
.bars-axis { display: flex; gap: 3px; margin-top: 6px; }
.axis-tick { flex: 1; font-size: 10px; color: var(--text-muted); text-align: left; white-space: nowrap; }
.legend { display: flex; flex-wrap: wrap; gap: 8px 12px; margin-top: 6px; max-height: 88px; overflow-y: auto; }
.legend-item { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
.dot { width: 9px; height: 9px; border-radius: 2px; flex-shrink: 0; }

/* 环形图 */
.donut-wrap { display: flex; flex-direction: column; align-items: center; gap: 18px; }
.donut {
  width: 150px; height: 150px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
}
.donut-hole {
  width: 96px; height: 96px; border-radius: 50%; background: var(--bg-card);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
}
.donut-num { font-size: 24px; font-weight: 700; }
.donut-cap { font-size: 11px; color: var(--text-muted); }
.donut-legend { width: 100%; display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 6px 14px; max-height: 132px; overflow-y: auto; }
.dl-row { display: flex; align-items: center; gap: 8px; font-size: 13px; min-width: 0; }
.dl-name { flex: 1; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dl-pct { color: var(--text-primary); }

.info-list { display: flex; flex-direction: column; }
.info-list > div { display: flex; justify-content: space-between; padding: 9px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.info-list > div:last-child { border-bottom: none; }
.info-list .k { color: var(--text-muted); }
.info-list .v { color: var(--text-primary); }
/* 平台信息全宽长条：横向排开，每项「标签在上、值在下」 */
.info-bar { display: flex; flex-wrap: wrap; gap: 12px 0; }
.ib {
  flex: 1; min-width: 120px; display: flex; flex-direction: column; gap: 4px;
  padding: 0 20px; border-right: 1px solid var(--border);
}
.ib:last-child { border-right: none; }
.ib-k { font-size: 12px; color: var(--text-muted); }
.ib-v { font-size: 15px; font-weight: 600; color: var(--text-primary); }
@media (max-width: 700px) { .ib { flex-basis: 33%; border-right: none; padding: 0 8px; } }

.job-list { display: flex; flex-direction: column; gap: 8px; max-height: 216px; overflow-y: auto; padding-right: 4px; }
/* 滚动条细化，深色风格 */
.job-list::-webkit-scrollbar { width: 6px; }
.job-list::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 3px; }
.job-list::-webkit-scrollbar-track { background: transparent; }
.job {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  font-size: 13px; padding: 8px 0; border-bottom: 1px solid var(--border);
}
.job:last-child { border-bottom: none; }
.job-main { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.job-name { color: var(--text-primary); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.job-plugin { color: var(--accent); font-size: 12px; }
.job-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; flex-shrink: 0; }
.job-trigger { color: var(--text-secondary); font-size: 12px; }
.job-next { color: var(--text-muted); font-size: 12px; }

.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th { text-align: left; color: var(--text-muted); font-weight: 500; padding: 6px 10px; border-bottom: 1px solid var(--border); }
.tbl td { padding: 9px 10px; border-bottom: 1px solid var(--border); }
.tbl tr:last-child td { border-bottom: none; }
.empty { padding: 12px 0; }
.alert { background: var(--danger-dim); color: var(--danger); padding: 10px 14px; border-radius: var(--radius-sm); margin-bottom: 16px; }

/* 手机适配 */
@media (max-width: 768px) {
  .grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
  .stat { padding: 12px; gap: 10px; }
  .stat-icon { width: 38px; height: 38px; }
  .stat-value { font-size: 18px; }
  /* 账号表格：超宽时容器内横向滚动，不撑破布局 */
  .tbl { display: block; overflow-x: auto; white-space: nowrap; }
  .info-bar { gap: 10px 0; }
  .ib { flex-basis: 50%; }
}
</style>
