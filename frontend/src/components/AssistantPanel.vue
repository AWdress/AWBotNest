<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Bot, Check, ChevronDown, Clock3, Expand, FileText, ImagePlus, Mic, Minimize2, Paperclip, Plus, RefreshCw, Send, Settings2, Square, Trash2, X } from '@lucide/vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { api, getAuthRevision, getToken, subscribeAuthChanges } from '../api'
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
  enabled: true, model: '', allow_actions: false,
  timeout_seconds: 120, memory_enabled: true, notify_tasks: false,
  allow_issue_submission: false,
  mcp_enabled: false,
  web_enabled: false,
}
const catalog = ref({ sessions: [], models: [], skills: [], memories: [], tasks: [], tools: [], settings: { ...defaults }, issue_status: { configured: false, enabled: false } })
const settingsDraft = ref({ ...defaults })
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
const imageDrafts = ref({})
const fileDrafts = ref({})
const audioDrafts = ref({})
const imageDraftGenerations = {}
const attachmentReaders = new Set()
const imageReadCleanups = new Set()
let attachmentSequence = 0
const imageProcessing = ref(false)
const imagePicker = ref(null)
const filePicker = ref(null)
const draftImages = computed(() => imageDrafts.value[selectedId.value || 'new'] || [])
const draftFiles = computed(() => fileDrafts.value[selectedId.value || 'new'] || [])
const draftAudio = computed(() => audioDrafts.value[selectedId.value || 'new'] || null)
const hasAttachments = computed(() => draftImages.value.length || draftFiles.value.length || !!draftAudio.value)
const channels = ref({ bindings: [], channels: [] })
const channelsLoading = ref(false)
const channelsError = ref('')
const pairDraft = ref({ kind: '', channel_id: '' })
const pairResult = ref(null)
const channelKinds = computed(() => [...new Set(channels.value.channels.map(item => item.kind))])
const availableChannels = computed(() => channels.value.channels.filter(item => item.kind === pairDraft.value.kind))
const mcpServers = ref([])
const mcpLoading = ref(false)
const mcpError = ref('')
const mcpEditorOpen = ref(false)
const emptyMcpDraft = () => ({ id: '', name: '', url: '', token: '', token_configured: false, clear_token: false, enabled: false, read_only_tools: '' })
const mcpDraft = ref(emptyMcpDraft())
const mcpDiscoveries = ref({})
const defaultSandboxUrl = 'http://assistant-sandbox:8765'
const sandboxConfig = ref({ enabled: false, url: defaultSandboxUrl, token_configured: false })
const sandboxDraft = ref({ enabled: false, url: defaultSandboxUrl, token: '', clear_token: false })
const sandboxLoaded = ref(false)
const sandboxLoading = ref(false)
const sandboxBusy = ref('')
const sandboxError = ref('')
const sandboxNotice = ref('')
const sandboxHealthy = ref(false)
const sandboxDirty = computed(() => sandboxDraft.value.enabled !== sandboxConfig.value.enabled
  || sandboxDraft.value.url !== sandboxConfig.value.url || !!sandboxDraft.value.token || sandboxDraft.value.clear_token)
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
const sandboxArtifacts = computed(() => {
  const result = [], seen = new Set()
  for (const item of Array.isArray(run.value?.artifacts) ? run.value.artifacts : []) {
    if (!item || typeof item.id !== 'string' || !/^[\w-]{1,128}$/.test(item.id)
        || item.mime_type !== 'image/jpeg' || seen.has(item.id)) continue
    seen.add(item.id)
    result.push({ id: item.id, title: typeof item.title === 'string' ? item.title.slice(0, 120) : '浏览器截图' })
    if (result.length === 16) break
  }
  return result
})
const artifactPreviews = ref(new Map())
const artifactControllers = new Map()
let artifactGeneration = 0
const activeRun = computed(() => ['queued', 'running', 'waiting_confirmation', 'waiting_input'].includes(run.value?.status))
const pending = computed(() => run.value?.pending_confirmation)
const pendingInput = computed(() => run.value?.pending_input)
const inputChoiceOnly = computed(() => pendingInput.value?.allow_custom === false)
const answerTooLong = computed(() => !!pendingInput.value && chatDraft.value.trim().length > 2000)
const pendingIssue = computed(() => {
  if (pending.value?.tool !== 'submit_github_issue') return null
  try {
    const value = typeof pending.value.arguments === 'string' ? JSON.parse(pending.value.arguments) : pending.value.arguments
    return value && ['repository', 'title', 'body'].every(key => typeof value[key] === 'string') ? value : null
  } catch { return null }
})
const pendingExpired = computed(() => pending.value?.expires_at && new Date(pending.value.expires_at).getTime() <= Date.now())
const inputExpired = computed(() => pendingInput.value?.expires_at && new Date(pendingInput.value.expires_at).getTime() <= Date.now())
const statusLabels = {
  queued: '排队中', running: '执行中', waiting_confirmation: '等待确认', waiting_input: '等待补充',
  completed: '已完成', failed: '失败', cancelled: '已停止', interrupted: '已中断',
  pending: '等待中', succeeded: '已完成', skipped: '已跳过',
}
const runLabel = computed(() => statusLabels[run.value?.status] || '')
const liveAnnouncement = ref('')
const announcementSequence = ref(0)
watch(() => JSON.stringify([
  props.open, loading.value, sessionLoading.value, selectedId.value, run.value?.id, run.value?.status, pending.value?.id, !!pendingExpired.value, pendingInput.value?.id,
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
const formRevisions = { settings: 0, memory: 0, skill: 0, task: 0, mcp: 0, sandbox: 0 }
watch(settingsDraft, () => { formRevisions.settings++ }, { deep: true, flush: 'sync' })
watch([memoryDraft, memoryEditingId], () => { formRevisions.memory++ }, { flush: 'sync' })
watch(skillDraft, () => { formRevisions.skill++ }, { deep: true, flush: 'sync' })
watch(taskDraft, () => { formRevisions.task++ }, { deep: true, flush: 'sync' })
watch(mcpDraft, () => { formRevisions.mcp++ }, { deep: true, flush: 'sync' })
watch(sandboxDraft, () => {
  formRevisions.sandbox++
  sandboxHealthy.value = false
  sandboxNotice.value = ''
  sandboxError.value = ''
}, { deep: true, flush: 'sync' })
const canSend = computed(() => (!!chatDraft.value.trim() || hasAttachments.value) && !inputChoiceOnly.value && !answerTooLong.value && !(activeRun.value && hasAttachments.value) && !(pendingInput.value && inputExpired.value) && !imageProcessing.value && !busy.value && !loading.value && !sessionLoading.value && catalog.value.settings.enabled)
const skillControllers = new Map()
let requestController = null
let overviewController = null
let overviewSequence = 0
let channelsController = null
let channelsSequence = 0
let mcpController = null
let mcpSequence = 0
let sandboxController = null
let sandboxSequence = 0
let eventController = null
let eventSequence = 0
let eventsUnavailable = false
let pollTimer = null
let openSequence = 0
let sessionSequence = 0
let previousFocus = null
let disposed = false
let actionController = null
let actionSequence = 0
let skillEditSequence = 0

function stopAction() {
  actionSequence += 1
  actionController?.abort()
  actionController = null
  busy.value = ''
}
function startAction(label) {
  if (disposed || !props.open || busy.value) return null
  const controller = new AbortController(), sequence = ++actionSequence, opening = openSequence
  const revision = getAuthRevision(), token = getToken()
  actionController = controller
  busy.value = label
  return {
    signal: controller.signal,
    current: () => !disposed && props.open && !controller.signal.aborted && opening === openSequence
      && sequence === actionSequence && revision === getAuthRevision() && token === getToken(),
    finish: () => {
      if (sequence === actionSequence) { busy.value = ''; actionController = null }
    },
  }
}

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
  return Object.fromEntries(Object.entries(defaults).map(([key, fallback]) => [key, value?.[key] ?? fallback]))
}
function channelKindName(kind) { return { telegram: 'Telegram Bot', bot: 'Telegram Bot', wecom: '企业微信' }[kind] || kind }
function channelName(kind, id) { return channels.value.channels.find(item => item.kind === kind && String(item.id) === String(id))?.name || id }
async function compressImage(file) {
  const url = URL.createObjectURL(file)
  const image = new Image()
  let released = false
  const cleanup = () => {
    if (released) return
    released = true
    image.removeAttribute('src')
    URL.revokeObjectURL(url)
  }
  imageReadCleanups.add(cleanup)
  try {
    image.src = url
    await image.decode()
    if (!image.naturalWidth || !image.naturalHeight || image.naturalWidth * image.naturalHeight > 40_000_000) throw new Error('图片尺寸过大，请缩小后再选择')
    const scale = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight))
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale))
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale))
    const context = canvas.getContext('2d')
    if (!context) throw new Error('浏览器暂时无法处理图片，请重试')
    context.fillStyle = '#ffffff'
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.drawImage(image, 0, 0, canvas.width, canvas.height)
    const dataUrl = canvas.toDataURL('image/jpeg', .86)
    canvas.width = canvas.height = 1
    if (!dataUrl.startsWith('data:image/jpeg;base64,')) throw new Error('图片处理失败，请换一张图片')
    return dataUrl
  } finally { imageReadCleanups.delete(cleanup); cleanup() }
}
function cancelAttachmentReads() {
  attachmentSequence += 1
  for (const cleanup of imageReadCleanups) cleanup()
  imageReadCleanups.clear()
  for (const reader of attachmentReaders) reader.abort()
  attachmentReaders.clear()
  imageProcessing.value = false
}
async function addImages(files) {
  if (!files.length) return
  if (activeRun.value) { toast.error('执行中的任务暂不支持补充图片，请等待完成或新建会话'); return }
  if (busy.value) { toast.error('正在提交，请稍后再添加图片'); return }
  if (imageProcessing.value) { toast.error('图片正在处理，请稍后再选择'); return }
  const key = selectedId.value || 'new'
  const generation = imageDraftGenerations[key] || 0
  const sequence = attachmentSequence
  imageProcessing.value = true
  try {
    for (const file of files) {
      if (disposed || sequence !== attachmentSequence || generation !== (imageDraftGenerations[key] || 0)) return
      const current = imageDrafts.value[key] || []
      if (current.length >= 3) { toast.error('一次最多发送 3 张图片'); break }
      if (!['image/jpeg', 'image/png', 'image/webp', 'image/gif'].includes(file.type)) { toast.error('请使用 JPEG、PNG、WebP 或 GIF 图片'); continue }
      if (!file.size || file.size > 5 * 1024 * 1024) { toast.error('每张图片不能超过 5 MiB'); continue }
      if (current.reduce((sum, item) => sum + item.size, 0) + file.size > 8 * 1024 * 1024) { toast.error('图片合计不能超过 8 MiB'); continue }
      try {
        const dataUrl = await compressImage(file)
        if (disposed || sequence !== attachmentSequence || generation !== (imageDraftGenerations[key] || 0)) return
        imageDrafts.value[key] = [...(imageDrafts.value[key] || []), { id: crypto.randomUUID(), name: file.name || '粘贴的图片', size: file.size, dataUrl }]
      } catch (error) { if (!disposed && sequence === attachmentSequence) toast.error(error.message || '图片无法读取，请重新选择') }
    }
  } finally { if (sequence === attachmentSequence) imageProcessing.value = false }
}
function selectImages(event) {
  const files = [...(event.target.files || [])]
  event.target.value = ''
  addImages(files)
}
function pasteImages(event) {
  const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter(Boolean)
  if (!files.length) return
  event.preventDefault()
  addImages(files)
}
function removeImage(id) {
  const key = selectedId.value || 'new'
  imageDrafts.value[key] = (imageDrafts.value[key] || []).filter(item => item.id !== id)
}
function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    attachmentReaders.add(reader)
    reader.onload = () => { attachmentReaders.delete(reader); resolve(String(reader.result).split(',')[1]) }
    reader.onerror = () => { attachmentReaders.delete(reader); reject(new Error('附件无法读取，请重新选择')) }
    reader.onabort = () => { attachmentReaders.delete(reader); reject(new DOMException('读取已取消', 'AbortError')) }
    reader.readAsDataURL(file)
  })
}
async function selectFiles(event) {
  const files = [...(event.target.files || [])]
  event.target.value = ''
  if (!files.length) return
  if (activeRun.value) { toast.error('执行中的任务暂不支持补充附件，请等待完成或新建会话'); return }
  if (busy.value || imageProcessing.value) { toast.error('正在处理，请稍后再添加附件'); return }
  const key = selectedId.value || 'new'
  const generation = imageDraftGenerations[key] || 0
  const sequence = attachmentSequence
  imageProcessing.value = true
  try {
    for (const file of files) {
      if (disposed || sequence !== attachmentSequence || generation !== (imageDraftGenerations[key] || 0)) return
      const extension = file.name.split('.').at(-1).toLowerCase()
      const audio = ['wav', 'wave', 'mp3', 'mpga', 'mpeg', 'webm', 'ogg', 'oga', 'opus', 'm4a', 'amr', 'aac', 'mp4'].includes(extension)
      if (!audio && !['txt', 'md', 'json', 'csv', 'tsv', 'yaml', 'yml', 'toml', 'ini', 'log'].includes(extension)) { toast.error('请选择文本文件或常见格式的音频'); continue }
      if (!file.size || file.size > (audio ? 8 * 1024 * 1024 : 64 * 1024)) { toast.error(audio ? '音频不能超过 8 MiB' : '单个文本文件不能超过 64 KiB'); continue }
      if (audio && audioDrafts.value[key]) { toast.error('一次只能发送一个音频，请先移除已选音频'); continue }
      const current = fileDrafts.value[key] || []
      if (!audio && current.length >= 3) { toast.error('一次最多发送 3 个文本文件'); continue }
      if (!audio && current.reduce((sum, item) => sum + item.size, 0) + file.size > 10000) { toast.error('文本文件合计不能超过 10,000 字节'); continue }
      try {
        if (!audio) {
          const text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
          if (text.includes('\0')) throw new Error('请使用 UTF-8 编码的文本文件')
        }
        if (disposed || sequence !== attachmentSequence || generation !== (imageDraftGenerations[key] || 0)) return
        const data = await readFileBase64(file)
        if (disposed || sequence !== attachmentSequence || generation !== (imageDraftGenerations[key] || 0)) return
        const attachment = { id: crypto.randomUUID(), name: file.name, size: file.size, content_type: file.type, data }
        if (audio) audioDrafts.value[key] = attachment
        else fileDrafts.value[key] = [...(fileDrafts.value[key] || []), attachment]
      } catch (error) { if (!disposed && sequence === attachmentSequence && error.name !== 'AbortError') toast.error(error instanceof TypeError ? '请使用 UTF-8 编码的文本文件' : (error.message || '附件无法读取，请重新选择')) }
    }
  } finally { if (sequence === attachmentSequence) imageProcessing.value = false }
}
function removeFile(id) {
  const key = selectedId.value || 'new'
  fileDrafts.value[key] = (fileDrafts.value[key] || []).filter(item => item.id !== id)
}
function removeAudio() { delete audioDrafts.value[selectedId.value || 'new'] }
function composerKeydown(event) {
  if (event.key !== 'Enter' || event.isComposing || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return
  if (window.matchMedia('(pointer: coarse)').matches) return
  event.preventDefault()
  sendMessage()
}
function stepContent(value) { return typeof value === 'string' ? value : (value == null ? '' : JSON.stringify(value, null, 2)) }
function releaseArtifact(id) {
  artifactControllers.get(id)?.abort()
  artifactControllers.delete(id)
  const preview = artifactPreviews.value.get(id)
  if (preview?.url) URL.revokeObjectURL(preview.url)
  artifactPreviews.value.delete(id)
}
function clearArtifacts() {
  artifactGeneration += 1
  for (const controller of artifactControllers.values()) controller.abort()
  artifactControllers.clear()
  for (const preview of artifactPreviews.value.values()) {
    if (preview.url) URL.revokeObjectURL(preview.url)
  }
  artifactPreviews.value.clear()
}
async function loadArtifact(item) {
  if (disposed || !props.open || !sandboxArtifacts.value.some(value => value.id === item.id)
      || ['loading', 'ready', 'expired'].includes(artifactPreviews.value.get(item.id)?.status)) return
  const controller = new AbortController(), generation = artifactGeneration
  const revision = getAuthRevision(), token = getToken(), sessionId = selectedId.value, runId = run.value?.id
  const current = () => !disposed && props.open && !controller.signal.aborted && generation === artifactGeneration
    && revision === getAuthRevision() && token === getToken() && sessionId === selectedId.value
    && runId === run.value?.id && sandboxArtifacts.value.some(value => value.id === item.id)
  artifactControllers.set(item.id, controller)
  artifactPreviews.value.set(item.id, { status: 'loading' })
  try {
    const blob = await api.getAssistantSandboxArtifact(item.id, controller.signal)
    if (!current()) return
    const url = URL.createObjectURL(blob)
    artifactPreviews.value.set(item.id, { status: 'ready', url })
  } catch (error) {
    if (current() && error.name !== 'AbortError') artifactPreviews.value.set(item.id, {
      status: [404, 410].includes(error.status) ? 'expired' : 'error',
    })
  } finally {
    if (artifactControllers.get(item.id) === controller) artifactControllers.delete(item.id)
  }
}
function artifactImageFailed(id, url) {
  if (artifactPreviews.value.get(id)?.url !== url) return
  releaseArtifact(id)
  artifactPreviews.value.set(id, { status: 'error' })
}
const unsubscribeArtifactAuth = subscribeAuthChanges(() => {
  clearArtifacts()
  stopAction()
  openSequence += 1
  sessionSequence += 1
  stopPolling()
  stopEvents()
  overviewController?.abort()
  channelsController?.abort()
  mcpController?.abort()
  sandboxController?.abort()
  for (const controller of skillControllers.values()) controller.abort()
  cancelAttachmentReads()
  drafts.value = {}; imageDrafts.value = {}; fileDrafts.value = {}; audioDrafts.value = {}
  sandboxDraft.value.token = ''; mcpDraft.value.token = ''; pairResult.value = null
  emit('close')
})
watch([() => props.open, selectedId, () => run.value?.id], clearArtifacts, { flush: 'sync' })
watch(() => sandboxArtifacts.value.map(item => item.id).join(','), () => {
  const ids = new Set(sandboxArtifacts.value.map(item => item.id))
  for (const id of artifactPreviews.value.keys()) if (!ids.has(id)) releaseArtifact(id)
}, { flush: 'sync' })
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
function stopEvents() {
  eventSequence += 1
  eventController?.abort()
  eventController = null
}
async function startEvents(id) {
  if (eventController || eventsUnavailable || !activeRun.value || disposed || !props.open || document.hidden) return
  const controller = new AbortController()
  const sequence = ++eventSequence
  const opening = openSequence
  eventController = controller
  const current = () => !disposed && props.open && !document.hidden && opening === openSequence && sequence === eventSequence && selectedId.value === id
  try {
    await api.streamAssistantSession(id, {
      signal: controller.signal,
      onSnapshot: async value => {
        if (!current() || value.id !== id) return
        const shouldScroll = nearBottom()
        sessionSequence += 1
        requestController?.abort()
        sessionLoading.value = false
        session.value = value
        updateSessionSummary(value)
        pollError.value = ''
        await scrollToBottom(shouldScroll && !value.run?.pending_confirmation && !value.run?.pending_input)
        if (current() && !activeRun.value) stopEvents()
      },
      onRun: async value => {
        if (!current() || !value.id || session.value?.run?.id !== value.id) return
        if (value.updated_at && run.value?.updated_at && value.updated_at < run.value.updated_at) return
        const shouldScroll = nearBottom()
        sessionSequence += 1
        requestController?.abort()
        sessionLoading.value = false
        session.value.run = value
        pollError.value = ''
        await scrollToBottom(shouldScroll)
      },
    })
    if (current() && activeRun.value) eventsUnavailable = true
  } catch (error) {
    if (error.name !== 'AbortError' && current()) eventsUnavailable = true
  } finally {
    if (eventController === controller) eventController = null
    if (current()) schedulePoll()
  }
}
function schedulePoll() {
  clearTimeout(pollTimer)
  if (disposed || !props.open || document.hidden || !activeRun.value || !selectedId.value) return
  if (eventController) return
  if (!eventsUnavailable) { startEvents(selectedId.value); return }
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
  stopEvents()
  eventsUnavailable = false
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
    catalog.value = { ...catalog.value, ...value, settings: settingsValue(value.settings) }
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
  const action = startAction('session')
  if (!action) return
  try {
    const value = await api.createAssistantSession({}, action.signal)
    if (!action.current()) return
    updateSessionSummary(value)
    await selectSession(value.id)
    if (!action.current()) return
    activeTab.value = 'chat'
    await nextTick()
    composer.value?.focus()
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`创建会话失败：${error.message}`) }
  finally { action.finish() }
}
async function deleteSession() {
  const id = selectedId.value
  if (!id || busy.value || activeRun.value) return
  const action = startAction('delete-session')
  if (!action) return
  try {
    if (!await confirm({ title: '删除会话', message: '删除后无法恢复。', danger: true, confirmText: '删除' }) || !action.current()) return
    await api.deleteAssistantSession(id, action.signal)
    if (!action.current()) return
    catalog.value.sessions = catalog.value.sessions.filter(item => item.id !== id)
    delete drafts.value[id]
    delete imageDrafts.value[id]
    delete fileDrafts.value[id]
    delete audioDrafts.value[id]
    delete draftRevisions[id]
    imageDraftGenerations[id] = (imageDraftGenerations[id] || 0) + 1
    if (selectedId.value === id) await selectSession(catalog.value.sessions[0]?.id || '')
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`删除失败：${error.message}`) }
  finally { action.finish() }
}
async function sendMessage() {
  if (!canSend.value) return
  const content = chatDraft.value.trim()
  const images = [...draftImages.value]
  const files = [...draftFiles.value]
  const audio = draftAudio.value
  let draftKey = selectedId.value || 'new'
  const submittedRevision = draftRevisions[draftKey] || 0
  let id = selectedId.value
  const currentRun = run.value
  const steering = activeRun.value
  const input = pendingInput.value
  const action = startAction('send')
  if (!action) return
  try {
    if (!id) {
      const created = await api.createAssistantSession({ title: content.slice(0, 40) || '附件问题' }, action.signal)
      if (!action.current()) return
      id = created.id
      updateSessionSummary(created)
      // Migrate the latest draft now, not in catch: input may have changed while
      // creating the session, and may change again while sending its message.
      if (selectedId.value === '') {
        drafts.value[id] = drafts.value.new || ''
        imageDrafts.value[id] = imageDrafts.value.new || []
        fileDrafts.value[id] = fileDrafts.value.new || []
        if (audioDrafts.value.new) audioDrafts.value[id] = audioDrafts.value.new
        draftRevisions[id] = draftRevisions.new || 0
        delete drafts.value.new
        delete imageDrafts.value.new
        delete fileDrafts.value.new
        delete audioDrafts.value.new
        delete draftRevisions.new
        draftKey = id
        selectedId.value = id
      }
    }
    const result = input && currentRun?.id === run.value?.id
      ? await api.answerAssistantRun(currentRun.id, input.id, content, action.signal)
      : (steering ? await api.steerAssistantRun(currentRun.id, content, action.signal) : await api.sendAssistantMessage(id, content, images.map(item => item.dataUrl), files.map(({ name, data, content_type }) => ({ name, data, content_type })), audio ? { name: audio.name, data: audio.data, content_type: audio.content_type } : null, action.signal))
    if (!action.current()) return
    if ((draftRevisions[draftKey] || 0) === submittedRevision && (drafts.value[draftKey] || '').trim() === content) drafts.value[draftKey] = ''
    const sentIds = new Set(images.map(item => item.id))
    imageDrafts.value[draftKey] = (imageDrafts.value[draftKey] || []).filter(item => !sentIds.has(item.id))
    const sentFiles = new Set(files.map(item => item.id))
    fileDrafts.value[draftKey] = (fileDrafts.value[draftKey] || []).filter(item => !sentFiles.has(item.id))
    if (audioDrafts.value[draftKey]?.id === audio?.id) delete audioDrafts.value[draftKey]
    if (selectedId.value === id && props.open) {
      if (!session.value) session.value = { id, messages: [], run: result }
      else session.value.run = result
      await loadSession(id)
    }
  } catch (error) {
    if (action.current() && error.name !== 'AbortError') toast.error(`发送失败：${error.message}`)
  } finally { action.finish() }
}
async function actOnRun(action, approved) {
  const currentRun = run.value
  const id = selectedId.value
  if (!currentRun || (action !== 'cancel' && !currentRun.pending_confirmation)) return
  const operation = startAction(action)
  if (!operation) return
  try {
    const result = action === 'cancel'
      ? await api.cancelAssistantRun(currentRun.id, operation.signal)
      : await api.confirmAssistantRun(currentRun.id, currentRun.pending_confirmation.id, approved, operation.signal)
    if (operation.current() && selectedId.value === id && session.value) {
      session.value.run = result
      await loadSession(id, { polling: true })
    }
  } catch (error) { if (operation.current() && error.name !== 'AbortError') toast.error(`操作失败：${error.message}`) }
  finally { operation.finish() }
}
async function answerChoice(option) {
  const currentRun = run.value
  const input = pendingInput.value
  const id = selectedId.value
  if (!currentRun || !input || inputExpired.value || busy.value) return
  const action = startAction('answer')
  if (!action) return
  try {
    const result = await api.answerAssistantRun(currentRun.id, input.id, option.id, action.signal)
    if (action.current() && selectedId.value === id && session.value) {
      session.value.run = result
      await loadSession(id, { polling: true })
    }
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`回答失败：${error.message}`) }
  finally { action.finish() }
}
async function saveSettings() {
  const action = startAction('settings')
  if (!action) return
  overviewSequence += 1
  overviewController?.abort()
  const submittedRevision = formRevisions.settings
  const submitted = settingsValue(settingsDraft.value)
  try {
    const value = await api.saveAssistantSettings(submitted, action.signal)
    if (!action.current()) return
    catalog.value.settings = settingsValue(value)
    if (formRevisions.settings === submittedRevision) settingsDraft.value = settingsValue(value)
    catalog.value.issue_status = { ...catalog.value.issue_status, enabled: !!(value.enabled && value.allow_actions && value.allow_issue_submission) }
    toast.success('助手设置已保存')
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`保存失败：${error.message}`) }
  finally { action.finish() }
}
async function loadChannels() {
  if (!props.open || disposed) return
  const opening = openSequence
  const sequence = ++channelsSequence
  channelsController?.abort()
  const controller = new AbortController()
  channelsController = controller
  channelsLoading.value = true
  channelsError.value = ''
  try {
    const value = await api.getAssistantChannels(controller.signal)
    if (disposed || !props.open || opening !== openSequence || sequence !== channelsSequence) return
    channels.value = { bindings: value.bindings || [], channels: value.channels || [] }
    if (!channelKinds.value.includes(pairDraft.value.kind)) pairDraft.value.kind = channelKinds.value[0] || ''
    if (!availableChannels.value.some(item => String(item.id) === pairDraft.value.channel_id)) pairDraft.value.channel_id = String(availableChannels.value[0]?.id || '')
  } catch (error) {
    if (error.name !== 'AbortError' && opening === openSequence && sequence === channelsSequence && props.open) channelsError.value = `渠道读取失败：${error.message}`
  } finally {
    if (opening === openSequence && sequence === channelsSequence) channelsLoading.value = false
    if (channelsController === controller) channelsController = null
  }
}
async function loadMcpServers() {
  if (!props.open || disposed) return
  const opening = openSequence
  const sequence = ++mcpSequence
  mcpController?.abort()
  const controller = new AbortController()
  mcpController = controller
  mcpLoading.value = true
  mcpError.value = ''
  try {
    const value = await api.getAssistantMcpServers(controller.signal)
    if (disposed || !props.open || opening !== openSequence || sequence !== mcpSequence) return
    mcpServers.value = Array.isArray(value) ? value : (value.servers || [])
  } catch (error) {
    if (error.name !== 'AbortError' && opening === openSequence && sequence === mcpSequence && props.open) mcpError.value = `MCP 服务读取失败：${error.message}`
  } finally {
    if (opening === openSequence && sequence === mcpSequence) mcpLoading.value = false
    if (mcpController === controller) mcpController = null
  }
}
function publicSandboxConfig(value) {
  return { enabled: value?.enabled === true,
    url: typeof value?.url === 'string' ? value.url : defaultSandboxUrl,
    token_configured: value?.token_configured === true }
}
function clearSandboxTokenChanged() {
  if (sandboxDraft.value.clear_token) sandboxDraft.value.enabled = false
}
async function loadSandbox() {
  if (!props.open || disposed || sandboxBusy.value) return
  const opening = openSequence, sequence = ++sandboxSequence
  const revision = formRevisions.sandbox, wasDirty = sandboxDirty.value
  sandboxController?.abort()
  const controller = new AbortController()
  sandboxController = controller
  sandboxLoading.value = true
  sandboxError.value = ''
  try {
    const value = publicSandboxConfig(await api.getAssistantSandbox(controller.signal))
    if (disposed || !props.open || opening !== openSequence || sequence !== sandboxSequence) return
    sandboxConfig.value = value
    if (!wasDirty && revision === formRevisions.sandbox) sandboxDraft.value = {
      enabled: value.enabled, url: value.url, token: '', clear_token: false,
    }
    sandboxLoaded.value = true
    sandboxHealthy.value = false
    sandboxNotice.value = ''
  } catch (error) {
    if (error.name !== 'AbortError' && props.open && opening === openSequence && sequence === sandboxSequence) sandboxError.value = `沙箱配置读取失败：${error.message}`
  } finally {
    if (opening === openSequence && sequence === sandboxSequence) sandboxLoading.value = false
    if (sandboxController === controller) sandboxController = null
  }
}
async function saveSandbox() {
  if (!sandboxLoaded.value || sandboxLoading.value || sandboxBusy.value || !sandboxDirty.value) return
  const submitted = { ...sandboxDraft.value }, revision = formRevisions.sandbox
  const opening = openSequence, sequence = ++sandboxSequence
  sandboxController?.abort()
  const controller = new AbortController()
  sandboxController = controller
  sandboxBusy.value = 'save'
  sandboxError.value = ''; sandboxNotice.value = ''; sandboxHealthy.value = false
  try {
    const values = { enabled: submitted.enabled, url: submitted.url.trim() }
    if (submitted.clear_token) values.clear_token = true
    else if (submitted.token) values.token = submitted.token
    const value = publicSandboxConfig(await api.saveAssistantSandbox(values, controller.signal))
    if (disposed || !props.open || opening !== openSequence || sequence !== sandboxSequence) return
    sandboxConfig.value = value
    if (revision === formRevisions.sandbox) sandboxDraft.value = {
      enabled: value.enabled, url: value.url, token: '', clear_token: false,
    }
    sandboxNotice.value = value.enabled ? '沙箱配置已保存，尚未检查连接。' : '沙箱配置已保存，沙箱功能已关闭。'
  } catch (error) {
    if (error.name !== 'AbortError' && props.open && opening === openSequence && sequence === sandboxSequence) sandboxError.value = `沙箱配置保存失败：${error.message}`
  } finally {
    if (opening === openSequence && sequence === sandboxSequence) sandboxBusy.value = ''
    if (sandboxController === controller) sandboxController = null
  }
}
async function checkSandbox() {
  if (!sandboxLoaded.value || sandboxLoading.value || sandboxBusy.value || sandboxDirty.value || !sandboxConfig.value.enabled) return
  const opening = openSequence, sequence = ++sandboxSequence, revision = formRevisions.sandbox
  sandboxController?.abort()
  const controller = new AbortController()
  sandboxController = controller
  sandboxBusy.value = 'check'
  sandboxError.value = ''; sandboxNotice.value = ''; sandboxHealthy.value = false
  try {
    const value = await api.checkAssistantSandbox(controller.signal)
    if (disposed || !props.open || opening !== openSequence || sequence !== sandboxSequence || revision !== formRevisions.sandbox) return
    if (value?.ok !== true || value?.service !== 'awbotnest-sandbox' || value?.protocol !== 1) throw new Error('执行服务返回了不兼容的健康状态')
    sandboxHealthy.value = true
  } catch (error) {
    if (error.name !== 'AbortError' && props.open && opening === openSequence && sequence === sandboxSequence && revision === formRevisions.sandbox) sandboxError.value = `沙箱连接检查失败：${error.message}`
  } finally {
    if (opening === openSequence && sequence === sandboxSequence) sandboxBusy.value = ''
    if (sandboxController === controller) sandboxController = null
  }
}
function editMcpServer(item) {
  mcpDraft.value = item ? { ...emptyMcpDraft(), id: item.id, name: item.name, url: item.url, enabled: item.enabled, token_configured: item.token_configured, read_only_tools: (item.read_only_tools || []).join('\n') } : emptyMcpDraft()
  mcpEditorOpen.value = true
}
function mcpValues(item) {
  return { name: item.name, url: item.url, enabled: !!item.enabled, read_only_tools: Array.isArray(item.read_only_tools) ? [...item.read_only_tools] : item.read_only_tools.split('\n').map(value => value.trim()).filter(Boolean) }
}
async function saveMcpServer() {
  const action = startAction('mcp-save')
  if (!action) return
  const submitted = { ...mcpDraft.value }
  const revision = formRevisions.mcp
  try {
    const values = mcpValues(submitted)
    if (submitted.clear_token) values.token = ''
    else if (submitted.token) values.token = submitted.token
    if (submitted.id) await api.updateAssistantMcpServer(submitted.id, values, action.signal)
    else await api.createAssistantMcpServer(values, action.signal)
    if (!action.current()) return
    if (revision === formRevisions.mcp) { mcpDraft.value = emptyMcpDraft(); mcpEditorOpen.value = false }
    if (submitted.id) delete mcpDiscoveries.value[submitted.id]
    await loadMcpServers()
    if (action.current()) toast.success('MCP 服务已保存')
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`MCP 保存失败：${error.message}`) }
  finally { action.finish() }
}
async function toggleMcpServer(item) {
  const action = startAction(`mcp-${item.id}`)
  if (!action) return
  try {
    await api.updateAssistantMcpServer(item.id, { ...mcpValues(item), enabled: !item.enabled }, action.signal)
    if (!action.current()) return
    delete mcpDiscoveries.value[item.id]
    await loadMcpServers()
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`MCP 修改失败：${error.message}`) }
  finally { action.finish() }
}
async function discoverMcpServer(item) {
  if (busy.value || !item.enabled || !catalog.value.settings.enabled || !catalog.value.settings.mcp_enabled) return
  const action = startAction(`discover-${item.id}`)
  if (!action) return
  try {
    const value = await api.discoverAssistantMcpServer(item.id, action.signal)
    if (action.current()) mcpDiscoveries.value[item.id] = value
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`MCP 连接失败：${error.message}`) }
  finally { action.finish() }
}
async function deleteMcpServer(item) {
  const action = startAction(`mcp-${item.id}`)
  if (!action) return
  try {
    if (!await confirm({ title: '删除 MCP 服务', message: `确定删除“${item.name}”？`, danger: true, confirmText: '删除服务' }) || !action.current()) return
    await api.deleteAssistantMcpServer(item.id, action.signal)
    if (!action.current()) return
    delete mcpDiscoveries.value[item.id]
    await loadMcpServers()
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`MCP 删除失败：${error.message}`) }
  finally { action.finish() }
}
async function pairChannel() {
  if (busy.value || !pairDraft.value.kind || !pairDraft.value.channel_id) return
  const action = startAction('pair')
  if (!action) return
  pairResult.value = null
  try {
    const { kind, channel_id } = pairDraft.value
    const value = await api.pairAssistantChannel(kind, channel_id, action.signal)
    if (action.current()) pairResult.value = { ...value, kind, channel_id }
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`配对码生成失败：${error.message}`) }
  finally { action.finish() }
}
async function revokeChannel(item) {
  const action = startAction('revoke')
  if (!action) return
  try {
    if (!await confirm({ title: '撤销助手配对', message: '撤销后，该账号将不能通过此渠道使用系统助手。', danger: true, confirmText: '撤销配对' }) || !action.current()) return
    await api.revokeAssistantChannel(item.id, action.signal)
    if (!action.current()) return
    await loadChannels()
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`撤销失败：${error.message}`) }
  finally { action.finish() }
}
async function saveResource(kind) {
  const action = startAction(kind)
  if (!action) return
  const submittedRevision = formRevisions[kind]
  try {
    if (kind === 'memory') {
      await api.saveAssistantMemory(memoryDraft.value.trim(), memoryEditingId.value, action.signal)
      if (!action.current()) return
      if (formRevisions.memory === submittedRevision) {
        memoryDraft.value = ''
        memoryEditingId.value = ''
      }
    } else if (kind === 'skill') {
      const value = { ...skillDraft.value }
      await api.saveAssistantSkill({ ...(value.id ? { id: value.id } : {}), name: value.name, description: value.description, content: value.content }, action.signal)
      if (!action.current()) return
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
      await api.saveAssistantTask({ name: value.name, prompt: value.prompt, ...schedule }, action.signal)
      if (!action.current()) return
      if (formRevisions.task === submittedRevision) taskDraft.value = { name: '', prompt: '', mode: 'once', run_at: '', cron: '0 9 * * *' }
    }
    await loadOverview()
    if (action.current()) toast.success('已保存')
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`保存失败：${error.message}`) }
  finally { action.finish() }
}
async function deleteResource(kind, item) {
  if (busy.value || (kind === 'skill' && item.builtin)) return
  const action = startAction(`${kind}-${item.id}`)
  if (!action) return
  try {
    if (!await confirm({ title: `删除${{ memory: '记忆', skill: '技能', task: '预约' }[kind]}`, message: '确定删除这条记录？', danger: true, confirmText: '删除' }) || !action.current()) return
    await ({ memory: api.deleteAssistantMemory, skill: api.deleteAssistantSkill, task: api.deleteAssistantTask })[kind](item.id, action.signal)
    if (!action.current()) return
    if (kind === 'skill') delete skillDetails.value[item.id]
    await loadOverview()
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`删除失败：${error.message}`) }
  finally { action.finish() }
}
async function readSkill(item) {
  if (skillDetails.value[item.id]) return skillDetails.value[item.id]
  if (skillLoading.value[item.id]) return null
  const controller = new AbortController()
  const opening = openSequence
  skillControllers.set(item.id, controller)
  skillLoading.value[item.id] = true
  delete skillLoadErrors.value[item.id]
  try {
    const value = await api.getAssistantSkill(item.id, controller.signal)
    if (disposed || !props.open || controller.signal.aborted || opening !== openSequence) return null
    skillDetails.value[item.id] = value
    return value
  } catch (error) {
    if (!disposed && props.open && opening === openSequence && error.name !== 'AbortError') skillLoadErrors.value[item.id] = `读取失败：${error.message}`
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
  const sequence = ++skillEditSequence, opening = openSequence
  const value = await readSkill(item)
  if (!value || disposed || !props.open || opening !== openSequence || sequence !== skillEditSequence) return
  if (revision !== formRevisions.skill) { toast.error('已保留正在编辑的内容，请再次点击编辑'); return }
  skillDraft.value = { id: item.id, name: value.name, description: value.description, content: value.content }
  skillEditorOpen.value = true
  await nextTick()
  dialog.value?.querySelector('#assistant-skill-name')?.focus()
}
async function toggleTask(item) {
  const action = startAction(`task-${item.id}`)
  if (!action) return
  try {
    await api.toggleAssistantTask(item.id, !item.enabled, action.signal)
    if (!action.current()) return
    await loadOverview()
  } catch (error) { if (action.current() && error.name !== 'AbortError') toast.error(`修改失败：${error.message}`) }
  finally { action.finish() }
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
  if (document.hidden) { stopPolling(); stopEvents(); sessionLoading.value = false }
  else if (props.open && selectedId.value) { eventsUnavailable = false; loadSession(selectedId.value, { polling: true }) }
}
watch(() => props.open, async (value) => {
  openSequence += 1
  const opening = openSequence
  stopAction()
  skillEditSequence += 1
  sessionSequence += 1
  stopPolling()
  stopEvents()
  eventsUnavailable = false
  overviewController?.abort()
  channelsController?.abort()
  channelsSequence += 1
  channelsLoading.value = false
  mcpController?.abort()
  mcpSequence += 1
  mcpLoading.value = false
  sandboxController?.abort()
  sandboxSequence += 1
  sandboxLoading.value = false
  sandboxBusy.value = ''
  sandboxHealthy.value = false
  sandboxNotice.value = ''
  sessionLoading.value = false
  if (!value) {
    pairResult.value = null
    mcpDraft.value.token = ''
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
  if (disposed || !props.open || opening !== openSequence) return
  dialog.value?.focus()
  await loadOverview(true)
  if (!disposed && props.open && opening === openSequence && activeTab.value === 'settings') loadChannels()
}, { immediate: true })
watch(activeTab, () => {
  if (props.open && !loading.value && activeTab.value !== 'chat') loadOverview()
  if (props.open && activeTab.value === 'settings') loadChannels()
})
watch(() => pairDraft.value.kind, () => {
  if (!availableChannels.value.some(item => String(item.id) === pairDraft.value.channel_id)) pairDraft.value.channel_id = String(availableChannels.value[0]?.id || '')
})
onBeforeUnmount(() => {
  disposed = true
  stopAction()
  cancelAttachmentReads()
  unsubscribeArtifactAuth()
  clearArtifacts()
  openSequence += 1
  stopPolling()
  stopEvents()
  overviewController?.abort()
  channelsController?.abort()
  mcpController?.abort()
  sandboxController?.abort()
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
                    <p v-if="message.images?.length" class="assistant-image-note"><ImagePlus aria-hidden="true" />附图 {{ message.images.length }} 张</p>
                    <p v-if="message.attachments?.length" class="assistant-image-note"><FileText aria-hidden="true" />{{ message.attachments.map(item => item.name).join('、') }}</p>
                    <p v-if="message.audio" class="assistant-image-note"><Mic aria-hidden="true" />{{ message.audio.name || '语音消息' }}</p>
                  </article>
                  <section v-if="run" class="assistant-run" aria-label="执行进度">
                    <div class="assistant-run-head"><span class="assistant-run-status" :class="run.status"><i v-if="activeRun"></i><Check v-else-if="run.status === 'completed'" aria-hidden="true" />{{ runLabel }}</span><span v-if="run.usage?.total_tokens" class="assistant-usage" :title="`输入 ${tokens(run.usage.input_tokens)} · 输出 ${tokens(run.usage.output_tokens)}`">{{ run.usage.estimated ? '约 ' : '' }}{{ tokens(run.usage.total_tokens) }} tokens</span></div>
                    <div v-if="activeRun && run.partial_text" class="assistant-stream-text assistant-markdown" v-html="markdown(run.partial_text)"></div>
                    <ol v-if="run.plan?.length" class="assistant-plan" aria-label="任务计划"><li v-for="step in run.plan" :key="step.id" :class="step.status"><Check v-if="step.status === 'completed'" aria-hidden="true" /><span v-else class="assistant-plan-marker" aria-hidden="true"></span><span>{{ step.title }}</span><small>{{ statusLabels[step.status] || '' }}</small></li></ol>
                    <details v-if="run.steps?.length" class="assistant-steps" :open="activeRun"><summary>执行步骤 <span>{{ run.steps.length }}</span></summary><ol><li v-for="step in run.steps" :key="step.id"><div><strong>{{ step.title || '处理步骤' }}</strong><span>{{ statusLabels[step.status] || '' }}</span></div><pre v-if="step.content">{{ stepContent(step.content) }}</pre></li></ol></details>
                    <section v-if="sandboxArtifacts.length" class="assistant-artifacts" aria-label="浏览器截图">
                      <figure v-for="item in sandboxArtifacts" :key="item.id">
                        <figcaption>{{ item.title || '浏览器截图' }}</figcaption>
                        <img v-if="artifactPreviews.get(item.id)?.status === 'ready'" :src="artifactPreviews.get(item.id).url" :alt="item.title || '浏览器截图'" @error="artifactImageFailed(item.id, $event.currentTarget.src)" />
                        <p v-else-if="artifactPreviews.get(item.id)?.status === 'expired'" role="status">截图已过期，请重新生成</p>
                        <template v-else><p v-if="artifactPreviews.get(item.id)?.status === 'error'" role="status">截图加载失败</p><button type="button" class="btn" :disabled="artifactPreviews.get(item.id)?.status === 'loading'" @click="loadArtifact(item)">{{ artifactPreviews.get(item.id)?.status === 'loading' ? '正在读取截图…' : (artifactPreviews.get(item.id)?.status === 'error' ? '重新加载截图' : '查看截图') }}</button></template>
                      </figure>
                    </section>
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
                    <div v-if="pendingInput" class="assistant-question" role="group" aria-label="助手需要补充信息">
                      <h3>需要补充信息</h3><p>{{ pendingInput.question }}</p>
                      <small v-if="inputExpired">问题已过期，请重新发起任务。</small>
                      <template v-else><div v-if="pendingInput.options?.length" class="assistant-choices"><button v-for="option in pendingInput.options" :key="option.id" type="button" class="btn" :disabled="!!busy" @click="answerChoice(option)">{{ option.label }}</button></div><button v-if="!inputChoiceOnly" type="button" class="assistant-text-button" @click="composer?.focus()">输入其他答案</button></template>
                    </div>
                    <p v-if="run.error" class="assistant-error" role="alert">{{ run.error }}</p>
                  </section>
                  <div v-if="pollError" class="assistant-poll-error" role="status"><span>{{ pollError }}</span><button type="button" @click="loadSession(selectedId, { polling: true })">重试</button></div>
                </template>
              </div>
              <form class="assistant-composer" @submit.prevent="sendMessage">
                <div v-if="!catalog.settings.enabled" class="assistant-disabled">助手已关闭。<button type="button" @click="activeTab = 'settings'">前往设置</button></div>
                <label for="assistant-message" class="assistant-composer-label">{{ pendingInput ? '回答助手' : (activeRun ? '补充要求' : '发送消息') }}<span v-if="activeRun && !pendingInput">会在下一步处理</span></label>
                <textarea id="assistant-message" ref="composer" v-model="chatDraft" rows="3" :maxlength="pendingInput ? 2000 : 8000" :disabled="!catalog.settings.enabled || inputChoiceOnly" :placeholder="inputChoiceOnly ? '请先选择上方选项' : (pendingInput ? '输入答案…' : (activeRun ? '继续补充要求，不会中断当前步骤' : '输入问题，或粘贴图片…'))" @keydown="composerKeydown" @paste="pasteImages"></textarea>
                <p v-if="answerTooLong && !inputChoiceOnly" class="assistant-image-note" role="status">回答不能超过 2000 字，请精简后发送。原草稿已保留。</p>
                <div v-if="draftImages.length" class="assistant-image-drafts" aria-label="待发送的图片"><figure v-for="image in draftImages" :key="image.id"><img :src="image.dataUrl" :alt="image.name" /><button type="button" class="assistant-icon" :aria-label="`移除图片：${image.name}`" @click="removeImage(image.id)"><X /></button></figure></div>
                <div v-if="draftFiles.length || draftAudio" class="assistant-file-drafts" aria-label="待发送的文件"><div v-for="file in draftFiles" :key="file.id"><FileText aria-hidden="true" /><span>{{ file.name }}</span><button type="button" class="assistant-icon" :aria-label="`移除文件：${file.name}`" @click="removeFile(file.id)"><X /></button></div><div v-if="draftAudio"><Mic aria-hidden="true" /><span>{{ draftAudio.name }}</span><button type="button" class="assistant-icon" aria-label="移除音频" @click="removeAudio"><X /></button></div></div>
                <p v-if="imageProcessing" class="assistant-image-note" role="status">正在处理附件…</p>
                <p v-if="activeRun && hasAttachments" class="assistant-image-note" role="status">任务执行中不能补充附件，附件草稿已保留。</p>
                <input ref="imagePicker" type="file" accept="image/jpeg,image/png,image/webp,image/gif" multiple hidden aria-label="选择助手图片" @change="selectImages" />
                <input ref="filePicker" type="file" accept=".txt,.md,.json,.csv,.tsv,.yaml,.yml,.toml,.ini,.log,.wav,.wave,.mp3,.mpga,.mpeg,.webm,.ogg,.oga,.opus,.m4a,.amr,.aac,.mp4" multiple hidden aria-label="选择文本或音频附件" @change="selectFiles" />
                <div class="assistant-composer-footer"><div class="assistant-composer-tools"><button type="button" class="assistant-icon" aria-label="添加图片" title="最多 3 张，每张 5 MiB，合计 8 MiB" :disabled="activeRun || !!busy || imageProcessing || draftImages.length >= 3 || !catalog.settings.enabled" @click="imagePicker?.click()"><ImagePlus /></button><button type="button" class="assistant-icon" aria-label="添加文本或音频附件" title="文本合计 10,000 字节；音频 8 MiB" :disabled="activeRun || !!busy || imageProcessing || !catalog.settings.enabled" @click="filePicker?.click()"><Paperclip /></button><span>{{ catalog.settings.allow_actions ? '修改需确认' : '只读模式' }}</span></div><div><button v-if="activeRun" type="button" class="btn" :disabled="!!busy" @click="actOnRun('cancel')"><Square aria-hidden="true" />停止</button><button type="submit" class="btn btn-primary" :disabled="!canSend"><Send aria-hidden="true" />{{ busy === 'send' ? '发送中…' : (pendingInput ? '回答' : (activeRun ? '补充' : '发送')) }}</button></div></div>
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
                  <label class="assistant-switch-row"><span>允许网页搜索和阅读<small>仅访问公网网页，不登录、不执行网页脚本。</small></span><input v-model="settingsDraft.web_enabled" type="checkbox" /></label>
                  <label class="assistant-switch-row"><span>允许使用外部工具（MCP）</span><input v-model="settingsDraft.mcp_enabled" type="checkbox" /></label>
                  <label class="assistant-switch-row"><span>预约通知<small>通过默认 Bot 接收结果和待确认提醒。</small></span><input v-model="settingsDraft.notify_tasks" type="checkbox" /></label>
                  <div class="assistant-issue-settings">
                    <label class="assistant-switch-row"><span>允许提交 GitHub Issue<small>{{ settingsDraft.allow_actions ? '提交前确认仓库、标题和正文。' : '提交前需先开启“允许执行修改操作”。' }}</small></span><input v-model="settingsDraft.allow_issue_submission" type="checkbox" /></label>
                    <p class="assistant-field-note">{{ catalog.issue_status?.configured ? '使用系统设置中已保存的 GitHub Token。' : '未配置 GitHub Token，只生成问题报告。可在系统设置中配置。' }}</p>
                  </div>
                  <details class="assistant-advanced"><summary>请求设置</summary><label class="assistant-field">单次调用超时（秒）<input v-model.number="settingsDraft.timeout_seconds" type="number" min="15" max="300" class="input" /></label></details>
                  <button type="submit" class="btn btn-primary" :disabled="!!busy">{{ busy === 'settings' ? '保存中…' : '保存设置' }}</button>
                </form>
                <section class="assistant-channel-settings" aria-labelledby="assistant-channels-title">
                  <div class="assistant-section-heading"><h3 id="assistant-channels-title">消息渠道</h3><button type="button" class="assistant-icon" aria-label="刷新助手渠道" :disabled="channelsLoading || !!busy" @click="loadChannels"><RefreshCw /></button></div>
                  <p class="assistant-section-description">配对后，可在 Telegram 私聊或企业微信中使用助手。未配对的账号无法使用。</p>
                  <p v-if="channelsError" class="assistant-error" role="alert">{{ channelsError }} <button type="button" class="assistant-text-button" @click="loadChannels">重试</button></p>
                  <p v-if="channelsLoading" class="assistant-field-note" role="status">正在读取渠道…</p>
                  <form v-if="channels.channels.length" class="assistant-form" @submit.prevent="pairChannel">
                    <div class="assistant-form-grid"><label class="assistant-field">渠道类型<select v-model="pairDraft.kind" class="select" :disabled="!!busy"><option v-for="kind in channelKinds" :key="kind" :value="kind">{{ channelKindName(kind) }}</option></select></label><label class="assistant-field">消息渠道<select v-model="pairDraft.channel_id" class="select" :disabled="!!busy"><option v-for="channel in availableChannels" :key="channel.id" :value="String(channel.id)">{{ channel.name || channel.id }}</option></select></label></div>
                    <button type="submit" class="btn" :disabled="!!busy || channelsLoading || !pairDraft.channel_id">{{ busy === 'pair' ? '正在生成…' : '生成配对码' }}</button>
                  </form>
                  <p v-else-if="!channelsLoading && !channelsError" class="assistant-field-note">还没有可用渠道，请先在系统设置中添加 Bot 或企业微信。</p>
                  <div v-if="pairResult" class="assistant-pair-result" role="status"><p>向 {{ channelName(pairResult.kind, pairResult.channel_id) }} {{ pairResult.kind === 'telegram' ? '发送私聊消息' : '发送消息' }}：</p><code>{{ pairResult.command }}</code><small>一次有效，{{ formatTime(pairResult.expires_at) }} 前使用。请勿分享给其他人。</small><button type="button" class="assistant-text-button" @click="loadChannels">已发送，刷新配对结果</button></div>
                  <div v-if="channels.bindings.length" class="assistant-resource-list"><article v-for="binding in channels.bindings" :key="binding.id" class="assistant-resource"><div><h4>{{ channelName(binding.kind, binding.channel_id) }}</h4><p>{{ channelKindName(binding.kind) }} · {{ binding.user_id }}</p><small>{{ binding.enabled ? '助手模式已开启' : '助手模式已退出' }} · {{ formatTime(binding.created_at) }}</small></div><button type="button" class="assistant-icon" :aria-label="`撤销配对：${binding.user_id}`" :disabled="!!busy" @click="revokeChannel(binding)"><Trash2 /></button></article></div>
                </section>
                <details class="assistant-sandbox-settings" @toggle="$event.target.open && loadSandbox()">
                  <summary>独立沙箱</summary>
                  <p class="assistant-section-description">代码、文件和浏览器操作在独立容器中执行，每次操作仍需确认。不会挂载系统配置或插件目录。</p>
                  <p class="assistant-field-note">需单独部署执行服务，并在两端设置相同的 Token。保存配置不会启动容器或检查连接。</p>
                  <div class="assistant-section-heading"><h3>执行服务配置</h3><button type="button" class="assistant-icon" aria-label="刷新沙箱配置" :disabled="sandboxLoading || !!sandboxBusy" @click="loadSandbox"><RefreshCw /></button></div>
                  <p v-if="sandboxLoading" class="assistant-field-note" role="status">正在读取沙箱配置…</p>
                  <p v-if="sandboxError" class="assistant-error" role="alert">{{ sandboxError }} <button v-if="!sandboxLoaded" type="button" class="assistant-text-button" @click="loadSandbox">重试</button></p>
                  <form v-if="sandboxLoaded" class="assistant-form assistant-sandbox-form" @submit.prevent="saveSandbox">
                    <label class="assistant-switch-row"><span>启用独立沙箱</span><input v-model="sandboxDraft.enabled" type="checkbox" :disabled="sandboxLoading || sandboxDraft.clear_token" /></label>
                    <label class="assistant-field">沙箱服务地址<input v-model="sandboxDraft.url" class="input" type="url" maxlength="2048" required spellcheck="false" autocapitalize="off" :disabled="sandboxLoading" :placeholder="defaultSandboxUrl" /></label>
                    <label class="assistant-field">沙箱 Token<input v-model="sandboxDraft.token" class="input" type="password" autocomplete="new-password" maxlength="256" spellcheck="false" autocapitalize="off" :disabled="sandboxLoading || sandboxDraft.clear_token" :placeholder="sandboxConfig.token_configured ? '已保存，留空不修改' : '未配置'" /></label>
                    <p class="assistant-field-note">使用至少 32 位的随机 Token，不要填写系统登录密码或 AI 服务密钥。</p>
                    <label v-if="sandboxConfig.token_configured" class="assistant-switch-row"><span>清除沙箱 Token</span><input v-model="sandboxDraft.clear_token" type="checkbox" :disabled="sandboxLoading" @change="clearSandboxTokenChanged" /></label>
                    <div class="assistant-inline-actions assistant-sandbox-actions">
                      <button type="submit" class="btn btn-primary" :disabled="sandboxLoading || !!sandboxBusy || !sandboxDirty">{{ sandboxBusy === 'save' ? '保存中…' : '保存沙箱配置' }}</button>
                      <button type="button" class="btn" :disabled="sandboxLoading || !!sandboxBusy || sandboxDirty || !sandboxConfig.enabled" @click="checkSandbox">{{ sandboxBusy === 'check' ? '检查中…' : '检查沙箱连接' }}</button>
                    </div>
                    <p v-if="sandboxHealthy" class="assistant-sandbox-status" role="status"><Check aria-hidden="true" />执行服务连接正常</p>
                    <p v-else-if="sandboxNotice" class="assistant-field-note" role="status">{{ sandboxNotice }}</p>
                    <p v-else-if="sandboxDirty" class="assistant-field-note">配置尚未保存，保存后可检查连接。</p>
                    <p v-else class="assistant-field-note">{{ sandboxConfig.enabled ? '尚未检查连接。' : '沙箱功能未开启。' }}</p>
                  </form>
                </details>
                <details class="assistant-mcp-settings" @toggle="$event.target.open && loadMcpServers()">
                  <summary>外部工具（MCP）</summary>
                  <p class="assistant-section-description">接入公网 HTTP MCP 服务。只读工具需由管理员明确指定，其余调用都需要确认。</p>
                  <p v-if="!catalog.settings.enabled || !catalog.settings.mcp_enabled" class="assistant-field-note">启用助手和外部工具并保存设置后，可连接和调用 MCP 服务。</p>
                  <div class="assistant-inline-actions"><button type="button" class="btn" :disabled="!!busy || mcpServers.length >= 4" @click="editMcpServer()"><Plus aria-hidden="true" />添加服务</button><button type="button" class="assistant-icon" aria-label="刷新 MCP 服务" :disabled="mcpLoading || !!busy" @click="loadMcpServers"><RefreshCw /></button></div>
                  <p v-if="mcpLoading" class="assistant-field-note" role="status">正在读取 MCP 服务…</p>
                  <p v-if="mcpError" class="assistant-error" role="alert">{{ mcpError }} <button type="button" class="assistant-text-button" @click="loadMcpServers">重试</button></p>
                  <form v-if="mcpEditorOpen" class="assistant-form assistant-mcp-editor" @submit.prevent="saveMcpServer">
                    <div class="assistant-form-grid"><label class="assistant-field">服务名称<input v-model="mcpDraft.name" class="input" maxlength="80" required /></label><label class="assistant-field">服务地址<input v-model="mcpDraft.url" class="input" type="url" maxlength="1000" required placeholder="https://example.com/mcp" spellcheck="false" /></label></div>
                    <label class="assistant-field">Token（可选）<input v-model="mcpDraft.token" class="input" type="password" autocomplete="new-password" maxlength="4096" :disabled="mcpDraft.clear_token" :placeholder="mcpDraft.token_configured ? '已保存，留空不修改' : '未配置'" spellcheck="false" /></label>
                    <label v-if="mcpDraft.token_configured" class="assistant-switch-row"><span>清除已保存的 Token</span><input v-model="mcpDraft.clear_token" type="checkbox" /></label>
                    <label class="assistant-field">只读工具名称<textarea v-model="mcpDraft.read_only_tools" class="textarea" rows="2" maxlength="3300" placeholder="每行一个，留空则每次调用都需确认" spellcheck="false"></textarea></label>
                    <label class="assistant-switch-row"><span>启用此服务</span><input v-model="mcpDraft.enabled" type="checkbox" /></label>
                    <div class="assistant-inline-actions"><button type="button" class="btn" :disabled="!!busy" @click="mcpEditorOpen = false; mcpDraft = emptyMcpDraft()">取消</button><button type="submit" class="btn btn-primary" :disabled="!!busy">{{ busy === 'mcp-save' ? '保存中…' : '保存服务' }}</button></div>
                  </form>
                  <div v-if="mcpServers.length" class="assistant-resource-list">
                    <article v-for="server in mcpServers" :key="server.id" class="assistant-resource">
                      <div>
                        <h4>{{ server.name }}<span class="assistant-task-state">{{ server.enabled ? '已启用' : '未启用' }}</span></h4>
                        <p class="assistant-mcp-url">{{ server.url }}</p>
                        <small>{{ server.token_configured ? '已配置 Token' : '未配置 Token' }} · {{ server.read_only_tools?.length || 0 }} 个只读工具</small>
                        <div class="assistant-inline-actions assistant-mcp-actions">
                          <button type="button" class="assistant-text-button" :disabled="!!busy" @click="editMcpServer(server)">编辑</button>
                          <button type="button" class="assistant-text-button" :disabled="!!busy" @click="toggleMcpServer(server)">{{ server.enabled ? '停用' : '启用' }}</button>
                          <button type="button" class="assistant-text-button" :disabled="!!busy || !server.enabled || !catalog.settings.enabled || !catalog.settings.mcp_enabled" @click="discoverMcpServer(server)">{{ busy === `discover-${server.id}` ? '正在连接…' : '连接并读取工具' }}</button>
                        </div>
                        <details v-if="mcpDiscoveries[server.id]" class="assistant-mcp-tools">
                          <summary>已发现 {{ mcpDiscoveries[server.id].tools?.length || 0 }} 个工具<span v-if="mcpDiscoveries[server.id].skipped">，{{ mcpDiscoveries[server.id].skipped }} 个不支持</span></summary>
                          <ul><li v-for="tool in mcpDiscoveries[server.id].tools || []" :key="tool.name"><strong>{{ tool.name }}</strong><small>{{ tool.read_only ? '只读' : '需确认' }}</small><p>{{ tool.description }}</p></li></ul>
                        </details>
                      </div>
                      <button type="button" class="assistant-icon" :aria-label="`删除 MCP 服务：${server.name}`" :disabled="!!busy" @click="deleteMcpServer(server)"><Trash2 /></button>
                    </article>
                  </div>
                  <p v-else-if="!mcpLoading && !mcpError && !mcpEditorOpen" class="assistant-list-empty">还没有添加 MCP 服务</p>
                </details>
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
.assistant-run-status.waiting_input { color: var(--accent-hover); }
.assistant-run-status svg { width: 15px; height: 15px; }
.assistant-stream-text { margin: 8px 0 20px; }
.assistant-run-status i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.assistant-usage { font-size: 11px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.assistant-plan { list-style: none; margin: 0 0 16px; padding: 0; }
.assistant-plan li { display: flex; align-items: baseline; gap: 9px; padding: 6px 0; color: var(--text-secondary); font-size: 12px; }
.assistant-plan li > span:nth-child(2) { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.assistant-plan li.running { color: var(--text-primary); }
.assistant-plan li.completed > svg { color: var(--success); }
.assistant-plan li > svg { width: 13px; height: 13px; flex: 0 0 13px; align-self: center; }
.assistant-plan-marker { width: 7px; height: 7px; margin: 3px; border: 1px solid currentColor; border-radius: 50%; flex: 0 0 7px; align-self: center; }
.assistant-plan li.running .assistant-plan-marker { color: var(--accent-hover); background: currentColor; }
.assistant-plan small { flex-shrink: 0; font-size: 11px; }
.assistant-steps summary { color: var(--text-secondary); font-size: 12px; cursor: pointer; }
.assistant-steps summary span { margin-left: 4px; }
.assistant-steps ol { list-style: none; padding-top: 10px; }
.assistant-steps li { margin-bottom: 10px; padding: 10px 0; }
.assistant-steps li > div { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; font-size: 12px; }
.assistant-steps li > div strong { min-width: 0; overflow-wrap: anywhere; }
.assistant-steps li > div span { color: var(--text-secondary); white-space: nowrap; font-size: 11px; }
.assistant-steps pre { max-height: 220px; overflow: auto; margin-top: 8px; padding: 10px 12px; border-radius: var(--radius-sm); background: var(--bg-base); color: var(--text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; line-height: 1.6; }
.assistant-artifacts { display: grid; gap: 14px; margin: 16px 0; }
.assistant-artifacts figure { min-width: 0; margin: 0; padding: 12px; border: 1px solid var(--border-light); border-radius: var(--radius-sm); }
.assistant-artifacts figcaption { margin-bottom: 10px; color: var(--text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.assistant-artifacts img { display: block; max-width: 100%; max-height: 320px; height: auto; margin: 0 auto; object-fit: contain; border-radius: 4px; }
.assistant-artifacts p { margin: 8px 0; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }
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
.assistant-question { padding-top: 18px; margin-top: 18px; border-top: 1px solid var(--border-light); }
.assistant-question h3 { font-size: 14px; }
.assistant-question > p { margin: 10px 0 14px; color: var(--text-primary); font-size: 13px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.assistant-question > small { color: var(--warning); }
.assistant-choices { display: flex; flex-wrap: wrap; gap: 8px; }
.assistant-choices .btn { max-width: 100%; white-space: normal; text-align: left; overflow-wrap: anywhere; }
.assistant-history-note { margin-bottom: 20px; color: var(--text-secondary); font-size: 11px; text-align: center; }
.assistant-error { font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; margin-top: 12px; }
.assistant-poll-error { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; color: var(--warning); padding-top: 12px; }
.assistant-poll-error button, .assistant-disabled button { color: var(--accent-hover); background: none; border: none; font: inherit; text-decoration: underline; cursor: pointer; }
.assistant-composer { flex-shrink: 0; max-height: 58%; overflow-y: auto; scrollbar-color: var(--border-light) transparent; scrollbar-width: thin; padding: 16px 22px max(18px, env(safe-area-inset-bottom)); border-top: 1px solid var(--border); background: var(--bg-card); }
.assistant-composer-label { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
.assistant-composer-label span { font-size: 11px; }
.assistant-composer textarea { width: 100%; display: block; min-height: 72px; max-height: 180px; resize: vertical; padding: 12px; border: 1px solid var(--border-light); border-radius: 10px; color: var(--text-primary); background: var(--bg-elevated); font: inherit; font-size: 14px; line-height: 1.6; caret-color: var(--accent-hover); }
.assistant-composer textarea::placeholder { color: var(--text-secondary); }
.assistant-composer-footer { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; }
.assistant-composer-footer > div { display: flex; gap: 8px; }
.assistant-composer-footer svg { width: 14px; height: 14px; }
.assistant-composer-tools { align-items: center; min-width: 0; }
.assistant-composer-tools > span { color: var(--text-secondary); font-size: 11px; }
.assistant-composer-tools .assistant-icon { width: 34px; height: 34px; }
.assistant-composer-tools .assistant-icon svg { width: 18px; height: 18px; }
.assistant-image-drafts { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
.assistant-image-drafts figure { position: relative; width: 72px; height: 64px; border-radius: var(--radius-sm); border: 1px solid var(--border-light); background: var(--bg-base); }
.assistant-image-drafts img { display: block; width: 100%; height: 100%; object-fit: cover; border-radius: inherit; }
.assistant-image-drafts .assistant-icon { position: absolute; right: -7px; top: -7px; width: 28px; height: 28px; padding: 5px; color: var(--text-primary); background: var(--bg-elevated); border: 1px solid var(--border-light); border-radius: 50%; }
.assistant-image-drafts .assistant-icon:hover { background: var(--bg-hover); }
.assistant-image-note { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 11px; line-height: 1.7; color: var(--text-secondary); }
.assistant-image-note svg { width: 14px; height: 14px; }
.assistant-file-drafts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; max-height: 92px; overflow-y: auto; }
.assistant-file-drafts > div { display: flex; align-items: center; gap: 7px; min-width: 0; max-width: 100%; padding-left: 10px; border: 1px solid var(--border-light); border-radius: var(--radius-sm); color: var(--text-secondary); background: var(--bg-elevated); }
.assistant-file-drafts > div > svg { width: 15px; height: 15px; flex: 0 0 15px; }
.assistant-file-drafts span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.assistant-file-drafts .assistant-icon { width: 32px; height: 32px; padding: 8px; }
.assistant-disabled { color: var(--warning); font-size: 12px; margin-bottom: 10px; }
.assistant-management { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 26px 24px; scrollbar-color: var(--border-light) transparent; scrollbar-width: thin; }
.assistant-section-heading { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.assistant-section-heading h3 { font-size: 16px; }
.assistant-section-heading > svg { color: var(--accent); width: 18px; height: 18px; }
.assistant-section-heading > span { font-size: 12px; color: var(--text-secondary); margin-left: auto; }
.assistant-section-description, .assistant-field-note { color: var(--text-secondary); font-size: 12px; line-height: 1.7; margin-bottom: 22px; }
.assistant-field-note { margin-top: -8px; margin-bottom: 4px; }
.assistant-issue-settings { display: flex; flex-direction: column; gap: 16px; padding-top: 12px; border-top: 1px solid var(--border); }
.assistant-field small { font-size: 11px; line-height: 1.7; color: var(--text-secondary); }
.assistant-form { display: flex; flex-direction: column; align-items: stretch; gap: 16px; }
.assistant-form > .btn, .assistant-form > .assistant-inline-actions { align-self: flex-start; }
.assistant-field { display: flex; flex-direction: column; gap: 8px; font-size: 12px; color: var(--text-secondary); min-width: 0; }
.assistant-field input, .assistant-field textarea, .assistant-field select { min-width: 0; font-size: 14px; }
.assistant-switch-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 48px; color: var(--text-primary); font-size: 13px; }
.assistant-switch-row small { display: block; font-size: 12px; color: var(--text-secondary); margin-top: 7px; line-height: 1.7; }
.assistant-panel input[type="checkbox"] { width: 18px; height: 18px; flex: 0 0 18px; accent-color: var(--accent); cursor: pointer; }
.assistant-channel-settings { margin-top: 32px; padding-top: 28px; border-top: 1px solid var(--border); }
.assistant-channel-settings .assistant-section-heading > button { margin-left: auto; }
.assistant-channel-settings .assistant-form-grid { margin-top: 0; }
.assistant-pair-result { margin-top: 20px; padding: 16px 0; border-top: 1px solid var(--border); }
.assistant-pair-result p { color: var(--text-secondary); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.assistant-pair-result code { display: block; margin: 10px 0; padding: 12px; background: var(--bg-elevated); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 15px; user-select: all; overflow-wrap: anywhere; }
.assistant-pair-result small { display: block; color: var(--text-secondary); font-size: 11px; line-height: 1.7; }
.assistant-mcp-settings, .assistant-sandbox-settings { margin-top: 32px; padding-top: 24px; border-top: 1px solid var(--border); }
.assistant-mcp-settings > summary, .assistant-sandbox-settings > summary { cursor: pointer; font-size: 14px; color: var(--text-primary); }
.assistant-mcp-settings > .assistant-section-description, .assistant-sandbox-settings > .assistant-section-description { margin-top: 16px; }
.assistant-mcp-settings > .assistant-field-note, .assistant-sandbox-settings > .assistant-field-note { margin-top: 12px; }
.assistant-sandbox-settings > .assistant-section-heading { margin-top: 20px; }
.assistant-sandbox-settings .assistant-section-heading > button { margin-left: auto; }
.assistant-sandbox-actions { flex-wrap: wrap; }
.assistant-sandbox-status { display: flex; align-items: center; gap: 6px; color: var(--accent); font-size: 12px; line-height: 1.7; }
.assistant-sandbox-status > svg { width: 16px; height: 16px; flex-shrink: 0; }
.assistant-mcp-editor { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--border); }
.assistant-mcp-editor .assistant-form-grid { margin-top: 0; }
.assistant-mcp-actions { flex-wrap: wrap; column-gap: 18px; margin-top: 6px; }
.assistant-mcp-url { word-break: break-all; }
.assistant-mcp-tools ul { list-style: none; padding-top: 10px; }
.assistant-mcp-tools li { padding: 10px 0; border-bottom: 1px solid var(--border); }
.assistant-mcp-tools strong { font-size: 12px; overflow-wrap: anywhere; }
.assistant-mcp-tools li > small { margin-left: 10px; }
.assistant-advanced { padding-top: 12px; border-top: 1px solid var(--border); }
.assistant-advanced summary { cursor: pointer; font-size: 12px; color: var(--text-secondary); }
.assistant-advanced > .assistant-field { margin-top: 16px; }
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
  .assistant-composer-tools .assistant-icon { width: 44px; height: 44px; }
  .assistant-image-drafts { gap: 16px; }
  .assistant-image-drafts figure { width: 68px; height: 56px; }
  .assistant-image-drafts .assistant-icon { right: -12px; top: -12px; width: 44px; height: 44px; padding: 12px; }
  .assistant-file-drafts .assistant-icon { width: 44px; height: 44px; }
  :global(html.keyboard-open) .assistant-composer textarea { min-height: 48px; max-height: 64px; }
  :global(html.keyboard-open) .assistant-image-drafts figure { height: 44px; }
  .assistant-management { padding: 22px max(16px, env(safe-area-inset-right)) max(22px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }
  .assistant-text-button { min-height: 44px; }
  .assistant-form-grid { gap: 12px; }
}
@media (max-width: 360px) { .assistant-composer-tools > span { max-width: 48px; } .assistant-form-grid { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { .assistant-enter-active, .assistant-leave-active, .assistant-enter-active .assistant-panel, .assistant-leave-active .assistant-panel { transition: none; } }
</style>
