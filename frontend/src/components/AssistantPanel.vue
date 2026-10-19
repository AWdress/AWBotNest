<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Bot, Check, ChevronDown, Clock3, Expand, Minimize2, Plus, Send, Settings2, Square, Trash2, X } from '@lucide/vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { api } from '../api'
import { confirm, confirmState } from '../composables/confirm'
import { toast } from '../composables/toast'
import CronInput from './CronInput.vue'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close'])
const tabs = [
  { id: 'chat', label: '对话' }, { id: 'tasks', label: '预约' },
  { id: 'memories', label: '记忆' }, { id: 'skills', label: '技能' },
  { id: 'settings', label: '设置' },
]
const defaults = {
  enabled: true, model: '', allow_actions: false, allowed_plugins: [],
  max_rounds: 8, max_tool_calls: 16, max_tokens: 32000, timeout_seconds: 120, memory_enabled: true, notify_tasks: false,
  allow_issue_submission: false, issue_repositories: ['AWdress/AWBotNest'],
}
const catalog = ref({ sessions: [], models: [], skills: [], memories: [], tasks: [], tools: [], settings: { ...defaults }, issue_status: { configured: false, enabled: false, repositories: [] } })
const settingsDraft = ref({ ...defaults, allowed_plugins: [], issue_repositories: [...defaults.issue_repositories] })
const issueRepositoriesText = computed({
  get: () => settingsDraft.value.issue_repositories.join('\n'),
  set: value => { settingsDraft.value.issue_repositories = value.split('\n') },
})
const pluginOptions = ref([])
const activeTab = ref('chat')
const fullscreen = ref(false)
const loading = ref(false)
const sessionLoading = ref(false)
const loadError = ref('')
const pollError = ref('')
const busy = ref('')
const selectedId = ref('')
const session = ref(null)
const drafts = ref({})
const draftRevisions = {}
const dialog = ref(null)
const transcript = ref(null)
const confirmationCard = ref(null)
const composer = ref(null)
const chatDraft = computed({
  get: () => drafts.value[selectedId.value || 'new'] || '',
  set: (value) => {
    const key = selectedId.value || 'new'
    drafts.value[key] = value
    draftRevisions[key] = (draftRevisions[key] || 0) + 1
  },
})
const run = computed(() => session.value?.run || null)
const activeRun = computed(() => ['queued', 'running', 'waiting_confirmation'].includes(run.value?.status))
const pending = computed(() => run.value?.pending_confirmation)
const pendingIssue = computed(() => {
  if (pending.value?.tool !== 'submit_github_issue') return null
  try {
    const value = typeof pending.value.arguments === 'string' ? JSON.parse(pending.value.arguments) : pending.value.arguments
    return value && ['repository', 'title', 'body'].every(key => typeof value[key] === 'string') ? value : null
  } catch { return null }
})
const pendingExpired = computed(() => pending.value?.expires_at && new Date(pending.value.expires_at).getTime() <= Date.now())
const statusLabels = {
  queued: '排队中', running: '执行中', waiting_confirmation: '等待确认',
  completed: '已完成', failed: '失败', cancelled: '已停止', interrupted: '已中断',
  pending: '等待中', succeeded: '已完成', skipped: '已跳过',
}
const runLabel = computed(() => statusLabels[run.value?.status] || '')
const liveAnnouncement = ref('')
const announcementSequence = ref(0)
watch(() => JSON.stringify([
  props.open, loading.value, sessionLoading.value, selectedId.value, run.value?.id, run.value?.status, pending.value?.id, !!pendingExpired.value,
]), () => {
  if (!props.open || loading.value || sessionLoading.value || !run.value?.id) { liveAnnouncement.value = ''; return }
  liveAnnouncement.value = pending.value
    ? (pendingExpired.value ? '系统助手的操作确认已过期，请重新发起任务。' : '系统助手有操作等待管理员确认，请在对话中查看操作内容。')
    : `系统助手任务${runLabel.value || '状态已更新'}。`
  // Replace the short announcement only on a state transition or a new
  // confirmation, never on an otherwise identical polling response.
  announcementSequence.value += 1
}, { flush: 'post' })
const memoryDraft = ref('')
const memoryEditingId = ref('')
const skillDraft = ref({ name: '', description: '', content: '' })
const skillEditorOpen = ref(false)
const skillDetails = ref({})
const skillLoading = ref({})
const skillLoadErrors = ref({})
const builtinSkillCount = computed(() => catalog.value.skills.filter(item => item.builtin).length)
const customSkillCount = computed(() => catalog.value.skills.filter(item => !item.builtin).length)
const taskDraft = ref({ name: '', prompt: '', mode: 'once', run_at: '', cron: '0 9 * * *' })
// Revisions distinguish even an edit that happens to return to the submitted value.
// Save responses must not clear text entered while the request was in flight.
const formRevisions = { settings: 0, memory: 0, skill: 0, task: 0 }
watch(settingsDraft, () => { formRevisions.settings++ }, { deep: true, flush: 'sync' })
watch([memoryDraft, memoryEditingId], () => { formRevisions.memory++ }, { flush: 'sync' })
watch(skillDraft, () => { formRevisions.skill++ }, { deep: true, flush: 'sync' })
watch(taskDraft, () => { formRevisions.task++ }, { deep: true, flush: 'sync' })
const canSend = computed(() => !!chatDraft.value.trim() && !busy.value && !loading.value && !sessionLoading.value && catalog.value.settings.enabled)
const skillControllers = new Map()
let requestController = null
let overviewController = null
let overviewSequence = 0
let pollTimer = null
let openSequence = 0
let sessionSequence = 0
let previousFocus = null
let disposed = false

