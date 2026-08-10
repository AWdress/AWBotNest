<script setup>
import { ref, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { getToken } from '../api'

const logs = ref([])
const connected = ref(false)
const levelFilter = ref('ALL')
const search = ref('')
const autoScroll = ref(true)
const paused = ref(false)
let ws = null
let reconnectTimer = null
const logBox = ref(null)
const seenLogs = new Set()

const levels = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR']

const levelClass = (lv) => ({
  DEBUG: 'lv-debug', INFO: 'lv-info', WARNING: 'lv-warn',
  ERROR: 'lv-err', CRITICAL: 'lv-err',
}[lv] || 'lv-info')

const logTime = (item) => item.date ? `${item.date.slice(5)} ${item.time}` : item.time

const filtered = computed(() => {
  return logs.value.filter((l) => {
    if (levelFilter.value === 'ERROR') {
      if (!['ERROR', 'CRITICAL'].includes(l.level)) return false
    } else if (levelFilter.value !== 'ALL' && l.level !== levelFilter.value) {
      return false
    }
    if (search.value && !(`${l.source} ${l.msg}`.toLowerCase().includes(search.value.toLowerCase()))) return false
    return true
  })
})

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const token = getToken()
  return `${proto}://${location.host}/api/logs/ws?token=${encodeURIComponent(token)}&batch_history=1`
}

function connect() {
  disconnect()
  ws = new WebSocket(wsUrl())
  ws.onopen = () => { connected.value = true }
  ws.onmessage = (e) => {
    if (paused.value) return
    try {
      const item = JSON.parse(e.data)
      if (item.type === 'history' && Array.isArray(item.logs)) {
        logs.value = item.logs.slice(0, 1000)
        seenLogs.clear()
        logs.value.forEach(entry => seenLogs.add(
          entry.id || `${entry.timestamp}|${entry.level}|${entry.source}|${entry.msg}`,
        ))
        nextTick(scrollToLatest)
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
      if (autoScroll.value) nextTick(scrollToLatest)
    } catch {}
  }
  ws.onclose = () => {
    connected.value = false
    reconnectTimer = setTimeout(connect, 3000)
  }
  ws.onerror = () => { ws?.close() }
}

function disconnect() {
  clearTimeout(reconnectTimer)
  if (ws) {
    ws.onclose = null
    ws.close()
    ws = null
  }
  connected.value = false
}

function togglePaused() {
  paused.value = !paused.value
  if (paused.value) disconnect()
  else connect()
}

function scrollToLatest() {
  if (logBox.value) logBox.value.scrollTop = 0
}

function clear() {
  logs.value = []
  seenLogs.clear()
}

onMounted(connect)
onUnmounted(() => {
  disconnect()
})
</script>

<template>
  <div class="logs-page">
    <div class="toolbar">
      <div class="row gap">
        <span class="conn" :class="{ on: connected }">
          <span class="dot"></span>{{ connected ? '实时' : '断开重连中' }}
        </span>
        <div class="level-tabs" aria-label="日志级别筛选">
          <button v-for="lv in levels" :key="lv" type="button"
                  :class="{ active: levelFilter === lv, [`is-${lv.toLowerCase()}`]: true }"
                  @click="levelFilter = lv">{{ lv }}</button>
        </div>
        <input class="input sm" v-model="search" placeholder="搜索插件名/内容…" />
      </div>
      <div class="row gap">
        <label class="chk"><input type="checkbox" v-model="autoScroll" /> 跟随最新</label>
        <button class="btn sm" :class="{ 'btn-primary': paused }" @click="togglePaused">
          {{ paused ? '已暂停' : '暂停' }}
        </button>
        <button class="btn sm" @click="clear">清空</button>
      </div>
    </div>

    <div class="log-box card" ref="logBox">
      <div v-if="filtered.length === 0" class="muted center">暂无日志</div>
      <div v-for="(l, i) in filtered" :key="l.id || `${l.timestamp}-${i}`" class="log-line">
        <span class="time">{{ logTime(l) }}</span>
        <span class="level" :class="levelClass(l.level)">{{ l.level }}</span>
        <span class="msg">{{ l.msg }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.logs-page { display: flex; flex-direction: column; height: 100%; }
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 10px; }
.select.sm, .input.sm { width: auto; padding: 6px 10px; font-size: 12px; }
.input.sm { width: 200px; }
.level-tabs { display: flex; align-items: center; padding: 3px; border: 1px solid var(--border); border-radius: 9px; background: rgba(6,11,19,.62); }
.level-tabs button { min-height: 28px; padding: 0 10px; border: 0; border-radius: 6px; background: transparent; color: var(--text-muted); font: 700 10px/1 inherit; cursor: pointer; }
.level-tabs button:hover { color: var(--text-primary); background: var(--bg-hover); }
.level-tabs button.active { color: var(--text-primary); background: var(--bg-elevated); box-shadow: 0 3px 10px rgba(0,0,0,.2); }
.level-tabs button.active.is-info { color: var(--accent); }
.level-tabs button.active.is-warning { color: var(--warning); }
.level-tabs button.active.is-error { color: var(--danger); }

.conn { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-muted); }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); }
.conn.on { color: var(--accent-2); }
.conn.on .dot { background: var(--accent-2); box-shadow: 0 0 8px var(--accent-2); }

.chk { font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 5px; cursor: pointer; }
.btn.sm { padding: 6px 12px; font-size: 12px; }

.log-box {
  flex: 1; overflow-y: auto; padding: 14px 16px;
  font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12.5px;
  line-height: 1.75; background: rgba(5, 10, 18, .93); overflow-anchor: none;
  border-radius: 13px;
}
.center { text-align: center; padding: 40px; }
.log-line { display: flex; gap: 10px; margin: 0 -8px; padding: 1px 8px; border-radius: 5px; white-space: pre-wrap; word-break: break-all; }
.log-line:hover { background: rgba(48,128,240,.07); }
.time { color: var(--text-muted); flex-shrink: 0; }
.level { flex-shrink: 0; width: 64px; font-weight: 600; }
.lv-debug { color: var(--text-muted); }
.lv-info { color: var(--accent); }
.lv-warn { color: var(--warning); }
.lv-err { color: var(--danger); }
.source { color: var(--accent-2); flex-shrink: 0; }
.msg { color: var(--text-primary); }

/* 手机适配 */
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; gap: 10px; }
  .toolbar .row { flex-wrap: wrap; }
  .level-tabs { width: 100%; overflow-x: auto; }
  .input.sm { width: 100%; }
}
</style>
