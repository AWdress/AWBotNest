<script setup>
import { computed, ref, useId, watch } from 'vue'
import { CalendarClock, Check, ChevronDown, X } from '@lucide/vue'
import { cronParts, describeCron, isValidCron, nextCronRuns, simpleCron } from '../utils/cron'

const props = defineProps({ value: { default: '' } })
const emit = defineEmits(['update'])
const rawInputId = useId()

const open = ref(false)
const manual = ref(false)
const draft = ref('')
const six = ref(false)
const mode = ref('daily')
const second = ref('0')
const minute = ref('0')
const hour = ref('9')
const day = ref('1')
const weekday = ref('1')

const hours = Array.from({ length: 24 }, (_, index) => String(index))
const minutes = Array.from({ length: 60 }, (_, index) => String(index))
const days = Array.from({ length: 31 }, (_, index) => String(index + 1))
const weekdays = [
  { value: '1', label: '星期一' }, { value: '2', label: '星期二' },
  { value: '3', label: '星期三' }, { value: '4', label: '星期四' },
  { value: '5', label: '星期五' }, { value: '6', label: '星期六' },
  { value: '0', label: '星期日' },
]
const modes = [
  { value: 'daily', label: '每天' }, { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' }, { value: 'hourly', label: '每小时' },
  { value: 'custom', label: '自定义' },
]

function sync(value) {
  draft.value = String(value ?? '').trim().replace(/\s+/g, ' ')
  const parsed = simpleCron(draft.value)
  const parts = cronParts(draft.value)
  six.value = parts.length === 6
  if (!parsed) {
    mode.value = draft.value ? 'custom' : 'daily'
    manual.value = Boolean(draft.value)
    return
  }
  mode.value = parsed.mode
  second.value = parsed.second
  minute.value = parsed.minute
  if (parsed.hour !== '*') hour.value = parsed.hour
  if (parsed.day !== '*') day.value = parsed.day
  if (parsed.weekday !== '*') weekday.value = String(Number(parsed.weekday) % 7)
}

watch(() => props.value, sync, { immediate: true })

function expression() {
  let parts
  if (mode.value === 'hourly') parts = [minute.value, '*', '*', '*', '*']
  else if (mode.value === 'weekly') parts = [minute.value, hour.value, '*', '*', weekday.value]
  else if (mode.value === 'monthly') parts = [minute.value, hour.value, day.value, '*', '*']
  else parts = [minute.value, hour.value, '*', '*', '*']
  return (six.value ? [second.value, ...parts] : parts).join(' ')
}

function updateBuilder() {
  if (mode.value === 'custom') { manual.value = true; return }
  manual.value = false
  draft.value = expression()
  emit('update', draft.value)
}

function selectMode(value) {
  mode.value = value
  updateBuilder()
}

function updateRaw(event) {
  draft.value = event.target.value
  emit('update', draft.value.trim().replace(/\s+/g, ' '))
}

function clear() {
  draft.value = ''
  open.value = false
  emit('update', '')
}

const description = computed(() => draft.value ? describeCron(draft.value) : '尚未设置执行周期')
const nextRuns = computed(() => nextCronRuns(draft.value))
const formatter = computed(() => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  weekday: 'short', hour: '2-digit', minute: '2-digit',
  second: six.value ? '2-digit' : undefined, hourCycle: 'h23',
}))
function formatRun(value) { return formatter.value.format(value).replace(/\//g, '-') }
</script>

<template>
  <div class="cron-editor" @keydown.esc="open = false">
    <div class="cron-trigger" :class="{ open }">
      <button type="button" class="cron-open" :aria-expanded="open"
              aria-label="编辑 Cron 执行周期" @click="open = !open">
        <CalendarClock :size="18" aria-hidden="true" />
        <span class="cron-trigger-copy">
          <code>{{ String(value || '').trim() || '点击设置 Cron' }}</code>
          <span>{{ description }}</span>
        </span>
        <ChevronDown class="cron-chevron" :size="18" aria-hidden="true" />
      </button>
      <button v-if="value" type="button" class="cron-clear" aria-label="清空执行周期"
              @click.stop="clear"><X :size="17" aria-hidden="true" /></button>
    </div>

    <Transition name="cron-panel">
      <section v-if="open" class="cron-panel" aria-label="Cron 编辑器">
        <div class="cron-mode" role="radiogroup" aria-label="执行周期类型">
          <button v-for="item in modes" :key="item.value" type="button" role="radio"
                  :aria-checked="mode === item.value" :class="{ active: mode === item.value }"
                  @click="selectMode(item.value)">{{ item.label }}</button>
        </div>

        <div v-if="mode !== 'custom'" class="cron-controls">
          <label v-if="mode === 'monthly'">
            <span>日期</span>
            <select v-model="day" @change="updateBuilder"><option v-for="item in days" :key="item" :value="item">{{ item }} 日</option></select>
          </label>
          <label v-if="mode === 'weekly'">
            <span>星期</span>
            <select v-model="weekday" @change="updateBuilder"><option v-for="item in weekdays" :key="item.value" :value="item.value">{{ item.label }}</option></select>
          </label>
          <label v-if="mode !== 'hourly'">
            <span>小时</span>
            <select v-model="hour" @change="updateBuilder"><option v-for="item in hours" :key="item" :value="item">{{ String(item).padStart(2, '0') }} 时</option></select>
          </label>
          <label>
            <span>分钟</span>
            <select v-model="minute" @change="updateBuilder"><option v-for="item in minutes" :key="item" :value="item">{{ String(item).padStart(2, '0') }} 分</option></select>
          </label>
          <label v-if="six">
            <span>秒</span>
            <select v-model="second" @change="updateBuilder"><option v-for="item in minutes" :key="item" :value="item">{{ String(item).padStart(2, '0') }} 秒</option></select>
          </label>
        </div>

        <div class="cron-summary" :class="{ invalid: draft && !isValidCron(draft) }">
          <strong>{{ description }}</strong>
          <div v-if="nextRuns.length" class="cron-next">
            <span>接下来</span>
            <time v-for="item in nextRuns" :key="item.toISOString()" :datetime="item.toISOString()">{{ formatRun(item) }}</time>
          </div>
          <span v-else-if="draft && isValidCron(draft)">复杂规则将按原表达式保存，运行时间由插件解析。</span>
          <span v-else-if="draft">请输入 5 位或 6 位 Cron，例如 6 3 * * *</span>
        </div>

        <div v-if="mode === 'custom' || manual" class="cron-manual">
          <label :for="rawInputId">Cron 表达式</label>
          <input :id="rawInputId" :value="draft" inputmode="text" autocomplete="off"
                 placeholder="6 3 * * *" @input="updateRaw" />
          <span>支持 5 位和 6 位格式，复杂表达式会原样交给插件。</span>
        </div>

        <div class="cron-actions">
          <button v-if="mode !== 'custom'" type="button" class="manual-btn" @click="manual = !manual">
            {{ manual ? '收起表达式' : '手动编辑' }}
          </button>
          <button type="button" class="done-btn" @click="open = false"><Check :size="16" />完成</button>
        </div>
      </section>
    </Transition>
  </div>
</template>

<style scoped>
.cron-editor { width: 100%; min-width: 0; }
.cron-trigger {
  width: 100%; min-height: 58px; display: flex; align-items: center; color: var(--text-primary); background: transparent;
  border: 1px solid var(--border-light); border-radius: var(--radius-sm); cursor: pointer;
  text-align: left; transition: border-color .18s ease-out, background .18s ease-out;
}
.cron-trigger:hover, .cron-trigger.open { border-color: var(--accent); background: var(--accent-dim); }
.cron-open { min-width: 0; min-height: 56px; padding: 9px 10px 9px 12px; flex: 1; display: flex; align-items: center; gap: 11px; color: inherit; background: transparent; border: 0; text-align: left; cursor: pointer; }
.cron-open:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: calc(var(--radius-sm) - 2px); }
.cron-open > svg:first-child { flex: 0 0 auto; color: var(--accent); }
.cron-trigger-copy { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 3px; }
.cron-trigger-copy code { overflow: hidden; color: var(--text-primary); font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.cron-trigger-copy span { overflow: hidden; color: var(--text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.cron-clear { width: 38px; height: 38px; margin-right: 7px; flex: 0 0 auto; display: grid; place-items: center; border: 0; border-radius: 8px; color: var(--text-muted); background: transparent; cursor: pointer; }
.cron-clear:hover { color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, transparent); }
.cron-chevron { flex: 0 0 auto; color: var(--text-muted); transition: transform .18s ease-out; }
.cron-trigger.open .cron-chevron { transform: rotate(180deg); }
.cron-panel {
  margin-top: 10px; padding: 16px; border: 1px solid var(--border-light); border-radius: var(--radius-sm);
  background: var(--bg-elevated); box-shadow: 0 12px 30px rgba(0, 0, 0, .16);
}
.cron-mode { display: flex; flex-wrap: wrap; gap: 7px; }
.cron-mode button { min-height: 36px; padding: 0 13px; border: 1px solid var(--border-light); border-radius: 999px; color: var(--text-secondary); background: var(--bg-card); cursor: pointer; }
.cron-mode button:hover { border-color: var(--accent); color: var(--accent); }
.cron-mode button.active { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); font-weight: 650; }
.cron-controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(112px, 1fr)); gap: 10px; margin-top: 14px; }
.cron-controls label { display: flex; min-width: 0; flex-direction: column; gap: 6px; }
.cron-controls label span, .cron-manual label { color: var(--text-muted); font-size: 12px; }
.cron-controls select, .cron-manual input { width: 100%; min-height: 42px; padding: 0 10px; color: var(--text-primary); background: var(--bg-card); border: 1px solid var(--border-light); border-radius: 9px; font-size: 14px; }
.cron-controls select:focus, .cron-manual input:focus { border-color: var(--accent); outline: 2px solid color-mix(in srgb, var(--accent) 28%, transparent); outline-offset: 1px; }
.cron-summary { margin-top: 14px; padding: 12px 13px; display: flex; flex-direction: column; gap: 7px; color: var(--text-secondary); background: var(--bg-card); border-radius: 10px; }
.cron-summary strong { color: var(--text-primary); font-size: 13px; }
.cron-summary > span, .cron-next { color: var(--text-muted); font-size: 12px; line-height: 1.5; }
.cron-summary.invalid strong, .cron-summary.invalid > span { color: var(--danger); }
.cron-next { display: flex; flex-wrap: wrap; gap: 6px 10px; }
.cron-next time { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.cron-manual { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
.cron-manual > span { color: var(--text-muted); font-size: 12px; }
.cron-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.cron-actions button { min-height: 40px; padding: 0 14px; border-radius: 9px; cursor: pointer; }
.manual-btn { margin-right: auto; color: var(--text-secondary); background: transparent; border: 1px solid var(--border-light); }
.done-btn { display: inline-flex; align-items: center; gap: 6px; color: #fff; background: var(--accent); border: 1px solid var(--accent); font-weight: 650; }
.cron-panel-enter-active, .cron-panel-leave-active { transition: opacity .16s ease-out, transform .16s ease-out; }
.cron-panel-enter-from, .cron-panel-leave-to { opacity: 0; transform: translateY(-5px); }
@media (max-width: 640px) {
  .cron-trigger { min-height: 62px; }
  .cron-open { min-height: 60px; padding-inline: 11px 8px; }
  .cron-panel { margin-inline: -2px; padding: 14px 12px max(14px, env(safe-area-inset-bottom)); box-shadow: none; }
  .cron-mode { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; scrollbar-width: thin; }
  .cron-mode button { min-width: max-content; min-height: 42px; }
  .cron-controls { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .cron-controls select, .cron-manual input { min-height: 46px; font-size: 16px; }
  .cron-actions button { min-height: 44px; }
}
@media (prefers-reduced-motion: reduce) {
  .cron-trigger, .cron-chevron, .cron-panel-enter-active, .cron-panel-leave-active { transition: none; }
}
</style>
