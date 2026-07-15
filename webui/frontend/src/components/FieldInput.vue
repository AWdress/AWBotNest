<script setup>
// 单个配置字段的渲染（递归组件）。顶层字段与 list 行内子字段都用它。
// 支持 type：string | password | number | boolean | select | multiselect | slider | text | list | chat | action | info
//   list：可增删行，每行一组子字段（spec.fields）
//   chat：会话选择器，从账号的群/频道/私聊里挑（multi 多选；chat_types 过滤；session 指定账号）
//   action：动作按钮，点击触发插件 ctx.action(name) 注册的函数（spec.action 为动作名；danger 需确认）
//   info：只读展示，显示 spec.text 或当前值（配合 ctx.update_config 可当状态显示）
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { toast } from '../composables/toast'
import { confirm } from '../composables/confirm'

const props = defineProps({
  spec: { type: Object, required: true },
  value: { default: undefined },
  name: { type: String, default: '' },
  pluginId: { type: String, default: '' },
  error: { type: String, default: '' },
})
const emit = defineEmits(['update'])

function set(v) { emit('update', v) }

function normOptions(opts) {
  return (opts || []).map((o) =>
    typeof o === 'object' ? { value: o.value, label: o.label ?? o.value } : { value: o, label: o })
}

function toggleMulti(val) {
  const cur = Array.isArray(props.value) ? [...props.value] : []
  const i = cur.indexOf(val)
  if (i >= 0) cur.splice(i, 1)
  else cur.push(val)
  set(cur)
}

// select 首选项：没写 default（或存档值不在选项内）时，回落到第一个选项，
// 避免「显示第一项、保存却是空值」的错位。挂载时归一一次。
const selectOptions = computed(() => normOptions(props.spec.options))
onMounted(() => {
  if (props.spec.type !== 'select') return
  const opts = selectOptions.value
  if (opts.length && !opts.some((o) => o.value === props.value)) set(opts[0].value)
})

// ── list 行操作 ──
const rows = computed(() => (Array.isArray(props.value) ? props.value : []))

function newRow() {
  const r = {}
  for (const [k, s] of Object.entries(props.spec.fields || {})) {
    if (s.default !== undefined && s.default !== null) { r[k] = s.default; continue }
    if (s.type === 'multiselect') r[k] = []
    else if (s.type === 'boolean') r[k] = false
    else if (s.type === 'number' || s.type === 'slider') r[k] = 0
    else if (s.type === 'select') r[k] = normOptions(s.options)[0]?.value ?? ''
    else r[k] = ''
  }
  return r
}
function addRow() { set([...rows.value, newRow()]) }
function delRow(i) { const a = [...rows.value]; a.splice(i, 1); set(a) }
function setCell(i, k, v) { set(rows.value.map((r, j) => (j === i ? { ...r, [k]: v } : r))) }

// ── chat 会话选择器 ──
const chatOpen = ref(false)
const chatLoading = ref(false)
const chatLoaded = ref(false)
const chatList = ref([])
const chatErr = ref('')
const chatQ = ref('')
const manualId = ref('')

const isMulti = computed(() => !!props.spec.multi)
const selectedIds = computed(() => {
  // Telegram 会话 id 恒非 0，故用真值判断：0 / '' / null 均视为「未选择」
  if (isMulti.value) return Array.isArray(props.value) ? props.value.filter(Boolean) : []
  return props.value ? [props.value] : []
})
function titleOf(id) {
  const c = chatList.value.find((x) => String(x.id) === String(id))
  return c ? c.title : String(id)
}
const filteredChats = computed(() => {
  const types = props.spec.chat_types
  const q = chatQ.value.trim().toLowerCase()
  return chatList.value.filter((c) => {
    if (Array.isArray(types) && types.length && !types.includes(c.type)) return false
    if (q && !String(c.title).toLowerCase().includes(q) && !String(c.id).includes(q)) return false
    return true
  })
})
async function toggleChatPanel() {
  chatOpen.value = !chatOpen.value
  if (!chatOpen.value || chatLoaded.value || chatLoading.value) return
  chatLoading.value = true
  chatErr.value = ''
  try {
    const d = await api.listPluginChats(props.pluginId, props.spec.session || '')
    chatList.value = d.chats || []
    chatLoaded.value = true
  } catch (e) {
    chatErr.value = e.message || '拉取会话失败'
  } finally {
    chatLoading.value = false
  }
}
function isSel(id) { return selectedIds.value.some((x) => String(x) === String(id)) }
function pickChat(id) {
  if (isMulti.value) {
    const cur = [...selectedIds.value]
    const i = cur.findIndex((x) => String(x) === String(id))
    if (i >= 0) cur.splice(i, 1)
    else cur.push(id)
    set(cur)
  } else {
    set(id)
    chatOpen.value = false
  }
}
function removeSel(id) {
  if (isMulti.value) set(selectedIds.value.filter((x) => String(x) !== String(id)))
  else set('')
}
function addManual() {
  const raw = manualId.value.trim()
  if (!raw) return
  const num = Number(raw)
  const val = Number.isFinite(num) && String(num) === raw ? num : raw
  if (isMulti.value) { if (!isSel(val)) set([...selectedIds.value, val]) }
  else set(val)
  manualId.value = ''
}

