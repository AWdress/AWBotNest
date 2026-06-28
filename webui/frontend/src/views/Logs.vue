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

const levels = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR']

const levelClass = (lv) => ({
  DEBUG: 'lv-debug', INFO: 'lv-info', WARNING: 'lv-warn',
  ERROR: 'lv-err', CRITICAL: 'lv-err',
}[lv] || 'lv-info')

const filtered = computed(() => {
  return logs.value.filter((l) => {
    if (levelFilter.value !== 'ALL' && l.level !== levelFilter.value) return false
    if (search.value && !(`${l.source} ${l.msg}`.toLowerCase().includes(search.value.toLowerCase()))) return false
    return true
  })
})

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const token = getToken()
  return `${proto}://${location.host}/api/logs/ws?token=${encodeURIComponent(token)}`
}

function connect() {
  ws = new WebSocket(wsUrl())
  ws.onopen = () => { connected.value = true }
  ws.onmessage = (e) => {
    if (paused.value) return
    try {
      const item = JSON.parse(e.data)
      logs.value.push(item)
      if (logs.value.length > 1000) logs.value.splice(0, logs.value.length - 1000)
      if (autoScroll.value) nextTick(scrollToBottom)
    } catch {}
  }
  ws.onclose = () => {
    connected.value = false
    reconnectTimer = setTimeout(connect, 3000)
  }
  ws.onerror = () => { ws?.close() }
}

function scrollToBottom() {
  if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
}

function clear() { logs.value = [] }

onMounted(connect)
onUnmounted(() => {
  clearTimeout(reconnectTimer)
  if (ws) { ws.onclose = null; ws.close() }
})
</script>

<template>
  <div class="logs-page">
    <div class="toolbar">
      <div class="row gap">
        <span class="conn" :class="{ on: connected }">
          <span class="dot"></span>{{ connected ? '实时' : '断开重连中' }}
        </span>
        <select class="select sm" v-model="levelFilter">
          <option v-for="lv in levels" :key="lv" :value="lv">{{ lv }}</option>
        </select>
        <input class="input sm" v-model="search" placeholder="搜索插件名/内容…" />
      </div>
      <div class="row gap">
        <label class="chk"><input type="checkbox" v-model="autoScroll" /> 自动滚动</label>
        <button class="btn sm" :class="{ 'btn-primary': paused }" @click="paused = !paused">
          {{ paused ? '已暂停' : '暂停' }}
        </button>
        <button class="btn sm" @click="clear">清空</button>
      </div>
    </div>

    <div class="log-box card" ref="logBox">
      <div v-if="filtered.length === 0" class="muted center">暂无日志</div>
      <div v-for="(l, i) in filtered" :key="i" class="log-line">
        <span class="time">{{ l.time }}</span>
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

.conn { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-muted); }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); }
.conn.on { color: var(--accent-2); }
.conn.on .dot { background: var(--accent-2); box-shadow: 0 0 8px var(--accent-2); }

.chk { font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 5px; cursor: pointer; }
.btn.sm { padding: 6px 12px; font-size: 12px; }

.log-box {
  flex: 1; overflow-y: auto; padding: 14px 16px;
  font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12.5px;
  line-height: 1.7; background: #07090f;
}
.center { text-align: center; padding: 40px; }
.log-line { display: flex; gap: 10px; white-space: pre-wrap; word-break: break-all; }
.log-line:hover { background: rgba(255,255,255,0.03); }
.time { color: var(--text-muted); flex-shrink: 0; }
.level { flex-shrink: 0; width: 64px; font-weight: 600; }
.lv-debug { color: var(--text-muted); }
.lv-info { color: var(--accent); }
.lv-warn { color: var(--warning); }
.lv-err { color: var(--danger); }
.source { color: var(--accent-2); flex-shrink: 0; }
.msg { color: var(--text-primary); }
</style>
