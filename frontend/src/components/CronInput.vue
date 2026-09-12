<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { RotateCcw, X } from '@lucide/vue'
import { cronParts, describeCron, isValidCron } from '../utils/cron'

const props = defineProps({
  modelValue: { default: undefined },
  // value / update 保留给平台 Schema 表单及早期接入方。
  value: { default: undefined },
  disabled: { type: Boolean, default: false },
  placeholder: { type: String, default: '例如：6 3 * * *' },
})
const emit = defineEmits(['update:modelValue', 'update', 'change'])

const root = ref(null)
const inputId = useId()
const panelId = `${inputId}-cron-panel`
const open = ref(false)
const draft = ref('')
const panelStyle = ref({})
const externalValue = computed(() => String(
  props.modelValue !== undefined ? props.modelValue : (props.value ?? ''),
))

const pad = (value) => String(value).padStart(2, '0')
const range = (start, end) => Array.from({ length: end - start + 1 }, (_, index) => String(start + index))
const months = range(1, 12)
const days = range(1, 31)
const hours = range(0, 23)
const minutes = range(0, 59)
const weekdays = [
  { value: '1', label: '星期一' }, { value: '2', label: '星期二' },
  { value: '3', label: '星期三' }, { value: '4', label: '星期四' },
  { value: '5', label: '星期五' }, { value: '6', label: '星期六' },
  { value: '0', label: '星期日' },
]

watch(externalValue, (value) => {
  if (value !== draft.value) draft.value = value.trim().replace(/\s+/g, ' ')
}, { immediate: true })

function commit(value, { changed = false } = {}) {
  const next = String(value ?? '')
  draft.value = next
  emit('update:modelValue', next)
  emit('update', next)
  if (changed) emit('change', next)
}

const parsed = computed(() => cronParts(draft.value))
const sixPart = computed(() => parsed.value.length === 6)
const visualParts = computed(() => {
  const parts = parsed.value.length ? parsed.value : ['0', '9', '*', '*', '*']
  return parts.every((item) => item === '*' || /^\d+$/.test(item)) ? parts : []
})
const editable = computed(() => visualParts.value.length > 0)
const indexes = computed(() => sixPart.value
  ? { second: 0, minute: 1, hour: 2, day: 3, month: 4, weekday: 5 }
  : { minute: 0, hour: 1, day: 2, month: 3, weekday: 4 })
const part = (name) => visualParts.value[indexes.value[name]] ?? '*'

function setPart(name, value) {
  if (props.disabled) return
  const parts = [...(visualParts.value.length ? visualParts.value : ['0', '9', '*', '*', '*'])]
  parts[indexes.value[name]] = value
  commit(parts.join(' '), { changed: true })
}

function rawInput(event) {
  commit(event.target.value)
}

function normalize() {
  const normalized = draft.value.trim().replace(/\s+/g, ' ')
  if (normalized !== draft.value) commit(normalized)
  emit('change', normalized)
}

function clearAll() {
  if (props.disabled) return
  commit('', { changed: true })
  open.value = false
}

function resetVisual() {
  commit(sixPart.value ? '0 0 9 * * *' : '0 9 * * *', { changed: true })
}

function openEditor() {
  if (props.disabled) return
  open.value = true
  nextTick(syncPanelLayout)
}

function syncPanelLayout() {
  const grid = root.value?.closest('.fields-grid')
  if (!grid) {
    panelStyle.value = {}
    return
  }
  const gridBox = grid.getBoundingClientRect()
  const rootBox = root.value.getBoundingClientRect()
  panelStyle.value = {
    width: `${Math.round(gridBox.width)}px`,
    marginLeft: `${Math.round(gridBox.left - rootBox.left)}px`,
  }
}

function closeOnOutside(event) {
  if (root.value && !root.value.contains(event.target)) open.value = false
}

onMounted(() => {
  document.addEventListener('pointerdown', closeOnOutside)
  window.addEventListener('resize', syncPanelLayout, { passive: true })
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', closeOnOutside)
  window.removeEventListener('resize', syncPanelLayout)
})

const invalidText = computed(() => draft.value && !isValidCron(draft.value)
  ? 'Cron 应为 5 位或 6 位表达式，例如 6 3 * * *'
  : '')
const advancedText = computed(() => draft.value && isValidCron(draft.value) && !editable.value
  ? describeCron(draft.value)
  : '')
</script>