// ── action 动作按钮 ──
const acting = ref(false)
async function runAction() {
  if (!props.pluginId || acting.value) return
  if (props.spec.danger) {
    const ok = await confirm({ title: props.spec.label || '执行动作', message: '确定执行此操作？', danger: true })
    if (!ok) return
  }
  acting.value = true
  try {
    const r = await api.invokePluginAction(props.pluginId, props.spec.action || props.name)
    if (r.ok === false) toast.error(r.message || '执行失败')
    else toast.success(r.message || '已执行')
  } catch (e) {
    toast.error(e.message || '执行失败')
  } finally {
    acting.value = false
  }
}

// ── info 只读展示 ──
const infoText = computed(() => {
  if (props.spec.text !== undefined && props.spec.text !== null && props.spec.text !== '') return props.spec.text
  return props.value ?? ''
})

const showHead = computed(() => props.spec.type !== 'action')

// 框型单控件（文本/密码/数字/下拉/多行）用 outlined 浮动 label（贴近 MoviePilot）；
// 其余（开关/滑块/多选/会话/列表/说明/按钮）保持 label 在上或各自样式
const BOX_TYPES = ['string', 'password', 'number', 'select', 'text']
const isBoxField = computed(() => BOX_TYPES.includes(props.spec.type))
</script>