function markdown(content) {
  return DOMPurify.sanitize(marked.parse(String(content || ''), { breaks: true }), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe', 'img', 'audio', 'video', 'source', 'picture', 'object', 'embed'],
    FORBID_ATTR: ['style', 'src', 'srcset', 'poster', 'background', 'data', 'xlink:href'],
  })
}
function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
function tokens(value) { return Number(value || 0).toLocaleString('zh-CN') }
function settingsValue(value) {
  return { ...defaults, ...value, allowed_plugins: [...(value?.allowed_plugins || [])], issue_repositories: [...(value?.issue_repositories || defaults.issue_repositories)] }
}
function composerKeydown(event) {
  if (event.key !== 'Enter' || event.isComposing || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return
  if (window.matchMedia('(pointer: coarse)').matches) return
  event.preventDefault()
  sendMessage()
}
function stepContent(value) { return typeof value === 'string' ? value : (value == null ? '' : JSON.stringify(value, null, 2)) }
function nearBottom() {
  const el = transcript.value
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 88
}
async function scrollToBottom(shouldScroll = true) {
  await nextTick()
  if (shouldScroll && transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
}
function stopPolling() {
  clearTimeout(pollTimer)
  pollTimer = null
  requestController?.abort()
  requestController = null
}
function schedulePoll() {
  clearTimeout(pollTimer)
  if (disposed || !props.open || document.hidden || !activeRun.value || !selectedId.value) return
  pollTimer = setTimeout(() => loadSession(selectedId.value, { polling: true }), 1500)
}
function updateSessionSummary(value) {
  if (!value?.id) return
  const { messages, run: currentRun, ...summary } = value
  const index = catalog.value.sessions.findIndex(item => item.id === value.id)
  if (index < 0) catalog.value.sessions.unshift(summary)
  else catalog.value.sessions[index] = { ...catalog.value.sessions[index], ...summary }
}
async function loadSession(id, { polling = false } = {}) {
  stopPolling()
  if (!props.open || document.hidden || disposed) return
  const sequence = ++sessionSequence
  const opening = openSequence
  const shouldScroll = !polling || nearBottom()
  const controller = new AbortController()
  requestController = controller
  if (!polling) sessionLoading.value = true
  const current = () => !disposed && props.open && opening === openSequence && sequence === sessionSequence && id === selectedId.value
  try {
    const value = await api.getAssistantSession(id, controller.signal)
    if (!current()) return
    const newConfirmation = value.run?.pending_confirmation?.id && value.run.pending_confirmation.id !== pending.value?.id
    session.value = value
    updateSessionSummary(value)
    pollError.value = ''
    sessionLoading.value = false
    if (shouldScroll && newConfirmation) {
      await nextTick()
      if (transcript.value && confirmationCard.value) {
        const top = confirmationCard.value.getBoundingClientRect().top - transcript.value.getBoundingClientRect().top
        transcript.value.scrollTop += top - 12
      }
    } else await scrollToBottom(shouldScroll && !value.run?.pending_confirmation)
  } catch (error) {
    if (error.name === 'AbortError' || !current()) return
    pollError.value = `会话读取失败：${error.message}`
  } finally {
    if (current()) {
      sessionLoading.value = false
      if (requestController === controller) requestController = null
      schedulePoll()
    }
  }
}
async function selectSession(id) {
  selectedId.value = id
  session.value = null
  pollError.value = ''
  if (id) await loadSession(id)
  else { stopPolling(); sessionLoading.value = false }
}
async function loadOverview(initialize = false) {
  if (!props.open || disposed) return
  const opening = openSequence
  const sequence = ++overviewSequence
  const settingsRevision = formRevisions.settings
  overviewController?.abort()
  if (initialize) { loading.value = true; loadError.value = '' }
  const controller = new AbortController()
  overviewController = controller
  try {
    const value = await api.getAssistant(controller.signal)
    if (disposed || !props.open || opening !== openSequence || sequence !== overviewSequence) return
    catalog.value = { ...catalog.value, ...value }
    if (initialize) {
      if (settingsRevision === formRevisions.settings) settingsDraft.value = settingsValue(value.settings)
      const id = value.sessions?.some(item => item.id === selectedId.value) ? selectedId.value : (value.sessions?.[0]?.id || '')
      loading.value = false
      await selectSession(id)
    }
  } catch (error) {
    if (error.name !== 'AbortError' && opening === openSequence && sequence === overviewSequence && props.open) {
      if (initialize) loadError.value = error.message
      else toast.error(`助手资料读取失败：${error.message}`)
    }
  } finally {
    if (opening === openSequence) loading.value = false
    if (overviewController === controller) overviewController = null
  }
}
async function newSession() {
  if (busy.value) return
  busy.value = 'session'
  try {
    const value = await api.createAssistantSession()
    updateSessionSummary(value)
    await selectSession(value.id)
    activeTab.value = 'chat'
    await nextTick()
    composer.value?.focus()
  } catch (error) { toast.error(`创建会话失败：${error.message}`) }
  finally { busy.value = '' }
}
async function deleteSession() {
  const id = selectedId.value
  if (!id || busy.value || activeRun.value) return
  if (!await confirm({ title: '删除会话', message: '删除后无法恢复。', danger: true, confirmText: '删除' })) return
  busy.value = 'delete-session'
  try {
    await api.deleteAssistantSession(id)
    catalog.value.sessions = catalog.value.sessions.filter(item => item.id !== id)
    delete drafts.value[id]
    if (selectedId.value === id) await selectSession(catalog.value.sessions[0]?.id || '')
  } catch (error) { toast.error(`删除失败：${error.message}`) }
  finally { busy.value = '' }
}
async function sendMessage() {
  if (!canSend.value) return
  const content = chatDraft.value.trim()
  let draftKey = selectedId.value || 'new'
  const submittedRevision = draftRevisions[draftKey] || 0
  let id = selectedId.value
  const currentRun = run.value
  const steering = activeRun.value
  busy.value = 'send'
  try {
    if (!id) {
      const created = await api.createAssistantSession({ title: content.slice(0, 40) })
      id = created.id
      updateSessionSummary(created)
      // Migrate the latest draft now, not in catch: input may have changed while
      // creating the session, and may change again while sending its message.
      if (selectedId.value === '') {
        drafts.value[id] = drafts.value.new || ''
        draftRevisions[id] = draftRevisions.new || 0
        delete drafts.value.new
        delete draftRevisions.new
        draftKey = id
        selectedId.value = id
      }
    }
    const result = steering
      ? await api.steerAssistantRun(currentRun.id, content)
      : await api.sendAssistantMessage(id, content)
    if ((draftRevisions[draftKey] || 0) === submittedRevision && (drafts.value[draftKey] || '').trim() === content) drafts.value[draftKey] = ''
    if (selectedId.value === id && props.open) {
      if (!session.value) session.value = { id, messages: [], run: result }
      else session.value.run = result
      await loadSession(id)
    }
  } catch (error) {
    toast.error(`发送失败：${error.message}`)
  } finally { busy.value = '' }
}
async function actOnRun(action, approved) {
  const currentRun = run.value
  const id = selectedId.value
  if (!currentRun || busy.value) return
  busy.value = action
  try {
    const result = action === 'cancel'
      ? await api.cancelAssistantRun(currentRun.id)
      : await api.confirmAssistantRun(currentRun.id, currentRun.pending_confirmation.id, approved)
    if (selectedId.value === id && props.open) {
      session.value.run = result
      await loadSession(id, { polling: true })
    }
  } catch (error) { toast.error(`操作失败：${error.message}`) }
  finally { busy.value = '' }
}
async function saveSettings() {
  if (busy.value) return
  overviewSequence += 1
  overviewController?.abort()
  busy.value = 'settings'
  const submittedRevision = formRevisions.settings
  const submitted = settingsValue(settingsDraft.value)
  submitted.issue_repositories = submitted.issue_repositories.map(value => value.trim()).filter(Boolean)
  if (submitted.issue_repositories.length > 20 || submitted.issue_repositories.some(value => !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\/[A-Za-z0-9_.-]{1,100}$/.test(value) || ['.', '..'].includes(value.split('/')[1]))) {
    busy.value = ''
    toast.error('仓库请按 owner/repo 填写，最多 20 个')
    return
  }
  try {
    const value = await api.saveAssistantSettings(submitted)
    catalog.value.settings = value
    if (formRevisions.settings === submittedRevision) settingsDraft.value = settingsValue(value)
    catalog.value.issue_status = { ...catalog.value.issue_status, enabled: !!(value.enabled && value.allow_actions && value.allow_issue_submission), repositories: value.issue_repositories || [] }
    toast.success('助手设置已保存')
  } catch (error) { toast.error(`保存失败：${error.message}`) }
  finally { busy.value = '' }
}
async function saveResource(kind) {
  if (busy.value) return
  busy.value = kind
  const submittedRevision = formRevisions[kind]
  try {
    if (kind === 'memory') {
      await api.saveAssistantMemory(memoryDraft.value.trim(), memoryEditingId.value)
      if (formRevisions.memory === submittedRevision) {
        memoryDraft.value = ''
        memoryEditingId.value = ''
      }
    } else if (kind === 'skill') {
      const value = { ...skillDraft.value }
      await api.saveAssistantSkill({ ...(value.id ? { id: value.id } : {}), name: value.name, description: value.description, content: value.content })
      if (value.id) delete skillDetails.value[value.id]
      if (formRevisions.skill === submittedRevision) {
        skillDraft.value = { name: '', description: '', content: '' }
        skillEditorOpen.value = false
      }
    } else {
      const value = { ...taskDraft.value }
      const schedule = value.mode === 'cron'
        ? { cron: value.cron }
        : { run_at: value.run_at ? new Date(value.run_at).toISOString() : '' }
      await api.saveAssistantTask({ name: value.name, prompt: value.prompt, ...schedule })
      if (formRevisions.task === submittedRevision) taskDraft.value = { name: '', prompt: '', mode: 'once', run_at: '', cron: '0 9 * * *' }
    }
    await loadOverview()
    toast.success('已保存')
  } catch (error) { toast.error(`保存失败：${error.message}`) }
  finally { busy.value = '' }
}
async function deleteResource(kind, item) {
  if (busy.value || (kind === 'skill' && item.builtin)) return
  if (!await confirm({ title: `删除${{ memory: '记忆', skill: '技能', task: '预约' }[kind]}`, message: '确定删除这条记录？', danger: true, confirmText: '删除' })) return
  busy.value = `${kind}-${item.id}`
  try {
    await ({ memory: api.deleteAssistantMemory, skill: api.deleteAssistantSkill, task: api.deleteAssistantTask })[kind](item.id)
    if (kind === 'skill') delete skillDetails.value[item.id]
    await loadOverview()
  } catch (error) { toast.error(`删除失败：${error.message}`) }
  finally { busy.value = '' }
}
async function readSkill(item) {
  if (skillDetails.value[item.id]) return skillDetails.value[item.id]
  if (skillLoading.value[item.id]) return null
  const controller = new AbortController()
  skillControllers.set(item.id, controller)
  skillLoading.value[item.id] = true
  delete skillLoadErrors.value[item.id]
  try {
    const value = await api.getAssistantSkill(item.id, controller.signal)
    if (disposed || !props.open) return null
    skillDetails.value[item.id] = value
    return value
  } catch (error) {
    if (error.name !== 'AbortError') skillLoadErrors.value[item.id] = `读取失败：${error.message}`
    return null
  } finally {
    if (skillControllers.get(item.id) === controller) {
      skillLoading.value[item.id] = false
      skillControllers.delete(item.id)
    }
  }
}
async function editSkill(item) {
  if (busy.value || item.builtin) return
  const revision = formRevisions.skill
  const value = await readSkill(item)
  if (!value || !props.open) return
  if (revision !== formRevisions.skill) { toast.error('已保留正在编辑的内容，请再次点击编辑'); return }
  skillDraft.value = { id: item.id, name: value.name, description: value.description, content: value.content }
  skillEditorOpen.value = true
  await nextTick()
  dialog.value?.querySelector('#assistant-skill-name')?.focus()
}
async function toggleTask(item) {
  if (busy.value) return
  busy.value = `task-${item.id}`
  try {
    await api.toggleAssistantTask(item.id, !item.enabled)
    await loadOverview()
  } catch (error) { toast.error(`修改失败：${error.message}`) }
  finally { busy.value = '' }
}
function keydown(event) {
  if (!props.open || confirmState.open) return
  if (event.key === 'Escape') { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab' || !dialog.value) return
  const items = [...dialog.value.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')]
    .filter(el => el.getClientRects().length)
  const first = items[0], last = items.at(-1)
  if (!first) { event.preventDefault(); dialog.value.focus() }
  else if (event.shiftKey && (document.activeElement === first || !dialog.value.contains(document.activeElement))) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.value || !dialog.value.contains(document.activeElement))) { event.preventDefault(); first.focus() }
}
function visibilityChanged() {
  if (document.hidden) { stopPolling(); sessionLoading.value = false }
  else if (props.open && selectedId.value) loadSession(selectedId.value, { polling: true })
}
watch(() => props.open, async (value) => {
  openSequence += 1
  sessionSequence += 1
  stopPolling()
  overviewController?.abort()
  sessionLoading.value = false
  if (!value) {
    for (const controller of skillControllers.values()) controller.abort()
    document.removeEventListener('keydown', keydown)
    document.removeEventListener('visibilitychange', visibilityChanged)
    previousFocus?.focus?.()
    return
  }
  previousFocus = document.activeElement
  document.addEventListener('keydown', keydown)
  document.addEventListener('visibilitychange', visibilityChanged)
  await nextTick()
  dialog.value?.focus()
  const opening = openSequence
  api.listPlugins().then(value => {
    if (opening !== openSequence || !props.open) return
    pluginOptions.value = Array.isArray(value) ? value : (value.plugins || [])
  }).catch(() => {})
  await loadOverview(true)
}, { immediate: true })
watch(activeTab, () => {
  if (props.open && !loading.value && activeTab.value !== 'chat') loadOverview()
})
onBeforeUnmount(() => {
  disposed = true
  openSequence += 1
  stopPolling()
  overviewController?.abort()
  for (const controller of skillControllers.values()) controller.abort()
  document.removeEventListener('keydown', keydown)
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="assistant">
      <div v-if="open" class="assistant-mask" @click.self="emit('close')">
        <section ref="dialog" class="assistant-panel" :class="{ fullscreen }" role="dialog" aria-modal="true" aria-labelledby="assistant-title" tabindex="-1">
          <p class="assistant-sr-only" role="status" aria-live="polite" aria-atomic="true"><span :key="announcementSequence">{{ liveAnnouncement }}</span></p>
          <header class="assistant-header">
            <div class="assistant-heading"><Bot aria-hidden="true" /><div><h2 id="assistant-title">系统助手</h2><span>AWBotNest AI</span></div></div>
            <div class="assistant-header-actions">
              <button type="button" class="assistant-icon fullscreen-toggle" :aria-label="fullscreen ? '退出全屏' : '全屏显示'" :title="fullscreen ? '退出全屏' : '全屏显示'" @click="fullscreen = !fullscreen"><Minimize2 v-if="fullscreen" /><Expand v-else /></button>
              <button type="button" class="assistant-icon" aria-label="关闭助手" title="关闭助手，任务继续执行" @click="emit('close')"><X /></button>
            </div>
          </header>
          <nav class="assistant-tabs" aria-label="助手功能">
            <button v-for="tab in tabs" :key="tab.id" type="button" :class="{ selected: activeTab === tab.id }" :aria-current="activeTab === tab.id ? 'page' : undefined" @click="activeTab = tab.id">{{ tab.label }}</button>
          </nav>
          <div v-if="loading" class="assistant-loading" role="status">正在读取助手…</div>
          <div v-else-if="loadError" class="assistant-loading" role="alert"><p>{{ loadError }}</p><button class="btn" @click="loadOverview(true)">重试</button></div>
          <template v-else>
            <div v-if="activeTab === 'chat'" class="assistant-chat">
              <div class="assistant-session-bar">
                <div class="assistant-session-select"><select :value="selectedId" aria-label="选择助手会话" :disabled="!!busy" @change="selectSession($event.target.value)"><option v-if="!catalog.sessions.length" value="">新会话</option><option v-for="item in catalog.sessions" :key="item.id" :value="item.id">{{ item.title || '新会话' }}</option></select><ChevronDown aria-hidden="true" /></div>
                <button type="button" class="assistant-icon" aria-label="新建会话" title="新建会话" :disabled="!!busy" @click="newSession"><Plus /></button>
                <button type="button" class="assistant-icon" aria-label="删除当前会话" :title="activeRun ? '停止任务后可删除会话' : '删除会话'" :disabled="!selectedId || !!busy || activeRun" @click="deleteSession"><Trash2 /></button>
              </div>
              <div ref="transcript" class="assistant-transcript" aria-label="对话记录" :aria-busy="sessionLoading">
                <div v-if="sessionLoading" class="assistant-empty" role="status">正在读取会话…</div>
                <template v-else>
                  <p v-if="session?.history_trimmed > 0" class="assistant-history-note">较早的消息已整理。</p>
                  <div v-if="!session?.messages?.length" class="assistant-empty assistant-welcome">
                    <Bot aria-hidden="true" /><h3>有什么需要帮忙？</h3><p>可以查看运行状态、分析日志，或安排稍后执行的任务。</p>
                    <div class="assistant-suggestions"><button type="button" @click="chatDraft = '检查系统运行状态，有异常的话告诉我'">检查运行状态</button><button type="button" @click="chatDraft = '看看最近的错误日志，分析可能的原因'">分析错误日志</button></div>
                  </div>
                  <article v-for="message in session?.messages || []" :key="message.id" class="assistant-message" :class="message.role === 'user' ? 'from-user' : 'from-assistant'">
                    <div class="assistant-message-meta"><strong>{{ message.role === 'user' ? '管理员' : '系统助手' }}</strong><time>{{ formatTime(message.created_at) }}</time></div>
                    <div v-if="message.role === 'user'" class="assistant-user-content">{{ message.content }}</div>
                    <div v-else class="assistant-markdown" v-html="markdown(message.content)"></div>
                  </article>
                  <section v-if="run" class="assistant-run" aria-label="执行进度">
                    <div class="assistant-run-head"><span class="assistant-run-status" :class="run.status"><i v-if="activeRun"></i><Check v-else-if="run.status === 'completed'" aria-hidden="true" />{{ runLabel }}</span><span v-if="run.usage?.total_tokens" class="assistant-usage" :title="`输入 ${tokens(run.usage.input_tokens)} · 输出 ${tokens(run.usage.output_tokens)}`">{{ run.usage.estimated ? '约 ' : '' }}{{ tokens(run.usage.total_tokens) }} tokens</span></div>
                    <details v-if="run.steps?.length" class="assistant-steps" :open="activeRun"><summary>执行步骤 <span>{{ run.steps.length }}</span></summary><ol><li v-for="step in run.steps" :key="step.id"><div><strong>{{ step.title || '处理步骤' }}</strong><span>{{ statusLabels[step.status] || '' }}</span></div><pre v-if="step.content">{{ stepContent(step.content) }}</pre></li></ol></details>
                    <div v-if="pending" ref="confirmationCard" class="assistant-confirmation" role="group" aria-label="操作确认">
                      <h3>需要管理员确认</h3><p>{{ pending.summary }}</p>
                      <template v-if="pendingIssue">
                        <p>将提交到 GitHub，内容可能公开。请确认没有敏感信息。</p>
                        <dl class="assistant-issue-preview"><dt>仓库</dt><dd>{{ pendingIssue.repository }}</dd><dt>标题</dt><dd>{{ pendingIssue.title }}</dd><dt>正文</dt><dd class="assistant-issue-body" role="region" aria-label="Issue 正文" tabindex="0">{{ pendingIssue.body }}</dd></dl>
                        <details class="assistant-confirmation-arguments"><summary>原始参数</summary><pre>{{ stepContent(pending.arguments) }}</pre></details>
                      </template>
                      <details v-else-if="pending.arguments" open class="assistant-confirmation-arguments"><summary>操作内容</summary><pre>{{ stepContent(pending.arguments) }}</pre></details>
                      <small v-if="pendingExpired">确认已过期，请重新发起任务。</small><div v-else class="assistant-inline-actions"><button type="button" class="btn" :disabled="!!busy" @click="actOnRun('confirm', false)">拒绝</button><button type="button" class="btn btn-primary" :disabled="!!busy" @click="actOnRun('confirm', true)">{{ busy === 'confirm' ? '正在提交…' : '确认执行' }}</button></div>
                    </div>
                    <p v-if="run.error" class="assistant-error" role="alert">{{ run.error }}</p>
                  </section>
                  <div v-if="pollError" class="assistant-poll-error" role="status"><span>{{ pollError }}</span><button type="button" @click="loadSession(selectedId, { polling: true })">重试</button></div>
                </template>
              </div>
              <form class="assistant-composer" @submit.prevent="sendMessage">
                <div v-if="!catalog.settings.enabled" class="assistant-disabled">助手已关闭。<button type="button" @click="activeTab = 'settings'">前往设置</button></div>
                <label for="assistant-message" class="assistant-composer-label">{{ activeRun ? '补充要求' : '发送消息' }}<span v-if="activeRun">会在下一步处理</span></label>
                <textarea id="assistant-message" ref="composer" v-model="chatDraft" rows="3" maxlength="8000" :disabled="!catalog.settings.enabled" :placeholder="activeRun ? '继续补充要求，不会中断当前步骤' : '输入问题或要执行的任务…'" @keydown="composerKeydown"></textarea>
                <div class="assistant-composer-footer"><span>{{ catalog.settings.allow_actions ? '修改操作需确认' : '只读模式' }}</span><div><button v-if="activeRun" type="button" class="btn" :disabled="!!busy" @click="actOnRun('cancel')"><Square aria-hidden="true" />停止</button><button type="submit" class="btn btn-primary" :disabled="!canSend"><Send aria-hidden="true" />{{ busy === 'send' ? '发送中…' : (activeRun ? '补充' : '发送') }}</button></div></div>
              </form>
            </div>

            <div v-else class="assistant-management">
              <template v-if="activeTab === 'settings'">
                <form class="assistant-form" @submit.prevent="saveSettings">
                  <div class="assistant-section-heading"><Settings2 aria-hidden="true" /><h3>助手设置</h3></div>
                  <label class="assistant-switch-row"><span>启用助手</span><input v-model="settingsDraft.enabled" type="checkbox" /></label>
                  <label class="assistant-field">模型<select v-model="settingsDraft.model" class="select"><option value="">使用系统默认文字模型</option><option v-for="model in catalog.models" :key="model.id" :value="model.id">{{ model.name }}</option></select></label>
                  <div class="assistant-field-note">模型和服务商在系统设置的 AI 服务中配置。</div>
                  <label class="assistant-switch-row"><span>允许执行修改操作<small>每次修改前单独确认，不开放终端和任意文件读写。</small></span><input v-model="settingsDraft.allow_actions" type="checkbox" /></label>
                  <label class="assistant-switch-row"><span>使用已保存的记忆</span><input v-model="settingsDraft.memory_enabled" type="checkbox" /></label>
                  <label class="assistant-switch-row"><span>预约通知<small>通过默认 Bot 接收结果和待确认提醒。</small></span><input v-model="settingsDraft.notify_tasks" type="checkbox" /></label>
                  <div class="assistant-issue-settings">
                    <label class="assistant-switch-row"><span>允许提交 GitHub Issue<small>{{ settingsDraft.allow_actions ? '提交前确认仓库、标题和正文。' : '提交前需先开启“允许执行修改操作”。' }}</small></span><input v-model="settingsDraft.allow_issue_submission" type="checkbox" /></label>
                    <p class="assistant-field-note">{{ catalog.issue_status?.configured ? '使用系统设置中已保存的 GitHub Token。' : '未配置 GitHub Token，只生成问题报告。可在系统设置中配置。' }}</p>
                    <label class="assistant-field">允许提交的仓库<textarea v-model="issueRepositoriesText" class="textarea assistant-repositories" rows="2" maxlength="3000" placeholder="AWdress/AWBotNest" spellcheck="false"></textarea><small>每行一个 owner/repo，最多 20 个。</small></label>
                  </div>
                  <fieldset class="assistant-plugin-list"><legend>允许访问的插件</legend><p v-if="!pluginOptions.length">暂无可选插件</p><label v-for="plugin in pluginOptions" :key="plugin.id"><input v-model="settingsDraft.allowed_plugins" type="checkbox" :value="plugin.id" /><span>{{ plugin.name || plugin.id }}</span></label></fieldset>
                  <details class="assistant-advanced"><summary>执行上限</summary><div class="assistant-form-grid"><label class="assistant-field">最多执行轮数<input v-model.number="settingsDraft.max_rounds" type="number" min="1" max="12" class="input" /></label><label class="assistant-field">最多工具调用<input v-model.number="settingsDraft.max_tool_calls" type="number" min="1" max="32" class="input" /></label><label class="assistant-field">Token 预算<input v-model.number="settingsDraft.max_tokens" type="number" min="2000" max="64000" step="1000" class="input" /></label><label class="assistant-field">单次调用超时（秒）<input v-model.number="settingsDraft.timeout_seconds" type="number" min="15" max="300" class="input" /></label></div></details>
                  <button type="submit" class="btn btn-primary" :disabled="!!busy">{{ busy === 'settings' ? '保存中…' : '保存设置' }}</button>
                </form>
              </template>
              <template v-else-if="activeTab === 'memories'">
                <div class="assistant-section-heading"><h3>记忆</h3><span>{{ catalog.memories.length }} 条</span></div><p class="assistant-section-description">保存常用信息，下次对话可继续使用。不要填写密码或密钥。</p>
                <form class="assistant-form" @submit.prevent="saveResource('memory')"><label class="assistant-field">{{ memoryEditingId ? '编辑记忆' : '添加记忆' }}<textarea v-model="memoryDraft" class="textarea" rows="3" maxlength="4000" required placeholder="例如：消息通知优先使用默认 Bot"></textarea></label><div class="assistant-inline-actions"><button v-if="memoryEditingId" type="button" class="btn" @click="memoryDraft = ''; memoryEditingId = ''">取消编辑</button><button type="submit" class="btn btn-primary" :disabled="!!busy || !memoryDraft.trim()">保存记忆</button></div></form>
                <div class="assistant-resource-list"><article v-for="item in catalog.memories" :key="item.id" class="assistant-resource"><div><p class="assistant-preserve-lines">{{ item.content }}</p><small>{{ formatTime(item.updated_at || item.created_at) }}</small><div><button type="button" class="assistant-text-button" :disabled="!!busy" @click="memoryEditingId = item.id; memoryDraft = item.content">编辑</button></div></div><button type="button" class="assistant-icon" aria-label="删除这条记忆" :disabled="!!busy" @click="deleteResource('memory', item)"><Trash2 /></button></article><p v-if="!catalog.memories.length" class="assistant-list-empty">还没有保存记忆</p></div>
              </template>
              <template v-else-if="activeTab === 'skills'">
                <div class="assistant-section-heading"><h3>技能</h3><span>内置 {{ builtinSkillCount }} · 自定义 {{ customSkillCount }}/50</span></div><p class="assistant-section-description">内置技能可直接使用。也可以保存常用步骤，不改变助手的操作权限。</p>
                <button v-if="!skillEditorOpen" type="button" class="btn" :disabled="!skillDraft.id && customSkillCount >= 50" @click="skillEditorOpen = true"><Plus aria-hidden="true" />{{ skillDraft.name || skillDraft.content ? '继续编辑技能' : '添加自定义技能' }}</button>
                <form v-if="skillEditorOpen" class="assistant-form" @submit.prevent="saveResource('skill')"><label class="assistant-field">名称<input id="assistant-skill-name" v-model="skillDraft.name" class="input" maxlength="80" required /></label><label class="assistant-field">用途<input v-model="skillDraft.description" class="input" maxlength="300" required /></label><label class="assistant-field">处理步骤<textarea v-model="skillDraft.content" class="textarea" rows="6" maxlength="12000" required></textarea></label><div class="assistant-inline-actions"><button type="button" class="btn" @click="skillDraft = { name: '', description: '', content: '' }; skillEditorOpen = false">取消编辑</button><button type="submit" class="btn btn-primary" :disabled="!!busy">{{ skillDraft.id ? '保存修改' : '添加技能' }}</button></div></form>
                <div class="assistant-resource-list"><article v-for="item in catalog.skills" :key="item.id" class="assistant-resource" :data-skill-id="item.id"><div><h4>{{ item.name }}<span class="assistant-skill-kind">{{ item.builtin ? '内置' : '自定义' }}</span></h4><p>{{ item.description }}</p><details @toggle="($event.target.open) && readSkill(item)"><summary>查看步骤</summary><p v-if="skillLoading[item.id]" role="status">正在读取…</p><div v-else-if="skillLoadErrors[item.id]" class="assistant-poll-error" role="status"><span>{{ skillLoadErrors[item.id] }}</span><button type="button" @click="readSkill(item)">重试</button></div><p v-else-if="skillDetails[item.id]" class="assistant-preserve-lines">{{ skillDetails[item.id].content }}</p></details><button v-if="!item.builtin" type="button" class="assistant-text-button" :disabled="!!busy || skillLoading[item.id]" @click="editSkill(item)">编辑</button></div><button v-if="!item.builtin" type="button" class="assistant-icon" :aria-label="`删除技能 ${item.name}`" :disabled="!!busy" @click="deleteResource('skill', item)"><Trash2 /></button></article><p v-if="!catalog.skills.length" class="assistant-list-empty">还没有可用技能</p></div>
              </template>
              <template v-else-if="activeTab === 'tasks'">
                <div class="assistant-section-heading"><Clock3 aria-hidden="true" /><h3>预约任务</h3><span>{{ catalog.tasks.length }} 个</span></div><p class="assistant-section-description">到时由助手执行。需要确认的修改会等待管理员批准。</p>
                <form class="assistant-form" @submit.prevent="saveResource('task')"><label class="assistant-field">任务名称<input v-model="taskDraft.name" class="input" maxlength="80" required /></label><label class="assistant-field">任务要求<textarea v-model="taskDraft.prompt" class="textarea" rows="3" maxlength="2000" required></textarea></label><label class="assistant-field">执行方式<select v-model="taskDraft.mode" class="select"><option value="once">指定时间</option><option value="cron">重复执行（北京时间）</option></select></label><label v-if="taskDraft.mode === 'once'" class="assistant-field">执行时间（本地时间）<input v-model="taskDraft.run_at" type="datetime-local" class="input" required /></label><div v-else class="assistant-field"><span>Cron 规则（五段，至少间隔 5 分钟）</span><CronInput v-model="taskDraft.cron" /></div><button type="submit" class="btn btn-primary" :disabled="!!busy">添加预约</button></form>
                <div class="assistant-resource-list"><article v-for="item in catalog.tasks" :key="item.id" class="assistant-resource"><div><h4>{{ item.name }}<span class="assistant-task-state">{{ item.enabled ? '已启用' : '已停用' }}</span></h4><p>{{ item.prompt }}</p><small>{{ item.cron ? `${item.cron} · 北京时间` : formatTime(item.run_at) }}</small><p v-if="item.last_status">{{ statusLabels[item.last_status] || item.last_status }}</p><div class="assistant-inline-actions"><button type="button" class="assistant-text-button" :disabled="!!busy" @click="toggleTask(item)">{{ item.enabled ? '停用' : '启用' }}</button><button v-if="item.last_session_id" type="button" class="assistant-text-button" @click="activeTab = 'chat'; selectSession(item.last_session_id)">查看结果</button></div></div><button type="button" class="assistant-icon" :aria-label="`删除预约 ${item.name}`" :disabled="!!busy" @click="deleteResource('task', item)"><Trash2 /></button></article><p v-if="!catalog.tasks.length" class="assistant-list-empty">还没有预约任务</p></div>
              </template>
            </div>
          </template>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.assistant-mask { position: fixed; inset: 0; z-index: 360; display: flex; justify-content: flex-end; background: rgba(2, 5, 10, .55); }
.assistant-panel { width: min(720px, 100vw); height: 100%; min-width: 0; display: flex; flex-direction: column; overflow: hidden; background: var(--bg-card); color: var(--text-primary); box-shadow: -16px 0 48px rgba(0, 0, 0, .3); outline: none; }
.assistant-panel.fullscreen { width: 100%; }
.assistant-sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); clip-path: inset(50%); white-space: nowrap; border: 0; }
.assistant-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: max(18px, env(safe-area-inset-top)) 22px 16px; flex-shrink: 0; }
.assistant-heading { display: flex; align-items: center; gap: 12px; }
.assistant-heading > svg { width: 28px; height: 28px; color: var(--accent); }
.assistant-heading h2 { font-size: 18px; font-weight: 650; }
.assistant-heading span { color: var(--text-secondary); font-size: 11px; }
.assistant-header-actions, .assistant-inline-actions { display: flex; align-items: center; gap: 8px; }
.assistant-icon { width: 38px; height: 38px; padding: 9px; display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: var(--radius-sm); color: var(--text-secondary); background: transparent; cursor: pointer; flex-shrink: 0; }
.assistant-icon:hover { color: var(--text-primary); background: var(--bg-hover); }
.assistant-icon svg { width: 19px; height: 19px; }
.assistant-panel button:disabled { opacity: .45; cursor: not-allowed; }
.assistant-tabs { display: flex; flex-shrink: 0; gap: 4px; padding: 0 22px; border-bottom: 1px solid var(--border); }
.assistant-tabs button { flex: 1; min-width: 0; padding: 12px 5px; border: 0; border-bottom: 2px solid transparent; color: var(--text-secondary); font: inherit; font-size: 13px; background: none; cursor: pointer; }
.assistant-tabs button:hover { color: var(--text-primary); }
.assistant-tabs .selected { color: var(--accent-hover); border-bottom-color: var(--accent); }
.assistant-loading { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; padding: 24px; color: var(--text-secondary); text-align: center; }
.assistant-chat { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.assistant-session-bar { display: flex; align-items: center; gap: 4px; padding: 10px 22px; border-bottom: 1px solid var(--border); }
.assistant-session-select { position: relative; min-width: 0; flex: 1; }
.assistant-session-select select { width: 100%; min-width: 0; appearance: none; padding: 9px 28px 9px 2px; font: inherit; font-size: 13px; text-overflow: ellipsis; border: 0; border-radius: 5px; background: none; cursor: pointer; }
.assistant-session-select svg { position: absolute; pointer-events: none; top: 50%; right: 6px; width: 14px; height: 14px; transform: translateY(-50%); color: var(--text-secondary); }
.assistant-transcript { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 24px; scrollbar-color: var(--border-light) transparent; scrollbar-width: thin; }
.assistant-empty { padding: 30px 12px; color: var(--text-secondary); text-align: center; }
.assistant-welcome { margin: 8vh auto 0; max-width: 450px; }
.assistant-welcome > svg { width: 42px; height: 42px; color: var(--accent); margin-bottom: 16px; }
.assistant-welcome h3 { color: var(--text-primary); font-size: 21px; margin-bottom: 10px; }
.assistant-welcome p { font-size: 13px; line-height: 1.8; }
.assistant-suggestions { display: flex; justify-content: center; flex-wrap: wrap; gap: 8px; margin-top: 24px; }
.assistant-suggestions button { padding: 9px 12px; border: 1px solid var(--border-light); border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); font: inherit; font-size: 12px; cursor: pointer; }
.assistant-suggestions button:hover { border-color: var(--accent); color: var(--text-primary); }
.assistant-message { min-width: 0; margin-bottom: 26px; overflow-wrap: anywhere; }
.assistant-message-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 9px; font-size: 11px; color: var(--text-secondary); }
.assistant-message-meta strong { color: var(--text-primary); font-weight: 600; }
.assistant-message-meta time { font-variant-numeric: tabular-nums; }
.assistant-message.from-user { margin-left: 36px; padding: 14px 16px; background: var(--bg-elevated); border-radius: 12px; }
.assistant-user-content, .assistant-preserve-lines { white-space: pre-wrap; line-height: 1.75; font-size: 14px; }
.assistant-markdown { font-size: 14px; line-height: 1.8; overflow-wrap: anywhere; }
.assistant-markdown :deep(p + p), .assistant-markdown :deep(ul), .assistant-markdown :deep(ol), .assistant-markdown :deep(pre), .assistant-markdown :deep(blockquote) { margin-top: 12px; }
.assistant-markdown :deep(h1), .assistant-markdown :deep(h2), .assistant-markdown :deep(h3) { font-size: 16px; margin: 22px 0 8px; }
.assistant-markdown :deep(ul), .assistant-markdown :deep(ol) { padding-left: 22px; }
.assistant-markdown :deep(a) { color: var(--accent-hover); text-decoration: underline; text-underline-offset: 3px; }
.assistant-markdown :deep(code) { padding: 2px 5px; border-radius: 4px; background: var(--bg-base); font-size: .9em; }
.assistant-markdown :deep(pre) { max-width: 100%; padding: 12px; overflow: auto; background: var(--bg-base); border-radius: var(--radius-sm); }
.assistant-markdown :deep(pre code) { padding: 0; }
.assistant-markdown :deep(blockquote) { padding-left: 14px; border-left: 1px solid var(--border-light); color: var(--text-secondary); }
.assistant-markdown :deep(table) { display: block; overflow-x: auto; border-collapse: collapse; max-width: 100%; }
.assistant-markdown :deep(td), .assistant-markdown :deep(th) { padding: 8px; border: 1px solid var(--border); }
.assistant-markdown :deep(img) { max-width: 100%; height: auto; }
.assistant-run { padding-top: 16px; border-top: 1px solid var(--border); margin-bottom: 12px; }
.assistant-run-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; font-size: 12px; }
.assistant-run-status { display: inline-flex; align-items: center; gap: 7px; color: var(--text-secondary); }
.assistant-run-status.completed { color: var(--success); }
.assistant-run-status.failed, .assistant-error { color: var(--danger); }
.assistant-run-status.waiting_confirmation { color: var(--warning); }
.assistant-run-status svg { width: 15px; height: 15px; }
.assistant-run-status i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.assistant-usage { font-size: 11px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.assistant-steps summary { color: var(--text-secondary); font-size: 12px; cursor: pointer; }
.assistant-steps summary span { margin-left: 4px; }
.assistant-steps ol { list-style: none; padding-top: 10px; }
.assistant-steps li { margin-bottom: 10px; padding: 10px 0; }
.assistant-steps li > div { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; font-size: 12px; }
.assistant-steps li > div span { color: var(--text-secondary); white-space: nowrap; font-size: 11px; }
.assistant-steps pre { max-height: 220px; overflow: auto; margin-top: 8px; padding: 10px 12px; border-radius: var(--radius-sm); background: var(--bg-base); color: var(--text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; line-height: 1.6; }
.assistant-confirmation { margin-top: 16px; padding: 16px; border: 1px solid var(--warning); border-radius: 12px; }
.assistant-confirmation h3 { color: var(--text-primary); font-size: 14px; }
.assistant-confirmation p { margin: 10px 0 16px; font-size: 13px; white-space: pre-wrap; line-height: 1.7; overflow-wrap: anywhere; }
.assistant-confirmation small { color: var(--warning); }
.assistant-confirmation-arguments { margin-bottom: 16px; }
.assistant-confirmation-arguments summary { font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.assistant-confirmation-arguments pre { max-height: 260px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; margin-top: 10px; border-radius: var(--radius-sm); color: var(--text-primary); background: var(--bg-base); font-size: 12px; line-height: 1.7; }
.assistant-issue-preview { margin-bottom: 16px; font-size: 13px; line-height: 1.7; }
.assistant-issue-preview dt { color: var(--text-secondary); margin: 12px 0 4px; font-size: 12px; }
.assistant-issue-preview dd { margin: 0; overflow-wrap: anywhere; }
.assistant-issue-body { max-height: 200px; overflow: auto; white-space: pre-wrap; padding: 12px; background: var(--bg-base); border-radius: var(--radius-sm); }
.assistant-history-note { margin-bottom: 20px; color: var(--text-secondary); font-size: 11px; text-align: center; }
.assistant-error { font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; margin-top: 12px; }
.assistant-poll-error { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; color: var(--warning); padding-top: 12px; }
.assistant-poll-error button, .assistant-disabled button { color: var(--accent-hover); background: none; border: none; font: inherit; text-decoration: underline; cursor: pointer; }
.assistant-composer { flex-shrink: 0; padding: 16px 22px max(18px, env(safe-area-inset-bottom)); border-top: 1px solid var(--border); background: var(--bg-card); }
.assistant-composer-label { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
.assistant-composer-label span { font-size: 11px; }
.assistant-composer textarea { width: 100%; display: block; min-height: 72px; max-height: 180px; resize: vertical; padding: 12px; border: 1px solid var(--border-light); border-radius: 10px; color: var(--text-primary); background: var(--bg-elevated); font: inherit; font-size: 14px; line-height: 1.6; caret-color: var(--accent-hover); }
.assistant-composer textarea::placeholder { color: var(--text-secondary); }
.assistant-composer-footer { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; }
.assistant-composer-footer > span { color: var(--text-secondary); font-size: 11px; }
.assistant-composer-footer > div { display: flex; gap: 8px; }
.assistant-composer-footer svg { width: 14px; height: 14px; }
.assistant-disabled { color: var(--warning); font-size: 12px; margin-bottom: 10px; }
.assistant-management { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 26px 24px; scrollbar-color: var(--border-light) transparent; scrollbar-width: thin; }
.assistant-section-heading { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.assistant-section-heading h3 { font-size: 16px; }
.assistant-section-heading > svg { color: var(--accent); width: 18px; height: 18px; }
.assistant-section-heading > span { font-size: 12px; color: var(--text-secondary); margin-left: auto; }
.assistant-section-description, .assistant-field-note { color: var(--text-secondary); font-size: 12px; line-height: 1.7; margin-bottom: 22px; }
.assistant-field-note { margin-top: -8px; margin-bottom: 4px; }
.assistant-issue-settings { display: flex; flex-direction: column; gap: 16px; padding-top: 12px; border-top: 1px solid var(--border); }
.assistant-repositories { width: 100%; overflow-wrap: anywhere; white-space: pre-wrap; }
.assistant-field small { font-size: 11px; line-height: 1.7; color: var(--text-secondary); }
.assistant-form { display: flex; flex-direction: column; align-items: stretch; gap: 16px; }
.assistant-form > .btn, .assistant-form > .assistant-inline-actions { align-self: flex-start; }
.assistant-field { display: flex; flex-direction: column; gap: 8px; font-size: 12px; color: var(--text-secondary); min-width: 0; }
.assistant-field input, .assistant-field textarea, .assistant-field select { min-width: 0; font-size: 14px; }
.assistant-switch-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 48px; color: var(--text-primary); font-size: 13px; }
.assistant-switch-row small { display: block; font-size: 12px; color: var(--text-secondary); margin-top: 7px; line-height: 1.7; }
.assistant-panel input[type="checkbox"] { width: 18px; height: 18px; flex: 0 0 18px; accent-color: var(--accent); cursor: pointer; }
.assistant-plugin-list { border: 0; padding: 12px 0 0; display: flex; flex-direction: column; gap: 12px; max-height: 240px; overflow: auto; }
.assistant-plugin-list legend { color: var(--text-primary); font-size: 13px; }
.assistant-plugin-list label { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--text-secondary); }
.assistant-plugin-list label span { min-width: 0; overflow-wrap: anywhere; }
.assistant-plugin-list p { color: var(--text-secondary); font-size: 12px; }
.assistant-advanced { padding-top: 12px; border-top: 1px solid var(--border); }
.assistant-advanced summary { cursor: pointer; font-size: 12px; color: var(--text-secondary); }
.assistant-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 16px; }
.assistant-resource-list { margin-top: 28px; border-top: 1px solid var(--border); }
.assistant-resource { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 20px 0; border-bottom: 1px solid var(--border); }
.assistant-resource > div { min-width: 0; flex: 1; overflow-wrap: anywhere; }
.assistant-resource h4 { color: var(--text-primary); font-size: 14px; line-height: 1.6; }
.assistant-skill-kind { display: inline-block; margin-left: 9px; font-size: 11px; color: var(--text-secondary); font-weight: 400; }
.assistant-resource p { color: var(--text-secondary); font-size: 13px; line-height: 1.75; margin: 6px 0; }
.assistant-resource small, .assistant-resource summary { color: var(--text-secondary); font-size: 11px; line-height: 1.7; }
.assistant-resource details { margin: 12px 0; }
.assistant-resource summary { cursor: pointer; }
.assistant-task-state { display: inline-block; margin-left: 8px; font-size: 11px; font-weight: 400; color: var(--text-secondary); }
.assistant-text-button { border: none; background: none; padding: 7px 0; font: inherit; font-size: 12px; color: var(--accent-hover); cursor: pointer; }
.assistant-list-empty { color: var(--text-secondary); font-size: 13px; text-align: center; padding: 36px 12px; }
.assistant-panel ::selection { color: var(--text-primary); background: var(--accent-dim); }
.assistant-enter-active, .assistant-leave-active { transition: opacity .2s ease; }
.assistant-enter-active .assistant-panel, .assistant-leave-active .assistant-panel { transition: transform .24s cubic-bezier(.16, 1, .3, 1); }
.assistant-enter-from, .assistant-leave-to { opacity: 0; }
.assistant-enter-from .assistant-panel, .assistant-leave-to .assistant-panel { transform: translateX(36px); }
@media (max-width: 768px) {
  .assistant-issue-body { max-height: 120px; }
  .assistant-mask { top: var(--visual-viewport-offset-top, 0px); bottom: auto; height: var(--visual-viewport-height, 100dvh); }
  .assistant-panel { width: 100%; height: 100%; box-shadow: none; }
  .assistant-panel input, .assistant-panel textarea, .assistant-panel select { font-size: 16px; }
  .assistant-header { padding: max(12px, env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) 10px max(16px, env(safe-area-inset-left)); }
  .assistant-heading { gap: 10px; }
  .assistant-heading h2 { font-size: 17px; }
  .assistant-icon { width: 44px; height: 44px; }
  .fullscreen-toggle { display: none; }
  .assistant-tabs { padding: 0 max(12px, env(safe-area-inset-right)) 0 max(12px, env(safe-area-inset-left)); }
  .assistant-tabs button { min-height: 44px; }
  .assistant-session-bar { padding: 4px max(8px, env(safe-area-inset-right)) 4px max(14px, env(safe-area-inset-left)); }
  .assistant-session-select select { min-height: 44px; }
  .assistant-transcript { padding: 20px max(16px, env(safe-area-inset-right)) 16px max(16px, env(safe-area-inset-left)); }
  .assistant-message.from-user { margin-left: 18px; }
  .assistant-welcome { margin-top: 3vh; padding: 18px 0; }
  .assistant-welcome h3 { font-size: 19px; }
  .assistant-suggestions { margin-top: 18px; }
  .assistant-suggestions button { min-height: 44px; }
  .assistant-composer { padding: 12px max(14px, env(safe-area-inset-right)) max(12px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left)); }
  .assistant-composer textarea { min-height: 64px; max-height: 120px; font-size: 16px; resize: none; }
  .assistant-composer-footer .btn { min-height: 44px; }
  .assistant-management { padding: 22px max(16px, env(safe-area-inset-right)) max(22px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }
  .assistant-text-button { min-height: 44px; }
  .assistant-form-grid { gap: 12px; }
}
@media (max-width: 360px) { .assistant-composer-footer > span { max-width: 80px; } .assistant-form-grid { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { .assistant-enter-active, .assistant-leave-active, .assistant-enter-active .assistant-panel, .assistant-leave-active .assistant-panel { transition: none; } }
</style>
