<script setup>
// 根据 config_schema 自动渲染配置表单（分区卡片 + 条件显示 + 丰富字段类型）。
// 每个字段 spec 支持：
//   type:    string | password | number | boolean | select | multiselect | slider | text
//   default, label, help(说明)
//   options: select / multiselect 的可选值（["a","b"] 或 [{value,label}]）
//   min/max/step: number / slider
//   section: 分区标题（同 section 归一组卡片）
//   show_if: 条件显示，如 {"enable_x": true} —— 仅当 enable_x 当前值为 true 才显示本字段
import { ref, watch, computed } from 'vue'

const props = defineProps({
  schema: { type: Object, default: () => ({}) },
  modelValue: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:modelValue'])

const values = ref({ ...props.modelValue })
watch(() => props.modelValue, (v) => { values.value = { ...v } })

function update(key, val) {
  values.value[key] = val
  emit('update:modelValue', { ...values.value })
}

// 条件显示：show_if 里所有键值都匹配当前值才显示
function visible(spec) {
  const cond = spec.show_if
  if (!cond || typeof cond !== 'object') return true
  return Object.entries(cond).every(([k, v]) => values.value[k] === v)
}

// 归一化 options 为 [{value,label}]
function normOptions(opts) {
  return (opts || []).map((o) =>
    typeof o === 'object' ? { value: o.value, label: o.label ?? o.value } : { value: o, label: o })
}

// multiselect 切换
function toggleMulti(key, val) {
  const cur = Array.isArray(values.value[key]) ? [...values.value[key]] : []
  const i = cur.indexOf(val)
  if (i >= 0) cur.splice(i, 1)
  else cur.push(val)
  update(key, cur)
}

// 按 section 分组（无 section 归「常规」）
const sections = computed(() => {
  const groups = {}
  for (const [key, spec] of Object.entries(props.schema)) {
    const sec = spec.section || '常规'
    if (!groups[sec]) groups[sec] = []
    groups[sec].push([key, spec])
  }
  return groups
})

const hasFields = computed(() => Object.keys(props.schema).length > 0)
</script>

<template>
  <div class="form">
    <div v-for="(fields, sec) in sections" :key="sec" class="section">
      <div class="section-title">{{ sec }}</div>

      <template v-for="[key, spec] in fields" :key="key">
        <div v-if="visible(spec)" class="field" :class="{ inline: spec.type === 'boolean' }">
          <div class="field-head">
            <label class="field-label">{{ spec.label || key }}</label>
            <!-- boolean → 开关 -->
            <div v-if="spec.type === 'boolean'" class="toggle"
                 :class="{ on: values[key] }" @click="update(key, !values[key])"></div>
            <!-- slider 当前值 -->
            <span v-else-if="spec.type === 'slider'" class="slider-val">{{ values[key] }}</span>
          </div>
          <div v-if="spec.help" class="field-help">{{ spec.help }}</div>

          <!-- select -->
          <select v-if="spec.type === 'select'" class="select"
                  :value="values[key]" @change="update(key, $event.target.value)">
            <option v-for="o in normOptions(spec.options)" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>

          <!-- multiselect → 多选标签 -->
          <div v-else-if="spec.type === 'multiselect'" class="chips">
            <span v-for="o in normOptions(spec.options)" :key="o.value"
                  class="chip" :class="{ on: (values[key]||[]).includes(o.value) }"
                  @click="toggleMulti(key, o.value)">{{ o.label }}</span>
          </div>

          <!-- slider -->
          <input v-else-if="spec.type === 'slider'" class="slider" type="range"
                 :min="spec.min ?? 0" :max="spec.max ?? 100" :step="spec.step ?? 1"
                 :value="values[key]" @input="update(key, Number($event.target.value))" />

          <!-- number -->
          <input v-else-if="spec.type === 'number'" class="input" type="number"
                 :min="spec.min" :max="spec.max" :step="spec.step ?? 1"
                 :value="values[key]" @input="update(key, Number($event.target.value))" />

          <!-- password -->
          <input v-else-if="spec.type === 'password'" class="input" type="password"
                 :value="values[key]" @input="update(key, $event.target.value)" />

          <!-- text 多行 -->
          <textarea v-else-if="spec.type === 'text'" class="textarea"
                    :value="values[key]" @input="update(key, $event.target.value)"></textarea>

          <!-- string（默认；boolean/slider 无输入框） -->
          <input v-else-if="!['boolean','slider'].includes(spec.type)" class="input" type="text"
                 :value="values[key]" @input="update(key, $event.target.value)" />
        </div>
      </template>
    </div>

    <div v-if="!hasFields" class="empty muted">此插件没有可配置项。</div>
  </div>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: 24px; }
.section { display: flex; flex-direction: column; gap: 16px; }
.section-title {
  font-size: 12px; font-weight: 600; color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.05em;
  padding-bottom: 8px; border-bottom: 1px solid var(--border);
}
.field { display: flex; flex-direction: column; gap: 8px; }
.field.inline .field-head { margin-bottom: 0; }
.field-head { display: flex; align-items: center; justify-content: space-between; }
.field-label { font-size: 13px; color: var(--text-secondary); }
.field-help { font-size: 12px; color: var(--text-muted); margin-top: -2px; }
.empty { padding: 20px 0; text-align: center; }

.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { font-size: 12px; padding: 4px 12px; border-radius: 16px; cursor: pointer;
  background: var(--bg-elevated); border: 1px solid var(--border-light); color: var(--text-secondary); }
.chip.on { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); }

.slider { width: 100%; accent-color: var(--accent); }
.slider-val { font-size: 13px; color: var(--accent); font-weight: 600; }
</style>