<template>
  <div class="field" :class="{ inline: spec.type === 'boolean', box: isBoxField }">
    <div v-if="showHead" class="field-head">
      <label class="field-label">{{ spec.label || name }}<span v-if="spec.required" class="req">*</span></label>
      <!-- boolean → 开关（跟随标签行内） -->
      <div v-if="spec.type === 'boolean'" class="toggle"
           :class="{ on: value }" @click="set(!value)"></div>
      <!-- slider 当前值 -->
      <span v-else-if="spec.type === 'slider'" class="slider-val">{{ value }}</span>
    </div>
    <!-- info → 只读展示 -->
    <div v-if="spec.type === 'info'" class="info-box">
      <svg class="info-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
      <span>{{ infoText || '—' }}</span>
    </div>

    <!-- action → 动作按钮 -->
    <button v-else-if="spec.type === 'action'" type="button" class="action-btn"
            :class="{ danger: spec.danger }" :disabled="acting || !pluginId" @click="runAction">
      {{ acting ? '执行中…' : (spec.label || '执行') }}
    </button>

    <!-- chat → 会话选择器 -->
    <div v-else-if="spec.type === 'chat'" class="chat">
      <div v-if="selectedIds.length" class="chips">
        <span v-for="id in selectedIds" :key="id" class="chip on">
          {{ titleOf(id) }}
          <span class="chip-x" @click="removeSel(id)">×</span>
        </span>
      </div>
      <div v-else class="muted-sm">未选择</div>
      <button type="button" class="chat-toggle" @click="toggleChatPanel">
        {{ chatOpen ? '收起' : (isMulti ? '选择会话' : '选择会话') }}
      </button>
      <div v-if="chatOpen" class="chat-panel">
        <div v-if="chatLoading" class="muted-sm">加载会话中…</div>
        <div v-if="chatErr" class="chat-err">{{ chatErr }}（可在下方手填会话 ID）</div>
        <input v-if="chatLoaded" v-model="chatQ" class="input" placeholder="搜索会话名 / ID" />
        <div v-if="chatLoaded" class="chat-list">
          <div v-for="c in filteredChats" :key="c.id" class="chat-row"
               :class="{ on: isSel(c.id) }" @click="pickChat(c.id)">
            <span class="chat-name">{{ c.title }}</span>
            <span class="chat-type">{{ c.type }}</span>
          </div>
          <div v-if="!filteredChats.length" class="muted-sm">无匹配会话</div>
        </div>
        <div class="chat-manual">
          <input v-model="manualId" class="input" placeholder="手填会话 ID（如 -100123456789）" @keyup.enter="addManual" />
          <button type="button" class="chat-add" @click="addManual">添加</button>
        </div>
      </div>
    </div>

    <!-- list → 可增删行 -->
    <div v-else-if="spec.type === 'list'" class="list">
      <div v-for="(row, i) in rows" :key="i" class="row-card">
        <div class="row-head">
          <span class="row-title">{{ (spec.item_label || '项') + ' ' + (i + 1) }}</span>
          <button type="button" class="row-del" @click="delRow(i)">删除</button>
        </div>
        <FieldInput v-for="(sub, k) in spec.fields" :key="k"
                    :spec="sub" :name="k" :value="row[k]" :plugin-id="pluginId"
                    @update="(v) => setCell(i, k, v)" />
      </div>
      <button type="button" class="row-add" @click="addRow">+ 添加{{ spec.item_label || '一项' }}</button>
    </div>

    <!-- select -->
    <select v-else-if="spec.type === 'select'" class="select"
            :value="value" @change="set($event.target.value)">
      <option v-for="o in selectOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
    </select>

    <!-- multiselect → 多选标签 -->
    <div v-else-if="spec.type === 'multiselect'" class="chips">
      <span v-for="o in normOptions(spec.options)" :key="o.value"
            class="chip" :class="{ on: (value || []).includes(o.value) }"
            @click="toggleMulti(o.value)">{{ o.label }}</span>
    </div>

    <!-- slider -->
    <input v-else-if="spec.type === 'slider'" class="slider" type="range"
           :min="spec.min ?? 0" :max="spec.max ?? 100" :step="spec.step ?? 1"
           :value="value" @input="set(Number($event.target.value))" />

    <!-- number -->
    <input v-else-if="spec.type === 'number'" class="input" type="number"
           :min="spec.min" :max="spec.max" :step="spec.step ?? 1"
           :value="value" @input="set(Number($event.target.value))" />

    <!-- password -->
    <input v-else-if="spec.type === 'password'" class="input" type="password"
           :value="value" @input="set($event.target.value)" />

    <!-- text 多行 -->
    <textarea v-else-if="spec.type === 'text'" class="textarea"
              :value="value" @input="set($event.target.value)"></textarea>

    <!-- string（默认；boolean/slider 无独立输入框） -->
    <input v-else-if="!['boolean', 'slider'].includes(spec.type)" class="input" type="text"
           :value="value" @input="set($event.target.value)" />

    <!-- 字段说明（挪到控件下方） -->
    <div v-if="spec.help" class="field-help">{{ spec.help }}</div>

    <!-- 校验错误 -->
    <div v-if="error" class="field-err">{{ error }}</div>
  </div>
</template>