<template>
  <div ref="root" class="cron-editor" :class="{ open, disabled }" @keydown.esc="open = false">
    <div class="cron-field">
      <input :id="inputId" class="cron-expression" :value="draft" :disabled="disabled"
             :placeholder="placeholder" autocomplete="off" autocapitalize="off" spellcheck="false"
             aria-label="Cron 表达式" :aria-expanded="open" :aria-controls="panelId"
             @focus="openEditor" @click="openEditor" @input="rawInput" @blur="normalize" />
      <button v-if="draft && !disabled" type="button" class="cron-clear" aria-label="清空 Cron 表达式"
              @pointerdown.prevent @click="clearAll">
        <X :size="22" aria-hidden="true" />
      </button>
    </div>

    <Transition name="cron-panel">
      <div v-if="open" :id="panelId" class="cron-panel" :style="panelStyle" aria-label="Cron 可视化编辑器">
        <div v-if="editable" class="cron-sentence">
          <span>每</span>
          <span class="cron-pill cron-fixed">年</span>
          <span>的</span>

          <span class="cron-pill">
            <select :value="part('month')" :disabled="disabled" aria-label="月份"
                    @change="setPart('month', $event.target.value)">
              <option value="*">每月</option>
              <option v-for="item in months" :key="item" :value="item">{{ item }} 月</option>
            </select>
            <button v-if="part('month') !== '*'" type="button" aria-label="恢复每月"
                    @pointerdown.prevent @click="setPart('month', '*')">×</button>
          </span>
          <span>的</span>

          <span class="cron-pill">
            <select :value="part('day')" :disabled="disabled" aria-label="日期"
                    @change="setPart('day', $event.target.value)">
              <option value="*">每日</option>
              <option v-for="item in days" :key="item" :value="item">{{ item }} 日</option>
            </select>
            <button v-if="part('day') !== '*'" type="button" aria-label="恢复每日"
                    @pointerdown.prevent @click="setPart('day', '*')">×</button>
          </span>
          <span>和</span>

          <span class="cron-pill cron-weekday">
            <select :value="part('weekday')" :disabled="disabled" aria-label="星期"
                    @change="setPart('weekday', $event.target.value)">
              <option value="*">一周的每一天</option>
              <option v-for="item in weekdays" :key="item.value" :value="item.value">{{ item.label }}</option>
            </select>
            <button v-if="part('weekday') !== '*'" type="button" aria-label="恢复一周的每一天"
                    @pointerdown.prevent @click="setPart('weekday', '*')">×</button>
          </span>
          <span>的</span>

          <span class="cron-pill cron-time">
            <select :value="part('hour')" :disabled="disabled" aria-label="小时"
                    @change="setPart('hour', $event.target.value)">
              <option value="*">每时</option>
              <option v-for="item in hours" :key="item" :value="item">{{ pad(item) }}</option>
            </select>
            <button v-if="part('hour') !== '*'" type="button" aria-label="清除小时"
                    @pointerdown.prevent @click="setPart('hour', '*')">×</button>
          </span>
          <span class="cron-colon">:</span>
          <span class="cron-pill cron-time">
            <select :value="part('minute')" :disabled="disabled" aria-label="分钟"
                    @change="setPart('minute', $event.target.value)">
              <option value="*">每分</option>
              <option v-for="item in minutes" :key="item" :value="item">{{ pad(item) }}</option>
            </select>
            <button v-if="part('minute') !== '*'" type="button" aria-label="清除分钟"
                    @pointerdown.prevent @click="setPart('minute', '*')">×</button>
          </span>
          <template v-if="sixPart">
            <span class="cron-colon">:</span>
            <span class="cron-pill cron-time">
              <select :value="part('second')" :disabled="disabled" aria-label="秒"
                      @change="setPart('second', $event.target.value)">
                <option value="*">每秒</option>
                <option v-for="item in minutes" :key="item" :value="item">{{ pad(item) }}</option>
              </select>
              <button v-if="part('second') !== '*'" type="button" aria-label="清除秒"
                      @pointerdown.prevent @click="setPart('second', '*')">×</button>
            </span>
          </template>
        </div>

        <div v-else class="cron-advanced" :class="{ invalid: invalidText }">
          <span>{{ invalidText || `${advancedText}，将按原表达式保存。` }}</span>
          <button type="button" @pointerdown.prevent @click="resetVisual">
            <RotateCcw :size="15" aria-hidden="true" />切换为可视化规则
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.cron-editor { position: relative; width: 100%; min-width: 0; }
.cron-field {
  min-height: 68px; display: flex; align-items: center;
  border: 1px solid var(--border-light); border-radius: 14px;
  background: transparent; transition: border-color .16s ease-out, box-shadow .16s ease-out;
}
.cron-editor.open .cron-field { border-color: var(--v2-violet); box-shadow: 0 0 0 1px var(--v2-violet); }
.cron-expression {
  width: 100%; min-width: 0; min-height: 66px; padding: 9px 17px;
  border: 0; outline: 0; color: var(--text-primary); background: transparent;
  font: 500 17px/1.4 'SFMono-Regular', Consolas, monospace; font-variant-numeric: tabular-nums;
}
.cron-expression::placeholder { color: var(--text-muted); font-family: inherit; font-weight: 400; }
.cron-clear {
  width: 42px; height: 42px; margin-right: 6px; flex: 0 0 42px; display: grid; place-items: center;
  border: 0; border-radius: 50%; color: var(--bg-card); background: transparent; cursor: pointer;
}
.cron-clear svg { padding: 3px; border-radius: 50%; background: var(--text-muted); }
.cron-clear:hover svg, .cron-clear:focus-visible svg { background: var(--text-secondary); }
.cron-panel {
  position: relative; z-index: 4; margin-top: -1px; padding: 24px 17px;
  border: 1px solid var(--border-light); border-radius: 0 0 var(--radius) var(--radius);
  color: var(--text-primary); background: var(--bg-card); box-shadow: var(--shadow-soft);
}
.cron-sentence {
  min-height: 50px; display: flex; flex-wrap: wrap; align-items: center; gap: 9px;
  font-size: 18px; line-height: 1.5;
}
.cron-pill {
  --cron-green: #4fc76b;
  min-height: 40px; display: inline-flex; align-items: center;
  border: 1px solid color-mix(in srgb, var(--cron-green) 14%, transparent);
  border-radius: 999px; color: color-mix(in srgb, var(--cron-green) 86%, white);
  background: color-mix(in srgb, var(--cron-green) 14%, var(--bg-elevated));
}
.cron-fixed { padding: 6px 14px; }
.cron-pill select {
  min-width: 0; min-height: 38px; padding: 5px 13px; appearance: none;
  border: 0; outline: 0; color: inherit; background: transparent; cursor: pointer;
  font: inherit; line-height: 1.2; text-align: center;
}
.cron-pill select option { color: var(--text-primary); background: var(--bg-elevated); }
.cron-pill button {
  width: 26px; min-height: 38px; margin-left: -8px; padding: 0 8px 1px 0;
  border: 0; color: color-mix(in srgb, var(--cron-green) 65%, var(--text-muted));
  background: transparent; font: 700 17px/1 sans-serif; cursor: pointer;
}
.cron-pill:focus-within { border-color: var(--cron-green); box-shadow: 0 0 0 2px color-mix(in srgb, var(--cron-green) 20%, transparent); }
.cron-weekday select { min-width: 128px; }
.cron-time select { min-width: 50px; padding-inline: 11px; font-variant-numeric: tabular-nums; }
.cron-colon { margin-inline: -3px; font-size: 20px; }
.cron-advanced { min-height: 46px; display: flex; align-items: center; justify-content: space-between; gap: 14px; color: var(--text-secondary); font-size: 13px; }
.cron-advanced.invalid { color: var(--danger); }
.cron-advanced button {
  min-height: 40px; padding: 0 12px; flex: 0 0 auto; display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--border-light); border-radius: 9px; color: var(--text-secondary);
  background: var(--bg-elevated); cursor: pointer;
}
.cron-advanced button:hover { border-color: var(--accent); color: var(--accent); }
.cron-editor.disabled { opacity: .55; }
.cron-panel-enter-active, .cron-panel-leave-active { transition: opacity .14s ease-out, transform .14s ease-out; }
.cron-panel-enter-from, .cron-panel-leave-to { opacity: 0; transform: translateY(-4px); }

@media (max-width: 640px) {
  .cron-field { min-height: 64px; }
  .cron-expression { min-height: 62px; font-size: 16px; }
  .cron-panel { padding: 15px 13px; box-shadow: none; }
  .cron-sentence { gap: 8px; font-size: 16px; }
  .cron-pill { min-height: 44px; }
  .cron-pill select { min-height: 42px; font-size: 16px; }
  .cron-pill button { min-height: 42px; }
  .cron-advanced { align-items: flex-start; flex-direction: column; }
  .cron-advanced button { width: 100%; justify-content: center; }
}
@media (prefers-reduced-motion: reduce) {
  .cron-field, .cron-panel-enter-active, .cron-panel-leave-active { transition: none; }
}
</style>
