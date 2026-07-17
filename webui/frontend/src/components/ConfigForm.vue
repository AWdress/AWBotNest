<script setup>
// 根据 config_schema 自动渲染配置表单（分区卡片 + 条件显示 + 丰富字段类型 + 自定义排版）。
// 每个字段 spec 支持：
//   type:    string | password | number | boolean | select | multiselect | slider | text | list
//   default, label, help(说明)
//   options: select / multiselect 的可选值（["a","b"] 或 [{value,label}]）
//   min/max/step: number / slider
//   section: 分区标题（同 section 归一组卡片）
//   show_if: 条件显示，如 {"enable_x": true} —— 仅当 enable_x 当前值为 true 才显示本字段
//   list：可增删行的表格，spec.fields 定义每行子字段，item_label 为行标题前缀
//   cols: 栅格列数（1-12），不指定则自动判断（大字段占满行，短字段自动并排）
//   order: 排序权重，数字越小越靠前（同 section 内有效）
import { ref, watch, computed } from 'vue'
import FieldInput from './FieldInput.vue'

const props = defineProps({
  schema: { type: Object, default: () => ({}) },
  modelValue: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const values = ref({ ...props.modelValue })
watch(() => props.modelValue, (v) => { values.value = { ...v } })

const errors = ref({})   // { key: 错误文案 }

function update(key, val) {
  values.value[key] = val
  if (errors.value[key]) { const e = { ...errors.value }; delete e[key]; errors.value = e }  // 改了就清该项错误
  emit('update:modelValue', { ...values.value })
}

// 条件显示：show_if 里所有键值都匹配当前值才显示
function visible(spec) {
  const cond = spec.show_if
  if (!cond || typeof cond !== 'object') return true
  return Object.entries(cond).every(([k, v]) => values.value[k] === v)
}

// 保存前校验：必填 / 数字范围（仅顶层可见字段；info/action 不校验）。返回是否通过。
function validate() {
  const errs = {}
  for (const [key, spec] of Object.entries(props.schema)) {
    if (!visible(spec) || ['info', 'action'].includes(spec.type)) continue
    const v = values.value[key]
    if (spec.required) {
      // chat 的会话 id 恒非 0，故 0 也算未选；其余类型 0 是合法值不算空
      const empty = spec.type === 'chat'
        ? (Array.isArray(v) ? v.length === 0 : !v)
        : (v === undefined || v === null || v === '' || (Array.isArray(v) && v.length === 0))
      if (empty) { errs[key] = '此项必填'; continue }
    }
    if ((spec.type === 'number' || spec.type === 'slider') && v !== '' && v !== null && v !== undefined) {
      if (spec.min !== undefined && v < spec.min) errs[key] = `不能小于 ${spec.min}`
      else if (spec.max !== undefined && v > spec.max) errs[key] = `不能大于 ${spec.max}`
    }
  }
  errors.value = errs
  return Object.keys(errs).length === 0
}

defineExpose({ validate })

// 按 section 分组（无 section 归「常规」），并按 order 排序
const sections = computed(() => {
  const groups = {}
  for (const [key, spec] of Object.entries(props.schema)) {
    const sec = spec.section || '常规'
    if (!groups[sec]) groups[sec] = []
    groups[sec].push([key, spec])
  }
  // 每个分区内按 order 排序（未指定 order 的排最后，按原始顺序）
  for (const sec in groups) {
    groups[sec].sort((a, b) => {
      const orderA = a[1].order ?? 9999
      const orderB = b[1].order ?? 9999
      return orderA - orderB
    })
  }
  return groups
})

const hasFields = computed(() => Object.keys(props.schema).length > 0)

// 大字段（多行文本 / 可增删表格 / 多选 / 会话选择 / 说明 / 按钮）占整行，
// 其余短字段在大画布里自动并排成多列（参考 MoviePilot 的栅格表单）
// 开发者也可以通过 cols 字段自定义列数（1-12）
const WIDE_TYPES = ['text', 'list', 'multiselect', 'chat', 'info', 'action']
function isWide(spec) {
  // 如果指定了 cols=12，强制占满行
  if (spec.cols === 12) return true
  // 否则根据类型自动判断
  return WIDE_TYPES.includes(spec.type)
}

// 计算字段的栅格列数（支持开发者自定义 cols: 1-12）
function getColSpan(spec) {
  if (spec.cols !== undefined) {
    const cols = Math.max(1, Math.min(12, Number(spec.cols) || 1))
    return cols
  }
  // 默认：大字段占 12 列（整行），短字段占 6 列（半行，自动并排）
  return isWide(spec) ? 12 : 6
}
</script>

<template>
  <div class="form">
    <div v-for="(fields, sec) in sections" :key="sec" class="section">
      <div class="section-title">{{ sec }}</div>

      <div class="fields-grid">
        <template v-for="[key, spec] in fields" :key="key">
          <FieldInput v-if="visible(spec)" :spec="spec" :name="key" :plugin-id="pluginId"
                      :style="{ gridColumn: `span ${getColSpan(spec)}` }"
                      :value="values[key]" :error="errors[key]" @update="(v) => update(key, v)" />
        </template>
      </div>
    </div>

    <div v-if="!hasFields" class="empty muted">此插件没有可配置项。</div>
  </div>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: 24px; }
.section { display: flex; flex-direction: column; gap: 16px; }
/* 12 列栅格布局：开发者可通过 cols 字段控制每个字段占几列（1-12）
   未指定时：大字段占 12 列（整行），短字段占 6 列（半行，自动并排）
   容器变窄时字段自然回落（min 280px 确保移动端单列） */
.fields-grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 16px;
  align-items: start;
}
/* 移动端回退到单列 */
@media (max-width: 768px) {
  .fields-grid { grid-template-columns: 1fr; }
  .fields-grid > * { grid-column: span 12 !important; }
}
.section-title {
  font-size: 12px; font-weight: 600; color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.05em;
  padding-bottom: 8px; border-bottom: 1px solid var(--border);
}
.empty { padding: 20px 0; text-align: center; }
</style>