<style scoped>
.field { display: flex; flex-direction: column; gap: 8px; }
.field.inline .field-head { margin-bottom: 0; }
.field-head { display: flex; align-items: center; justify-content: space-between; }
.field-label { font-size: 13px; color: var(--text-secondary); }
.field-help { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

/* outlined 浮动 label（框型字段）：label 骑在控件左上边框缺口上、控件透明填充（参考 MoviePilot） */
.field.box { position: relative; }
.field.box .field-head {
  position: absolute; top: -8px; left: 9px; z-index: 1;
  width: auto; margin: 0; padding: 0 5px;
  background: var(--bg-card);
}
.field.box .field-label { font-size: 11px; color: var(--text-muted); }
.field.box .input,
.field.box .select,
.field.box .textarea { background: transparent; }
.field.box:focus-within .field-label { color: var(--accent); }
.field.box:focus-within .input,
.field.box:focus-within .select,
.field.box:focus-within .textarea { border-color: var(--accent); }
.req { color: var(--danger, #e5484d); margin-left: 2px; }
.field-err { font-size: 12px; color: var(--danger, #e5484d); }
.muted-sm { font-size: 12px; color: var(--text-muted); }

.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { font-size: 12px; padding: 4px 12px; border-radius: 16px; cursor: pointer;
  background: var(--bg-elevated); border: 1px solid var(--border-light); color: var(--text-secondary); }
.chip.on { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); }
.chip-x { margin-left: 6px; cursor: pointer; font-weight: 700; }

.slider { width: 100%; accent-color: var(--accent); }
.slider-val { font-size: 13px; color: var(--accent); font-weight: 600; }

/* ── info → 蓝色提示条（参考 MoviePilot 的 alert）── */
.info-box {
  display: flex; gap: 10px; align-items: flex-start;
  font-size: 13px; color: var(--text-secondary); line-height: 1.5;
  padding: 12px 14px; border-radius: var(--radius-sm);
  background: var(--accent-dim); border: 1px solid rgba(48, 128, 240, 0.25); white-space: pre-wrap;
}
.info-ico { width: 18px; height: 18px; flex-shrink: 0; margin-top: 1px; color: var(--accent); }

/* ── action ── */
.action-btn {
  align-self: flex-start; font-size: 13px; padding: 8px 16px; border-radius: var(--radius-sm);
  cursor: pointer; background: var(--accent-dim); border: 1px solid var(--accent); color: var(--accent);
}
.action-btn:hover:not(:disabled) { background: var(--accent); color: #fff; }
.action-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.action-btn.danger { background: transparent; border-color: var(--danger, #e5484d); color: var(--danger, #e5484d); }
.action-btn.danger:hover:not(:disabled) { background: var(--danger, #e5484d); color: #fff; }

/* ── chat ── */
.chat { display: flex; flex-direction: column; gap: 8px; }
.chat-toggle {
  align-self: flex-start; font-size: 12px; padding: 5px 12px; border-radius: var(--radius-sm);
  cursor: pointer; background: var(--bg-elevated); border: 1px solid var(--border-light); color: var(--text-secondary);
}
.chat-toggle:hover { border-color: var(--accent); color: var(--accent); }
.chat-panel {
  display: flex; flex-direction: column; gap: 8px;
  padding: 10px; border-radius: var(--radius-sm);
  background: var(--bg-elevated); border: 1px solid var(--border-light);
}
.chat-err { font-size: 12px; color: var(--danger, #e5484d); }
.chat-list { max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
.chat-row {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--text-secondary);
}
.chat-row:hover { background: var(--bg-hover, rgba(127,127,127,0.08)); }
.chat-row.on { background: var(--accent-dim); color: var(--accent); }
.chat-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-type { font-size: 11px; color: var(--text-muted); flex-shrink: 0; }
.chat-manual { display: flex; gap: 8px; }
.chat-manual .input { flex: 1; }
.chat-add {
  font-size: 12px; padding: 0 14px; border-radius: var(--radius-sm); cursor: pointer;
  background: var(--accent-dim); border: 1px solid var(--accent); color: var(--accent);
}

/* ── list 行 ── */
.list { display: flex; flex-direction: column; gap: 12px; }
.row-card {
  display: flex; flex-direction: column; gap: 12px;
  padding: 14px; border-radius: var(--radius-sm);
  background: var(--bg-elevated); border: 1px solid var(--border-light);
}
.row-head { display: flex; align-items: center; justify-content: space-between; }
.row-title { font-size: 12px; font-weight: 600; color: var(--text-secondary); }
.row-del {
  font-size: 12px; padding: 3px 10px; border-radius: 6px; cursor: pointer;
  background: transparent; border: 1px solid var(--border-light); color: var(--text-muted);
}
.row-del:hover { border-color: var(--danger, #e5484d); color: var(--danger, #e5484d); }
.row-add {
  align-self: flex-start; font-size: 13px; padding: 7px 14px; border-radius: var(--radius-sm);
  cursor: pointer; background: var(--accent-dim); border: 1px dashed var(--accent); color: var(--accent);
}
.row-add:hover { background: var(--accent); color: #fff; }
</style>
