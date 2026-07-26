<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { api } from '../api'
import { toast } from '../composables/toast'
import { confirm } from '../composables/confirm'
import { publishNotificationSync, subscribeNotificationSync } from '../utils/notificationSync'

const tab = ref('login')   // login | notify | ai | api | system | maint

const TABS = [
  { key: 'login',  label: '安全认证' },
  { key: 'notify', label: '通知推送' },
  { key: 'ai',     label: 'AI 服务' },
  { key: 'api',    label: '开放接口' },
  { key: 'system', label: '系统配置' },
  { key: 'maint',  label: '维护' },
]

const s = ref(null)
const loading = ref(true)
const saving = ref(false)
const err = ref('')          // 仅用于加载失败（页面无数据时内联提示）

// 未保存改动检测：快照 vs 当前
const savedSnap = ref('')
const dirty = computed(() => !!s.value && JSON.stringify(s.value) !== savedSnap.value)
const ai = ref(null)
const aiSavedSnap = ref('')
const aiLoading = ref(false)
const aiSaving = ref(false)
const aiStatus = ref(null)
const aiPlugins = ref([])
const aiModels = ref({})
const aiModelLoading = ref({})
const aiTesting = ref({})
const aiDirty = computed(() => !!ai.value && JSON.stringify(ai.value) !== aiSavedSnap.value)
const currentDirty = computed(() => tab.value === 'ai' ? aiDirty.value : dirty.value)
const currentSaving = computed(() => tab.value === 'ai' ? aiSaving.value : saving.value)
const anyDirty = computed(() => dirty.value || aiDirty.value)
// 保存后需重启提示
const restartHint = ref(false)
const restarting = ref(false)
let restartTimer = null   // 重启轮询定时器；提升为模块级以便组件卸载时清理
const notificationSyncSource = `settings_${Math.random().toString(36).slice(2)}`
let stopNotificationSync = null
// 用户又开始改动时，隐藏“需重启”横幅（新改动得重新保存）
watch(dirty, (d) => { if (d) restartHint.value = false })

const AI_CAPABILITIES = [
  { key: 'text', label: '文字模型', desc: '总结、改写、问答和结构化处理' },
  { key: 'vision', label: '图片识别', desc: '识别图片、截图、海报和文字内容' },
  { key: 'image', label: '生图模型', desc: '根据提示词生成图片并保存给插件' },
]

function newAiProvider() {
  return {
    id: `ai_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
    name: '新 AI 服务',
    enabled: true,
    base_url: 'https://api.openai.com/v1',
    api_key: '',
  }
}

function newAiModel(providerId = '') {
  const id = `model_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
  const existing = new Set((ai.value.models || []).map((item) => item.alias))
  let number = ai.value.models.length + 1
  while (existing.has(`model-${number}`)) number += 1
  return {
    id,
    alias: `model-${number}`,
    name: '新模型',
    enabled: true,
    provider_id: providerId || ai.value.providers[0]?.id || '',
    model: '',
    capabilities: ['text'],
  }
}

async function loadAiSettings() {
  aiLoading.value = true
  try {
    const [configData, pluginData] = await Promise.all([
      api.getAiSettings(),
      api.listPlugins(),
    ])
    ai.value = configData.settings
    aiStatus.value = configData.status || {}
    aiPlugins.value = pluginData.plugins || []
    aiSavedSnap.value = JSON.stringify(ai.value)
  } catch (e) {
    toast.error('读取 AI 设置失败：' + e.message)
  } finally {
    aiLoading.value = false
  }
}

async function saveAiSettings() {
  if (!ai.value || aiSaving.value) return false
  aiSaving.value = true
  try {
    const data = await api.saveAiSettings(ai.value)
    ai.value = data.settings
    aiSavedSnap.value = JSON.stringify(ai.value)
    toast.success('AI 设置已保存并立即生效')
    return true
  } catch (e) {
    toast.error('保存 AI 设置失败：' + e.message)
    return false
  } finally {
    aiSaving.value = false
  }
}

function undoCurrent() {
  if (tab.value === 'ai') {
    loadAiSettings()
  } else {
    load()
  }
}

function saveCurrent() {
  return tab.value === 'ai' ? saveAiSettings() : save()
}

function addAiProvider() {
  const provider = newAiProvider()
  ai.value.providers.push(provider)
}

function removeAiProvider(index) {
  if (ai.value.providers.length <= 1) {
    toast.error('至少保留一个 AI 服务')
    return
  }
  const removed = ai.value.providers[index]
  ai.value.providers.splice(index, 1)
  const removedModelIds = new Set(
    ai.value.models.filter((item) => item.provider_id === removed.id).map((item) => item.id)
  )
  ai.value.models = ai.value.models.filter((item) => item.provider_id !== removed.id)
  for (const capability of AI_CAPABILITIES) {
    const target = ai.value.capabilities[capability.key]
    if (removedModelIds.has(target.default_model)) target.default_model = ''
    if (removedModelIds.has(target.fallback_model)) {
      ai.value.capabilities[capability.key].fallback_model = ''
    }
  }
  delete aiModels.value[removed.id]
}

async function fetchAiModels(provider) {
  aiModelLoading.value[provider.id] = true
  try {
    const data = await api.getAiModels(provider)
    aiModels.value[provider.id] = data.models || []
    toast.success(`已读取 ${data.count || 0} 个模型`)
  } catch (e) {
    toast.error('读取模型失败：' + e.message)
  } finally {
    aiModelLoading.value[provider.id] = false
  }
}

function aiProviderModels(providerId) {
  return aiModels.value[providerId] || []
}

function addAiModel() {
  ai.value.models.push(newAiModel())
}

function removeAiModel(index) {
  const removed = ai.value.models[index]
  ai.value.models.splice(index, 1)
  for (const capability of AI_CAPABILITIES) {
    const target = ai.value.capabilities[capability.key]
    if (target.default_model === removed.id) target.default_model = ''
    if (target.fallback_model === removed.id) target.fallback_model = ''
  }
}

function toggleAiModelCapability(model, capability) {
  const index = model.capabilities.indexOf(capability)
  if (index >= 0) model.capabilities.splice(index, 1)
  else model.capabilities.push(capability)
}

function availableAiModels(capability) {
  return ai.value.models.filter((item) =>
    item.enabled && item.model && item.capabilities.includes(capability)
  )
}

async function testAi(capability) {
  if (aiDirty.value && !(await saveAiSettings())) return
  aiTesting.value[capability] = true
  try {
    const result = await api.testAiCapability(capability)
    toast.success(result.message || '测试成功')
    aiStatus.value = await api.getAiStatus()
  } catch (e) {
    toast.error('测试失败：' + e.message)
  } finally {
    aiTesting.value[capability] = false
  }
}

function pluginPermission(pluginId) {
  return ai.value?.plugin_permissions?.[pluginId] || null
}

function pluginAiEnabled(pluginId) {
  return pluginPermission(pluginId)?.enabled !== false
}

function pluginAiCapability(pluginId, capability) {
  const permission = pluginPermission(pluginId)
  return !permission || permission.capabilities.includes(capability)
}

function ensurePluginPermission(pluginId) {
  if (!ai.value.plugin_permissions[pluginId]) {
    ai.value.plugin_permissions[pluginId] = {
      enabled: true,
      capabilities: AI_CAPABILITIES.map((item) => item.key),
    }
  }
  return ai.value.plugin_permissions[pluginId]
}

function togglePluginAi(pluginId) {
  const permission = ensurePluginPermission(pluginId)
  permission.enabled = !permission.enabled
}

function togglePluginAiCapability(pluginId, capability) {
  const permission = ensurePluginPermission(pluginId)
  const index = permission.capabilities.indexOf(capability)
  if (index >= 0) permission.capabilities.splice(index, 1)
  else permission.capabilities.push(capability)
}

async function load(silent = false) {
  if (!silent) loading.value = true
  err.value = ''
  try {
    const d = await api.getSettings()
    s.value = d.settings
    // 保证嵌套结构存在
    s.value.proxy_set = s.value.proxy_set || { proxy_enable: false, proxy: {}, PROXY_URL: '' }
    s.value.proxy_set.proxy = s.value.proxy_set.proxy || {}
    if (s.value.PIP_INDEX_URL === undefined) s.value.PIP_INDEX_URL = ''
    s.value.DB_INFO = s.value.DB_INFO || {}
    s.value.ACCOUNTS = s.value.ACCOUNTS || []
    s.value.BOTS = Array.isArray(s.value.BOTS) ? s.value.BOTS : []
    if (s.value.WEBHOOK_SECRET === undefined) s.value.WEBHOOK_SECRET = ''
    if (s.value.API_KEY === undefined) s.value.API_KEY = ''
    if (s.value.DEFAULT_BOT_CHAT_ID === undefined) s.value.DEFAULT_BOT_CHAT_ID = ''
    if (s.value.BOT_NAME === undefined) s.value.BOT_NAME = '主要通知渠道'
    if (s.value.DEFAULT_BOT_ID === undefined) s.value.DEFAULT_BOT_ID = 'default'
    // 初始化通知渠道配置（数组格式）
    s.value.NOTIFICATION_CHANNELS = Array.isArray(s.value.NOTIFICATION_CHANNELS) ? s.value.NOTIFICATION_CHANNELS : []

    // 自动迁移旧Bot配置到通知渠道
    if (s.value.NOTIFICATION_CHANNELS.length === 0 && (s.value.BOT_TOKEN || s.value.BOTS?.length > 0)) {
      // 迁移主Bot
      if (s.value.BOT_TOKEN) {
        s.value.NOTIFICATION_CHANNELS.push({
          id: 'default',
          name: s.value.BOT_NAME || '主要通知渠道',
          type: 'telegram',
          enabled: true,
          is_default: s.value.DEFAULT_BOT_ID === 'default',
          config: {
            token: s.value.BOT_TOKEN,
            chat_id: s.value.DEFAULT_BOT_CHAT_ID || ''
          },
          plugins: []  // 新增：初始化插件列表
        })
      }

      // 迁移额外的Bot
      if (Array.isArray(s.value.BOTS)) {
        s.value.BOTS.forEach((b) => {
          if (b.token) {
            s.value.NOTIFICATION_CHANNELS.push({
              id: b.id || `migrated_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
              name: b.name || '未命名Bot',
              type: 'telegram',
              enabled: true,
              is_default: s.value.DEFAULT_BOT_ID === b.id,
              config: {
                token: b.token,
                chat_id: b.chat_id || ''
              },
              plugins: []  // 新增：初始化插件列表
            })
          }
        })
      }

      console.log(`已自动迁移 ${s.value.NOTIFICATION_CHANNELS.length} 个Bot到通知渠道`)
    }

    s.value.LOG_CLEANER = s.value.LOG_CLEANER || { enabled: true, keep_lines: 100, hour: 3, minute: 0 }
    // 补齐旧数据里额外 Bot 缺失的 chat_id 字段，保证 v-model 响应
    s.value.BOTS.forEach((b) => { if (b.chat_id === undefined) b.chat_id = '' })
    savedSnap.value = JSON.stringify(s.value)   // 基线快照
  } catch (e) { err.value = e.message } finally { if (!silent) loading.value = false }
}

// 同步通知渠道到旧Bot配置，保持后端兼容
function syncChannelsToOldBots() {
  if (!Array.isArray(s.value.NOTIFICATION_CHANNELS)) return

  // 清空旧配置
  s.value.BOTS = []
  s.value.BOT_TOKEN = ''
  s.value.BOT_NAME = ''
  s.value.DEFAULT_BOT_CHAT_ID = ''
  s.value.DEFAULT_BOT_ID = ''

  const telegramChannels = s.value.NOTIFICATION_CHANNELS
    .filter(ch => ch.type === 'telegram' && ch.enabled && ch.config?.token)
  const builtinChannel = telegramChannels.find(ch => ch.id === 'default')
  const defaultChannel = telegramChannels.find(ch => ch.is_default) || telegramChannels[0]

  // 只有 id=default 的渠道映射到内置 Bot，其余渠道保留自己的 id。
  if (builtinChannel) {
    s.value.BOT_TOKEN = builtinChannel.config.token
    s.value.BOT_NAME = builtinChannel.name || '主要通知渠道'
    s.value.DEFAULT_BOT_CHAT_ID = builtinChannel.config?.chat_id || ''
  }
  telegramChannels.forEach((ch) => {
    if (ch.id === 'default') return
    s.value.BOTS.push({
      id: ch.id,
      name: ch.name,
      token: ch.config.token,
      chat_id: ch.config?.chat_id || ''
    })
  })
  if (defaultChannel) {
    s.value.DEFAULT_BOT_ID = defaultChannel.id
  } else if (builtinChannel) {
    s.value.DEFAULT_BOT_ID = 'default'
  }
}

async function save() {
  saving.value = true
  try {
    // 保存前同步通知渠道到旧Bot配置，保持后端兼容
    syncChannelsToOldBots()

    const r = await api.saveSettings(s.value)
    const needRestart = !!r.restart_required
    // 静默重载：同步服务端清洗后的值（如剔除畸形 Bot）并重置基线快照
    await load(true)
    restartHint.value = needRestart
    const failedBots = r.bot_sync?.failed || []
    if (failedBots.length) {
      toast.error(`已保存，但这些 Bot 连接失败：${failedBots.map((bot) => bot.name).join('、')}`)
    } else {
      toast.success(needRestart
        ? '已保存。基础凭据、代理或数据库等改动需重启平台生效。'
        : (r.bot_sync ? '已保存，Bot 设置已立即生效。' : '已保存。'))
    }
    // Bot 列表可能变化 → 刷新推送路由的可选项
    if (tab.value === 'bots') await loadRouting()
    return true
  } catch (e) {
    toast.error('保存失败：' + e.message)
    return false
  } finally { saving.value = false }
}

async function doRestart() {
  restarting.value = true
  try {
    await api.restartPlatform()
    toast.success('平台正在重启，十几秒后自动刷新')
    restartHint.value = false
    let tries = 0
    if (restartTimer) clearInterval(restartTimer)
    restartTimer = setInterval(async () => {
      tries++
      try { await api.status(); clearInterval(restartTimer); restartTimer = null; location.reload() }
      catch { if (tries > 30) { clearInterval(restartTimer); restartTimer = null; restarting.value = false } }
    }, 2000)
  } catch (e) { toast.error('重启请求失败：' + e.message); restarting.value = false }
}

// ── 连接测试（代理 / 数据库）──
const proxyTest = ref(null)     // { ok, message } | null
const proxyTesting = ref(false)
async function testProxy() {
  proxyTesting.value = true; proxyTest.value = null
  try { proxyTest.value = await api.testProxy(s.value.proxy_set) }
  catch (e) { proxyTest.value = { ok: false, message: e.message } }
  finally { proxyTesting.value = false }
}
const dbTest = ref(null)
const dbTesting = ref(false)
async function testDb() {
  dbTesting.value = true; dbTest.value = null
  try { dbTest.value = await api.testDb(s.value.DB_INFO) }
  catch (e) { dbTest.value = { ok: false, message: e.message } }
  finally { dbTesting.value = false }
}

// ── 备份 / 恢复 ──
const backupBusy = ref(false)
const restoreBusy = ref(false)
const restoreInput = ref(null)

function openRestorePicker() {
  restoreInput.value?.click()
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

async function downloadBackup() {
  backupBusy.value = true
  try {
    const { blob, filename } = await api.downloadBackup()
    saveBlob(blob, filename)
    toast.success('备份包已开始下载')
  } catch (e) {
    toast.error('导出备份失败：' + e.message)
  } finally {
    backupBusy.value = false
  }
}

async function onRestoreFile(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  const ok = await confirm({
    title: '导入恢复',
    message: '恢复会覆盖平台中的 data、sessions、db_file、plugins 目录内容。建议先确认备份包来源可信。继续恢复？',
    confirmText: '继续恢复',
    danger: true,
  })
  if (!ok) return

  restoreBusy.value = true
  try {
    const r = await api.restoreBackup(file)
    restartHint.value = !!r.restart_required
    if (r.pre_restore_backup) {
      try {
        const snapshot = await api.downloadStoredBackup(r.pre_restore_backup)
        saveBlob(snapshot.blob, snapshot.filename)
      } catch (downloadError) {
        toast.error('恢复包已暂存，但恢复前快照下载失败：' + downloadError.message)
      }
    }
    toast.success(`备份已校验，共 ${r.staged_files || 0} 个文件；重启平台后应用恢复`)
  } catch (err) {
    toast.error('恢复失败：' + err.message)
  } finally {
    restoreBusy.value = false
  }
}

// ── 日志清理 ──
const cleaningLogs = ref(false)

async function cleanLogsNow() {
  const ok = await confirm({
    title: '立即清理日志',
    message: `将清理平台日志和所有插件日志，每个日志保留最近 ${s.value.LOG_CLEANER.keep_lines} 条。确认执行？`,
    confirmText: '确认清理',
  })
  if (!ok) return

  cleaningLogs.value = true
  try {
    await api.cleanLogs()
    toast.success('日志已清理完成')
  } catch (err) {
    toast.error('清理日志失败：' + err.message)
  } finally {
    cleaningLogs.value = false
  }
}

// ── 多 Bot（额外 Bot 增删） ──
function addBot() {
  s.value.BOTS.push({ id: 'bot_' + Date.now().toString(36), name: '', token: '', chat_id: '' })
}
function removeBot(i) {
  const removed = s.value.BOTS[i]
  if (removed?.id === s.value.DEFAULT_BOT_ID) s.value.DEFAULT_BOT_ID = 'default'
  s.value.BOTS.splice(i, 1)
}
function setDefaultBot(id) { s.value.DEFAULT_BOT_ID = id || 'default' }

function configuredDefaultBotName() {
  if (s.value?.DEFAULT_BOT_ID === 'default') return s.value.BOT_NAME || '主要通知渠道'
  return s.value?.BOTS?.find((bot) => bot.id === s.value.DEFAULT_BOT_ID)?.name || '主要通知渠道'
}

// ── 平台 Webhook（密钥 + 随机按钮 + 地址展示） ──
function randomHex(bytesLen = 24) {
  const bytes = new Uint8Array(bytesLen)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}
function genWebhookSecret() { s.value.WEBHOOK_SECRET = randomHex(24) }
function genApiKey() { s.value.API_KEY = randomHex(32) }
const platformWebhookUrl = computed(() => {
  if (!s.value?.WEBHOOK_SECRET) return ''
  return `${location.origin}/api/v1/webhook?apikey=${s.value.WEBHOOK_SECRET}`
})
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* 落到降级方案 */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus(); ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch { return false }
}
async function copyPlatformWebhook() {
  if (!platformWebhookUrl.value) return
  if (await copyText(platformWebhookUrl.value)) toast.success('已复制平台 webhook 地址')
  else toast.error('复制失败，请手动选择复制')
}
async function copyApiKey() {
  if (!s.value?.API_KEY) return
  if (await copyText(s.value.API_KEY)) toast.success('已复制 API Key')
  else toast.error('复制失败，请手动选择复制')
}

// ── 通知渠道管理 ──
const channelModalOpen = ref(false)
const channelModalMode = ref('add')  // 'add' | 'edit'
const channelEditIndex = ref(-1)
const channelForm = ref({
  id: '',
  name: '',
  type: 'telegram',  // telegram | wechat | bark
  enabled: true,
  config: {}
})

const channelTypes = [
  { value: 'telegram', label: 'Telegram', icon: 'telegram' },
  { value: 'wechat', label: '企业微信', icon: 'wechat' },
  { value: 'bark', label: 'Bark', icon: 'bark' }
]

// 添加渠道下拉菜单
const addMenuOpen = ref(false)

function toggleAddMenu() {
  addMenuOpen.value = !addMenuOpen.value
}

async function selectChannelType(type) {
  addMenuOpen.value = false
  await loadRouting()
  channelModalMode.value = 'add'
  channelEditIndex.value = -1
  channelForm.value = {
    id: `ch_${Date.now()}`,
    name: '',
    type: type,
    enabled: true,
    is_default: false,
    config: {},
    plugins: []  // 新增：选择的插件列表
  }
  channelModalOpen.value = true
}

async function openAddChannel() {
  await loadRouting()
  channelModalMode.value = 'add'
  channelEditIndex.value = -1
  channelForm.value = {
    id: `ch_${Date.now()}`,
    name: '',
    type: 'telegram',
    enabled: true,
    is_default: false,
    config: {},
    plugins: []  // 新增：选择的插件列表
  }
  channelModalOpen.value = true
}

function routedPluginsForChannel(channelId) {
  return (routing.value.plugins || [])
    .filter(plugin => (plugin.bot || '').split(',').map(id => id.trim()).includes(channelId))
    .map(plugin => plugin.id)
}

async function openEditChannel(index) {
  await loadRouting()
  channelModalMode.value = 'edit'
  channelEditIndex.value = index
  const ch = s.value.NOTIFICATION_CHANNELS[index]
  channelForm.value = JSON.parse(JSON.stringify(ch))
  // 确保字段存在
  if (channelForm.value.is_default === undefined) {
    channelForm.value.is_default = false
  }
  // 插件路由是唯一数据源，不读取渠道配置里可能残留的旧副本。
  channelForm.value.plugins = routedPluginsForChannel(ch.id)
  channelModalOpen.value = true
}

async function saveChannel() {
  if (!channelForm.value.name.trim()) {
    toast.error('请输入名称')
    return
  }

  const originalSettings = JSON.parse(JSON.stringify(s.value))

  // 如果设为默认，取消其他渠道的默认状态
  if (channelForm.value.is_default) {
    s.value.NOTIFICATION_CHANNELS.forEach((ch, idx) => {
      if (channelModalMode.value === 'edit' && idx === channelEditIndex.value) return
      ch.is_default = false
    })
  }

  const routeSelection = [...(channelForm.value.plugins || [])]
  const channel = JSON.parse(JSON.stringify(channelForm.value))
  delete channel.plugins
  if (!channel.enabled) channel.is_default = false

  if (channelModalMode.value === 'add') {
    s.value.NOTIFICATION_CHANNELS.push(channel)
  } else {
    s.value.NOTIFICATION_CHANNELS[channelEditIndex.value] = channel
  }

  // 先关闭弹窗，让用户不用等待 Bot 连接和路由同步；失败时恢复并重新打开。
  channelModalOpen.value = false
  if (!await save()) {
    s.value = originalSettings
    channelModalOpen.value = true
    return
  }
  try {
    if (channel.enabled) {
      await syncChannelToRouting({ ...channel, plugins: routeSelection })
    }
    await loadRouting()
    publishNotificationSync({ source: notificationSyncSource, type: 'channels', channelId: channel.id })
  } catch (e) {
    await loadRouting()
    toast.error('渠道已保存，但插件路由更新失败：' + e.message)
  }
}

async function deleteChannel(index) {
  const channelName = s.value.NOTIFICATION_CHANNELS[index].name
  const ok = await confirm({
    title: '删除通知渠道',
    message: `确定删除通知渠道「${channelName}」？`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return

  const originalChannels = JSON.parse(JSON.stringify(s.value.NOTIFICATION_CHANNELS))
  s.value.NOTIFICATION_CHANNELS.splice(index, 1)
  if (await save()) {
    await loadRouting()
    publishNotificationSync({ source: notificationSyncSource, type: 'channels' })
    toast.success('已删除，相关插件路由已同步更新')
  } else {
    s.value.NOTIFICATION_CHANNELS = originalChannels
  }
}

async function toggleChannel(index) {
  const originalChannels = JSON.parse(JSON.stringify(s.value.NOTIFICATION_CHANNELS))
  s.value.NOTIFICATION_CHANNELS[index].enabled = !s.value.NOTIFICATION_CHANNELS[index].enabled
  const ch = s.value.NOTIFICATION_CHANNELS[index]

  if (!ch.enabled) {
    ch.is_default = false
  }

  if (await save()) {
    await loadRouting()
    publishNotificationSync({ source: notificationSyncSource, type: 'channels', channelId: ch.id })
  } else s.value.NOTIFICATION_CHANNELS = originalChannels
}

function getChannelIcon(type) {
  return type || 'telegram'
}

function getChannelTypeName(type) {
  const found = channelTypes.find(t => t.value === type)
  return found ? found.label : type
}

// 根据插件标签获取可用渠道（机器人/双账号插件只能选Telegram）
function getAvailableChannels(plugin) {
  const allChannels = (s.value.NOTIFICATION_CHANNELS || []).filter(ch => ch.enabled)

  // 检查插件是否有特殊标签
  const requiresTelegramBot = plugin.scope === 'bot' || plugin.scope === 'both'

  // 如果插件需要Bot功能，只返回Telegram渠道
  if (requiresTelegramBot) {
    return allChannels.filter(ch => ch.type === 'telegram')
  }

  // 其他插件可以使用所有渠道
  return allChannels
}

// 获取默认渠道名称
function getDefaultChannelName() {
  const defaultCh = (s.value.NOTIFICATION_CHANNELS || []).find(ch => ch.is_default)
  return defaultCh ? defaultCh.name : '未设置'
}

// 获取渠道可用的插件列表（根据渠道类型过滤）
function getAvailablePluginsForChannel(channelType) {
  const allPlugins = routing.value.plugins || []

  // 如果是企业微信或Bark，排除需要Bot功能的插件
  if (channelType === 'wechat' || channelType === 'bark') {
    return allPlugins.filter(p => p.scope !== 'bot' && p.scope !== 'both')
  }

  // Telegram支持所有插件
  return allPlugins
}

// 检查是否选择了"全部"（所有可用插件都被选中）
function isAllPluginsSelected(channel) {
  if (!Array.isArray(channel.plugins)) return false
  const available = getAvailablePluginsForChannel(channel.type)
  if (available.length === 0) return false
  return available.every(p => channel.plugins.includes(p.id))
}

// 切换"全部"选择
function toggleAllPlugins(channel) {
  const available = getAvailablePluginsForChannel(channel.type)
  if (isAllPluginsSelected(channel)) {
    // 取消全选
    channel.plugins = []
  } else {
    // 全选
    channel.plugins = available.map(p => p.id)
  }
}

// 切换单个插件选择
function togglePlugin(channel, pluginId) {
  if (!Array.isArray(channel.plugins)) {
    channel.plugins = []
  }

  const index = channel.plugins.indexOf(pluginId)
  if (index > -1) {
    channel.plugins.splice(index, 1)
  } else {
    channel.plugins.push(pluginId)
  }
}

// 双向同步：渠道配置 → 推送路由
async function syncChannelToRouting(channel) {
  const channelId = channel.id
  const selectedPlugins = channel.plugins || []
  const saves = []

  routing.value.plugins.forEach(plugin => {
    const currentChannels = (plugin.bot || '').split(',').map(id => id.trim()).filter(Boolean)
    const shouldInclude = selectedPlugins.includes(plugin.id)

    if (shouldInclude && !currentChannels.includes(channelId)) {
      currentChannels.push(channelId)
    } else if (!shouldInclude && currentChannels.includes(channelId)) {
      currentChannels.splice(currentChannels.indexOf(channelId), 1)
    } else {
      return  // 无变化，跳过
    }

    plugin.bot = currentChannels.join(',')
    // 收集所有保存请求，统一 await
    saves.push(api.setBotRouting(plugin.id, plugin.bot))
  })

  // 等所有路由都保存完再返回
  if (saves.length) await Promise.all(saves)
}

// ── 通知推送路由（哪个插件推到哪个 Bot） ──
const routing = ref({ bots: [], plugins: [] })
const routingLoading = ref(false)
const routeSaving = ref({})

async function loadRouting() {
  routingLoading.value = true
  try { routing.value = await api.getBotsRouting() }
  catch (e) { toast.error('加载推送路由失败：' + e.message) }
  finally { routingLoading.value = false }
}

// 检查插件是否选中了某个渠道
function isChannelSelected(plugin, channelId) {
  // p.bot 可能是单个ID字符串，或逗号分隔的多个ID
  const selected = plugin.bot || ''
  if (!selected) return false
  const ids = selected.split(',').map(id => id.trim()).filter(Boolean)
  return ids.includes(channelId)
}

// 切换插件的渠道选择（多选）
async function togglePluginChannel(plugin, channelId) {
  if (routeSaving.value[plugin.id]) return
  routeSaving.value[plugin.id] = true
  const selected = plugin.bot || ''
  let ids = selected.split(',').map(id => id.trim()).filter(Boolean)

  if (ids.includes(channelId)) {
    // 取消选择
    ids = ids.filter(id => id !== channelId)
  } else {
    // 添加选择
    ids.push(channelId)
  }

  // 更新插件的bot字段
  plugin.bot = ids.join(',')

  // 保存到后端
  try {
    await api.setBotRouting(plugin.id, plugin.bot)
    await loadRouting()
    publishNotificationSync({ source: notificationSyncSource, type: 'routing', pluginId: plugin.id })
    const channelNames = ids.map(id => {
      const ch = (s.value.NOTIFICATION_CHANNELS || []).find(c => c.id === id)
      return ch ? ch.name : id
    }).join('、')
    toast.success(`「${plugin.name}」→ ${channelNames || '默认'}`)
  } catch (e) {
    toast.error('保存失败：' + e.message)
    await loadRouting()
  } finally { routeSaving.value[plugin.id] = false }
}

// Bot 在线状态/用户名：取自 routing.bots（后端 list_bots，含 online/username）。
// 新加的额外 Bot 尚未保存重启，查不到状态，返回 null（UI 显示「未连接」）。
function botStatus(id) {
  return (routing.value.bots || []).find((b) => b.id === id) || null
}

// 推送路由搜索：按插件名/id 过滤
const routeSearch = ref('')
const filteredRoutePlugins = computed(() => {
  const q = routeSearch.value.trim().toLowerCase()
  const list = routing.value.plugins || []
  if (!q) return list
  return list.filter((p) => (p.name || '').toLowerCase().includes(q) || (p.id || '').toLowerCase().includes(q))
})

function goTab(k) {
  tab.value = k
  if (k === 'bots' && routing.value.plugins.length === 0) loadRouting()
  if (k === 'ai' && !ai.value && !aiLoading.value) loadAiSettings()
}

// ── 登录凭据修改 ──
const cred = ref({ old_password: '', new_username: '', new_password: '' })
const credBusy = ref(false)
const credMsg = ref('')
const credErr = ref('')

async function loadUsername() {
  try { const st = await api.authStatus(); cred.value.new_username = st.username || '' } catch {}
}

async function saveCred() {
  credBusy.value = true; credMsg.value = ''; credErr.value = ''
  if (!cred.value.old_password) { credErr.value = '请输入当前密码'; credBusy.value = false; return }
  try {
    await api.changeCredentials(cred.value.old_password, cred.value.new_username, cred.value.new_password)
    credMsg.value = '登录凭据已更新。下次登录用新账号密码。'
    cred.value.old_password = ''; cred.value.new_password = ''
  } catch (e) { credErr.value = e.message } finally { credBusy.value = false }
}

async function refreshNotificationSync(change) {
  if (change.source === notificationSyncSource) return
  if (change.type === 'channels') {
    if (!dirty.value) {
      await load(true)
    } else {
      // 只合并外部更新的通知字段，不覆盖本页其他尚未保存的设置。
      const data = await api.getSettings()
      const latest = data.settings || {}
      for (const key of ['NOTIFICATION_CHANNELS', 'BOT_TOKEN', 'BOT_NAME', 'BOTS', 'DEFAULT_BOT_ID', 'DEFAULT_BOT_CHAT_ID']) {
        s.value[key] = latest[key]
      }
    }
  }
  await loadRouting()
  if (channelModalOpen.value && channelModalMode.value === 'edit') {
    channelForm.value.plugins = routedPluginsForChannel(channelForm.value.id)
  }
}

onMounted(() => {
  load(); loadUsername()
  stopNotificationSync = subscribeNotificationSync(refreshNotificationSync)
})
onUnmounted(() => {
  stopNotificationSync?.()
  if (restartTimer) { clearInterval(restartTimer); restartTimer = null }
})

// 点击外部关闭添加渠道下拉菜单
function handleClickOutside(e) {
  if (addMenuOpen.value) {
    const dropdown = document.querySelector('.add-channel-dropdown')
    if (dropdown && !dropdown.contains(e.target)) {
      addMenuOpen.value = false
    }
  }
}
onMounted(() => document.addEventListener('click', handleClickOutside))
onUnmounted(() => document.removeEventListener('click', handleClickOutside))

// 未保存改动保护：刷新/关页 + 站内切换路由时提醒
function beforeUnload(e) { if (anyDirty.value) { e.preventDefault(); e.returnValue = '' } }
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onUnmounted(() => window.removeEventListener('beforeunload', beforeUnload))
onBeforeRouteLeave(async () => {
  if (!anyDirty.value) return true
  return await confirm({
    title: '离开系统设置',
    message: '有未保存的改动，离开将丢失。确定离开？',
    confirmText: '离开', danger: true,
  })
})
</script>

<template>
  <div class="settings-page">
    <!-- 顶部 Tab 切换：一个分类一个菜单 -->
    <div class="toolbar">
      <div class="tabs">
        <button v-for="t in TABS" :key="t.key" class="tab" :class="{ active: tab === t.key }"
                @click="goTab(t.key)">{{ t.label }}</button>
      </div>
      <div class="row gap" v-if="s && tab !== 'login'">
        <button class="btn" @click="undoCurrent" :disabled="!currentDirty" title="撤销未保存的改动，从服务器重新加载">撤销更改</button>
        <button class="btn btn-primary" @click="saveCurrent" :disabled="currentSaving || !currentDirty">
          <span v-if="currentDirty" class="dirty-dot"></span>{{ currentSaving ? '保存中…' : (currentDirty ? '保存设置' : '已保存') }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="muted center">加载中…</div>
    <div v-else-if="s" class="panel">
      <div v-if="err" class="alert err">{{ err }}</div>
      <!-- 保存后需重启：给一键重启入口 -->
      <div v-if="restartHint" class="restart-banner">
        <span>部分改动需重启平台才生效。</span>
        <div class="row gap">
          <button class="btn sm" @click="restartHint = false">稍后</button>
          <button class="btn sm btn-primary" @click="doRestart" :disabled="restarting">
            {{ restarting ? '重启中…' : '立即重启' }}
          </button>
        </div>
      </div>

      <!-- 控制台登录 -->
      <div v-show="tab === 'login'" class="card">
        <div class="card-title">控制台登录</div>
        <div class="hint muted">修改登录本控制台的用户名和密码（默认 admin / password）。</div>
        <div v-if="credErr" class="alert err">{{ credErr }}</div>
        <div v-if="credMsg" class="alert ok">{{ credMsg }}</div>
        <div class="grid2">
          <div class="field"><label>用户名</label>
            <input class="input" v-model="cred.new_username" placeholder="登录用户名" /></div>
          <div class="field"><label>当前密码（验证身份）</label>
            <input class="input" type="password" v-model="cred.old_password" placeholder="输入当前密码" /></div>
          <div class="field"><label>新密码（不改留空）</label>
            <input class="input" type="password" v-model="cred.new_password" placeholder="至少 4 位" /></div>
        </div>
        <div class="actions">
          <button class="btn btn-primary" @click="saveCred" :disabled="credBusy">
            {{ credBusy ? '保存中…' : '更新登录凭据' }}
          </button>
        </div>
      </div>

      <!-- Telegram 凭据 -->
      <div v-show="tab === 'login'" class="card" style="margin-top:16px">
        <div class="card-title">Telegram 凭据</div>
        <div class="hint muted">从 my.telegram.org 获取 API_ID / API_HASH。Bot Token 在「通知推送」页配置。敏感值显示为打码，不改就留着。</div>
        <div class="grid2">
          <div class="field"><label>API ID</label>
            <input class="input" type="number" v-model.number="s.API_ID" /></div>
          <div class="field"><label>API HASH</label>
            <input class="input" v-model="s.API_HASH" /></div>
        </div>
      </div>

      <!-- 通知渠道 -->
      <div v-show="tab === 'notify'" class="card">
        <div class="card-title">通知渠道</div>
        <div class="hint muted small" style="margin-bottom:16px">
          设置消息发送渠道参数
        </div>

        <!-- 通知渠道卡片网格 -->
        <div class="channel-grid-mp">
          <div v-for="(ch, idx) in s.NOTIFICATION_CHANNELS" :key="ch.id" class="channel-card-mp">
            <!-- 顶部行：状态灯+名称+默认标识+删除按钮 -->
            <div class="channel-top-row">
              <div class="channel-left-info">
                <span class="status-dot-mp" :class="{ on: ch.enabled }"></span>
                <span class="channel-name-text">{{ ch.name }}</span>
                <span v-if="ch.is_default" class="badge-default-mp">默认</span>
              </div>
              <button class="delete-btn-mp" @click="deleteChannel(idx)" title="删除">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M18 6 6 18M6 6l12 12"/>
                </svg>
              </button>
            </div>

            <!-- 底部行：类型标签+图标 -->
            <div class="channel-bottom-row" @click="openEditChannel(idx)">
              <div class="channel-type-label">{{ getChannelTypeName(ch.type) }}</div>
              <div class="channel-icon-mp">
                <!-- Telegram 图标 -->
                <svg v-if="ch.type === 'telegram'" viewBox="0 0 24 24" fill="currentColor" class="icon-telegram">
                  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/>
                </svg>
                <!-- 企业微信图标 -->
                <svg v-else-if="ch.type === 'wechat'" viewBox="0 0 24 24" fill="currentColor" class="icon-wechat">
                  <path d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 0 1 .213.665l-.39 1.48c-.019.07-.048.141-.048.213 0 .163.13.295.29.295a.326.326 0 0 0 .167-.054l1.903-1.114a.864.864 0 0 1 .717-.098 10.16 10.16 0 0 0 2.837.403c.276 0 .543-.027.811-.05-.857-2.578.157-4.972 1.932-6.446 1.703-1.415 3.882-1.98 5.853-1.838-.576-3.583-4.196-6.348-8.596-6.348zM5.785 5.991c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178A1.17 1.17 0 0 1 4.623 7.17c0-.651.52-1.18 1.162-1.18zm5.813 0c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178 1.17 1.17 0 0 1-1.162-1.178c0-.651.52-1.18 1.162-1.18zm5.34 2.867c-1.797-.052-3.746.512-5.28 1.786-1.72 1.428-2.687 3.72-1.78 6.22.942 2.453 3.666 4.229 6.884 4.229.826 0 1.622-.12 2.361-.336a.722.722 0 0 1 .598.082l1.584.926a.272.272 0 0 0 .14.045c.134 0 .24-.111.24-.247 0-.06-.023-.12-.038-.177l-.327-1.233a.582.582 0 0 1-.023-.156.49.49 0 0 1 .201-.398C23.024 18.48 24 16.82 24 14.98c0-3.21-2.931-5.837-6.656-6.088V8.89c-.135-.01-.27-.027-.407-.03zm-2.53 3.274c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982zm4.844 0c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982z"/>
                </svg>
                <!-- Bark 图标 -->
                <svg v-else-if="ch.type === 'bark'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="icon-bark">
                  <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                  <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
                </svg>
              </div>
            </div>
          </div>
        </div>

        <!-- 添加按钮（带下拉菜单） -->
        <div class="channel-footer" style="margin-top:16px">
          <div class="add-channel-dropdown">
            <button class="btn-add-mp" @click="toggleAddMenu">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 5v14m-7-7h14"/>
              </svg>
            </button>
            <!-- 下拉菜单 -->
            <div v-if="addMenuOpen" class="add-menu-dropdown">
              <button @click="selectChannelType('telegram')">
                <svg viewBox="0 0 24 24" fill="currentColor" class="icon-telegram">
                  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/>
                </svg>
                Telegram
              </button>
              <button @click="selectChannelType('wechat')">
                <svg viewBox="0 0 24 24" fill="currentColor" class="icon-wechat">
                  <path d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 0 1 .213.665l-.39 1.48c-.019.07-.048.141-.048.213 0 .163.13.295.29.295a.326.326 0 0 0 .167-.054l1.903-1.114a.864.864 0 0 1 .717-.098 10.16 10.16 0 0 0 2.837.403c.276 0 .543-.027.811-.05-.857-2.578.157-4.972 1.932-6.446 1.703-1.415 3.882-1.98 5.853-1.838-.576-3.583-4.196-6.348-8.596-6.348zM5.785 5.991c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178A1.17 1.17 0 0 1 4.623 7.17c0-.651.52-1.18 1.162-1.18zm5.813 0c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178 1.17 1.17 0 0 1-1.162-1.178c0-.651.52-1.18 1.162-1.18zm5.34 2.867c-1.797-.052-3.746.512-5.28 1.786-1.72 1.428-2.687 3.72-1.78 6.22.942 2.453 3.666 4.229 6.884 4.229.826 0 1.622-.12 2.361-.336a.722.722 0 0 1 .598.082l1.584.926a.272.272 0 0 0 .14.045c.134 0 .24-.111.24-.247 0-.06-.023-.12-.038-.177l-.327-1.233a.582.582 0 0 1-.023-.156.49.49 0 0 1 .201-.398C23.024 18.48 24 16.82 24 14.98c0-3.21-2.931-5.837-6.656-6.088V8.89c-.135-.01-.27-.027-.407-.03zm-2.53 3.274c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982zm4.844 0c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982z"/>
                </svg>
                企业微信
              </button>
              <button @click="selectChannelType('bark')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="icon-bark">
                  <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                  <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
                </svg>
                Bark
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- 推送路由 -->
      <div v-show="tab === 'notify'" class="card" style="margin-top:16px">
        <div class="card-title">推送路由</div>
        <div class="hint muted small" style="margin-bottom:12px">
          选择每个插件的通知发到哪个渠道；选择立即生效（无需保存设置）。未单独选择时使用当前默认渠道。
        </div>
        <div v-if="routingLoading" class="muted small">加载中…</div>
        <div v-else-if="routing.plugins.length === 0" class="muted small">还没有插件。</div>
        <template v-else>
          <input class="input route-search" v-model="routeSearch" placeholder="搜索插件名称 / id…" style="margin-bottom:12px" />
          <div v-if="filteredRoutePlugins.length === 0" class="muted small">没有匹配的插件。</div>
          <div v-else class="route-table-multi">
            <div v-for="p in filteredRoutePlugins" :key="p.id" class="route-row-multi">
              <div class="route-plugin-info">
                <span class="route-name" :title="p.id">{{ p.name }}</span>
                <span v-if="(p.tags || []).includes('机器人') || (p.tags || []).includes('双账号')" class="route-tag">需要Bot</span>
              </div>
              <div class="route-channels">
                <label v-for="ch in getAvailableChannels(p)" :key="ch.id" class="channel-checkbox">
                  <input
                    type="checkbox"
                    :checked="isChannelSelected(p, ch.id)"
                    @change="togglePluginChannel(p, ch.id)"
                    :disabled="routeSaving[p.id]"
                  />
                  <span class="channel-label">
                    {{ ch.name }} <span class="channel-type-badge">{{ getChannelTypeName(ch.type) }}</span>
                  </span>
                </label>
                <span v-if="getAvailableChannels(p).length === 0" class="muted small">无可用渠道</span>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 平台 AI 服务 -->
      <template v-if="tab === 'ai'">
        <div v-if="aiLoading" class="card center muted">正在读取 AI 设置…</div>
        <template v-else-if="ai">
          <div class="card ai-overview">
            <div class="ai-overview-main">
              <div class="ai-mark">AI</div>
              <div>
                <div class="card-title">平台 AI 服务</div>
                <div class="hint muted">
                  插件统一通过平台调用文字、图片识别和生图模型，不会接触服务商密钥。
                </div>
              </div>
            </div>
            <button type="button" class="toggle" :class="{ on: ai.enabled }"
                    :aria-pressed="ai.enabled" aria-label="启用平台 AI"
                    @click="ai.enabled = !ai.enabled"></button>
            <div v-if="aiStatus" class="ai-status-strip">
              <span><b>{{ aiStatus.total || 0 }}</b> 次调用</span>
              <span class="ok"><b>{{ aiStatus.succeeded || 0 }}</b> 成功</span>
              <span :class="{ bad: aiStatus.failed }"><b>{{ aiStatus.failed || 0 }}</b> 失败</span>
              <span><b>{{ aiStatus.active || 0 }}</b> 进行中</span>
            </div>
          </div>

          <div class="card" style="margin-top:16px">
            <div class="row between ai-section-head">
              <div>
                <div class="card-title">AI 服务商</div>
                <div class="hint muted">支持 OpenAI 兼容接口，可添加云端服务或本地模型服务。</div>
              </div>
              <button class="btn sm btn-primary" @click="addAiProvider">+ 添加服务</button>
            </div>
            <div class="ai-provider-grid">
              <div v-for="(provider, index) in ai.providers" :key="provider.id" class="ai-provider-card">
                <div class="row between">
                  <label class="ai-provider-enable">
                    <input type="checkbox" v-model="provider.enabled" />
                    <span>{{ provider.enabled ? '已启用' : '已停用' }}</span>
                  </label>
                  <button class="ai-remove" @click="removeAiProvider(index)" title="删除服务">×</button>
                </div>
                <div class="field">
                  <label>显示名称</label>
                  <input class="input" v-model="provider.name" placeholder="例如：主 AI 服务" />
                </div>
                <div class="field">
                  <label>服务地址</label>
                  <input class="input mono" v-model="provider.base_url"
                         placeholder="https://api.openai.com/v1" />
                </div>
                <div class="field">
                  <label>API Key</label>
                  <input class="input" type="password" v-model="provider.api_key"
                         placeholder="本地服务不需要时可留空" />
                </div>
                <button class="btn sm" @click="fetchAiModels(provider)"
                        :disabled="aiModelLoading[provider.id]">
                  {{ aiModelLoading[provider.id] ? '读取中…' : '读取模型列表' }}
                </button>
                <span v-if="aiProviderModels(provider.id).length" class="muted small">
                  已读取 {{ aiProviderModels(provider.id).length }} 个模型
                </span>
              </div>
            </div>
          </div>

          <div class="card" style="margin-top:16px">
            <div class="row between ai-section-head">
              <div>
                <div class="card-title">模型库</div>
                <div class="hint muted">
                  从服务商列表选择或手动填写真实模型名，并设置插件调用时使用的别名。
                </div>
              </div>
              <button class="btn sm btn-primary" @click="addAiModel">+ 添加模型</button>
            </div>
            <div v-if="ai.models.length === 0" class="muted center ai-model-empty">
              还没有模型，请先添加一个模型。
            </div>
            <div v-else class="ai-model-grid">
              <div v-for="(model, index) in ai.models" :key="model.id" class="ai-model-card">
                <div class="row between">
                  <label class="ai-provider-enable">
                    <input type="checkbox" v-model="model.enabled" />
                    <span>{{ model.enabled ? '已启用' : '已停用' }}</span>
                  </label>
                  <button class="ai-remove" @click="removeAiModel(index)" title="删除模型">×</button>
                </div>
                <div class="grid2">
                  <div class="field">
                    <label>显示名称</label>
                    <input class="input" v-model="model.name" placeholder="例如：快速文字模型" />
                  </div>
                  <div class="field">
                    <label>插件调用别名</label>
                    <input class="input mono" v-model="model.alias" placeholder="例如：fast" />
                  </div>
                </div>
                <div class="grid2">
                  <div class="field">
                    <label>所属服务</label>
                    <select class="select" v-model="model.provider_id">
                      <option v-for="provider in ai.providers" :key="provider.id" :value="provider.id">
                        {{ provider.name }}
                      </option>
                    </select>
                  </div>
                  <div class="field">
                    <label>真实模型名</label>
                    <input class="input mono" v-model="model.model"
                           :list="`provider-models-${model.id}`"
                           placeholder="从列表选择或手动填写" />
                    <datalist :id="`provider-models-${model.id}`">
                      <option v-for="item in aiProviderModels(model.provider_id)"
                              :key="item" :value="item"></option>
                    </datalist>
                  </div>
                </div>
                <div class="ai-model-capabilities">
                  <span class="muted small">模型能力</span>
                  <label v-for="capability in AI_CAPABILITIES" :key="capability.key">
                    <input type="checkbox"
                           :checked="model.capabilities.includes(capability.key)"
                           @change="toggleAiModelCapability(model, capability.key)" />
                    <span>{{ capability.label }}</span>
                  </label>
                </div>
              </div>
            </div>
          </div>

          <div class="card" style="margin-top:16px">
            <div class="card-title">模型分工</div>
            <div class="hint muted">
              不指定模型的插件使用这里的默认模型；插件也可以通过别名自主选择模型库中的其他模型。
            </div>
            <div class="ai-capability-grid">
              <div v-for="capability in AI_CAPABILITIES" :key="capability.key" class="ai-capability-card">
                <div class="ai-capability-title">
                  <span>{{ capability.label }}</span>
                  <button class="btn sm" @click="testAi(capability.key)"
                          :disabled="aiTesting[capability.key] || !ai.enabled">
                    {{ aiTesting[capability.key] ? '测试中…' : '测试' }}
                  </button>
                </div>
                <div class="muted small">{{ capability.desc }}</div>
                <div class="field">
                  <label>默认模型</label>
                  <select class="select" v-model="ai.capabilities[capability.key].default_model">
                    <option value="">未设置</option>
                    <option v-for="model in availableAiModels(capability.key)"
                            :key="model.id" :value="model.id">
                      {{ model.name }}（{{ model.alias }}）
                    </option>
                  </select>
                </div>
                <div class="field">
                  <label>备用模型（可空）</label>
                  <select class="select" v-model="ai.capabilities[capability.key].fallback_model">
                    <option value="">不使用备用模型</option>
                    <option v-for="model in availableAiModels(capability.key)"
                            :key="model.id" :value="model.id"
                            :disabled="model.id === ai.capabilities[capability.key].default_model">
                      {{ model.name }}（{{ model.alias }}）
                    </option>
                  </select>
                </div>
              </div>
            </div>
            <div class="hint muted small ai-test-note">
              生图测试会真正生成一张简单图片，可能产生少量服务商费用。
            </div>
          </div>

          <div class="card" style="margin-top:16px">
            <div class="card-title">调用保护</div>
            <div class="grid2">
              <div class="field">
                <label>请求超时（秒）</label>
                <input class="input" type="number" min="5" max="300"
                       v-model.number="ai.timeout_seconds" />
              </div>
              <div class="field">
                <label>最多同时调用</label>
                <input class="input" type="number" min="1" max="20"
                       v-model.number="ai.max_concurrency" />
              </div>
            </div>
            <div class="hint muted">超过并发数量的请求会自动排队，避免多个插件同时调用拖慢平台。</div>
          </div>

          <div class="card" style="margin-top:16px">
            <div class="card-title">插件 AI 权限</div>
            <div class="hint muted">默认允许插件使用全部能力，也可以在这里单独关闭。</div>
            <div v-if="aiPlugins.length === 0" class="muted center" style="padding:20px">暂无已安装插件</div>
            <div v-else class="ai-permission-list">
              <div v-for="plugin in aiPlugins" :key="plugin.id" class="ai-permission-row">
                <div class="ai-plugin-name">
                  <strong>{{ plugin.name }}</strong>
                  <span class="mono muted small">{{ plugin.id }}</span>
                </div>
                <label class="ai-permission-switch">
                  <input type="checkbox" :checked="pluginAiEnabled(plugin.id)"
                         @change="togglePluginAi(plugin.id)" />
                  <span>允许 AI</span>
                </label>
                <label v-for="capability in AI_CAPABILITIES" :key="capability.key"
                       class="ai-permission-cap" :class="{ disabled: !pluginAiEnabled(plugin.id) }">
                  <input type="checkbox"
                         :checked="pluginAiCapability(plugin.id, capability.key)"
                         :disabled="!pluginAiEnabled(plugin.id)"
                         @change="togglePluginAiCapability(plugin.id, capability.key)" />
                  <span>{{ capability.label }}</span>
                </label>
              </div>
            </div>
          </div>
        </template>
      </template>

      <!-- 平台 Webhook -->
      <div v-show="tab === 'api'" class="card">
        <div class="card-title">Webhook 入站</div>
        <div class="hint muted small" style="margin-bottom:12px">
          外部服务 POST 到下面的地址（JSON 可含 text/title/category 字段，或直接发文本），
          平台会把内容作为通知推送给管理员。留空密钥=关闭。改动随「保存设置」生效。
        </div>
        <div class="row gap">
          <input class="input" style="flex:1" v-model="s.WEBHOOK_SECRET" placeholder="点右侧随机生成，或自定义密钥" />
          <button class="btn sm" @click="genWebhookSecret" title="随机生成密钥">
            <svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
            </svg>随机
          </button>
        </div>
        <div v-if="platformWebhookUrl" class="webhook-url mono" style="margin-top:8px">{{ platformWebhookUrl }}</div>
        <button v-if="platformWebhookUrl" class="btn sm" style="margin-top:8px" @click="copyPlatformWebhook">复制地址</button>
      </div>

      <!-- 开放平台 API -->
      <div v-show="tab === 'api'" class="card" style="margin-top:16px">
        <div class="card-title">REST API</div>
        <div class="hint muted small" style="margin-bottom:12px">
          第三方工具（如 AI 助手、自动化脚本）可通过 API 远程管理平台和插件。
          请求时需携带此密钥验证身份。留空=关闭 API。改动随「保存设置」生效。
          <a href="https://github.com/AWdress/AWBotNest/blob/main/docs/API.md"
             target="_blank"
             style="color:#3b82f6;text-decoration:underline;margin-left:4px">
            查看 API 文档
          </a>
        </div>
        <div class="row gap">
          <input class="input" style="flex:1" v-model="s.API_KEY" placeholder="点右侧随机生成，或自定义密钥" />
          <button class="btn sm" @click="genApiKey" title="随机生成 API Key">
            <svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
            </svg>随机
          </button>
        </div>
        <button v-if="s.API_KEY" class="btn sm" style="margin-top:8px" @click="copyApiKey">复制密钥</button>
        <div v-if="s.API_KEY" class="hint muted small" style="margin-top:12px">
          <strong>使用示例</strong>（在请求头中携带）：<br>
          <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:12px">X-API-Key: {{ s.API_KEY.substring(0, 16) }}...</code>
        </div>
      </div>

      <!-- Web 控制台 -->
      <div v-show="tab === 'system'" class="card">
        <div class="card-title">Web 控制台</div>
        <div class="grid2">
          <div class="field"><label>监听端口 (WEB_UI_PORT)</label>
            <input class="input" type="number" v-model.number="s.WEB_UI_PORT" /></div>
          <div class="field"><label>外部地址 (WEB_UI_URL，可空)</label>
            <input class="input" v-model="s.WEB_UI_URL" /></div>
        </div>
        <div class="hint muted small">需要公网访问请自行用 Nginx / Caddy 等反向代理到本机端口。</div>
      </div>

      <!-- 运行代理 -->
      <div v-show="tab === 'system'" class="card" style="margin-top:16px">
        <div class="card-title">运行代理</div>
        <div class="row between">
          <span>启用代理</span>
          <div class="toggle" :class="{ on: s.proxy_set.proxy_enable }"
               @click="s.proxy_set.proxy_enable = !s.proxy_set.proxy_enable"></div>
        </div>
        <template v-if="s.proxy_set.proxy_enable">
          <div class="grid2">
            <div class="field"><label>协议</label>
              <select class="select" v-model="s.proxy_set.proxy.scheme">
                <option value="http">http</option><option value="socks4">socks4</option><option value="socks5">socks5</option>
              </select></div>
            <div class="field"><label>主机</label>
              <input class="input" v-model="s.proxy_set.proxy.hostname" /></div>
            <div class="field"><label>端口</label>
              <input class="input" type="number" v-model.number="s.proxy_set.proxy.port" /></div>
            <div class="field"><label>代理 URL (网页访问用)</label>
              <input class="input" v-model="s.proxy_set.PROXY_URL" /></div>
            <div class="field"><label>用户名 (可空)</label>
              <input class="input" v-model="s.proxy_set.proxy.username" /></div>
            <div class="field"><label>密码 (可空)</label>
              <input class="input" type="password" v-model="s.proxy_set.proxy.password" /></div>
          </div>
          <div class="test-row">
            <button class="btn sm" @click="testProxy" :disabled="proxyTesting">
              {{ proxyTesting ? '测试中…' : '测试代理' }}
            </button>
            <span v-if="proxyTest" class="test-result" :class="proxyTest.ok ? 'ok' : 'bad'">{{ proxyTest.message }}</span>
          </div>
        </template>
        <div class="field" style="margin-top:14px">
          <label>pip 镜像源 (插件装依赖用)</label>
          <input class="input" v-model="s.PIP_INDEX_URL"
                 placeholder="https://pypi.tuna.tsinghua.edu.cn/simple" />
          <div class="hint muted small">墙内建议填国内镜像（清华/阿里），境内直连不经墙。留空则走官方 pypi（此时若启用了上面的代理会自动用代理出墙）。</div>
        </div>
      </div>

      <!-- 数据库 -->
      <div v-show="tab === 'system'" class="card" style="margin-top:16px">
        <div class="card-title">数据库</div>
        <div class="grid2">
          <div class="field"><label>类型</label>
            <select class="select" v-model="s.DB_INFO.dbset">
              <option value="SQLite">SQLite</option><option value="mySQL">mySQL</option><option value="PostgreSQL">PostgreSQL</option>
            </select></div>
          <div class="field"><label>库名</label>
            <input class="input" v-model="s.DB_INFO.db_name" /></div>
        </div>
        <template v-if="s.DB_INFO.dbset !== 'SQLite'">
          <div class="grid2">
            <div class="field"><label>地址</label><input class="input" v-model="s.DB_INFO.address" /></div>
            <div class="field"><label>端口</label><input class="input" type="number" v-model.number="s.DB_INFO.port" /></div>
            <div class="field"><label>用户</label><input class="input" v-model="s.DB_INFO.user" /></div>
            <div class="field"><label>密码</label>
              <input class="input" type="password" v-model="s.DB_INFO.password" /></div>
          </div>
        </template>
        <div class="test-row">
          <button class="btn sm" @click="testDb" :disabled="dbTesting">
            {{ dbTesting ? '测试中…' : '测试连接' }}
          </button>
          <span v-if="dbTest" class="test-result" :class="dbTest.ok ? 'ok' : 'bad'">{{ dbTest.message }}</span>
        </div>
      </div>

      <!-- 维护 -->
      <div v-show="tab === 'maint'" class="card">
        <div class="card-title">维护</div>
        <div class="hint muted">
          这里可以设置日志自动清理、导出当前平台快照，或从已有备份包恢复。备份会包含 data/、sessions/、db_file/、plugins/。
          导入时会先校验并下载当前快照，重启平台后再应用恢复，避免损坏运行中的数据库。
        </div>

        <div class="maint-box">
          <div class="maint-item maint-settings">
            <div class="maint-heading">
              <div>
                <div class="maint-name">日志清理</div>
                <div class="maint-desc muted">定时清理平台运行日志和插件历史日志，避免长期占用磁盘空间。</div>
              </div>
              <button type="button" class="toggle" :class="{ on: s.LOG_CLEANER.enabled }"
                      :aria-pressed="s.LOG_CLEANER.enabled" aria-label="启用日志清理"
                      @click="s.LOG_CLEANER.enabled = !s.LOG_CLEANER.enabled"></button>
            </div>
            <div class="grid3" :class="{ disabled: !s.LOG_CLEANER.enabled }">
              <div class="field">
                <label>每天执行时间</label>
                <div class="time-fields">
                  <input class="input" type="number" min="0" max="23" aria-label="执行小时"
                         v-model.number="s.LOG_CLEANER.hour" />
                  <span>:</span>
                  <input class="input" type="number" min="0" max="59" aria-label="执行分钟"
                         v-model.number="s.LOG_CLEANER.minute" />
                </div>
              </div>
              <div class="field">
                <label>每个日志保留条数</label>
                <input class="input" type="number" min="1" max="1000" v-model.number="s.LOG_CLEANER.keep_lines" />
              </div>
            </div>
            <div style="margin-top:12px">
              <button class="btn sm" @click="cleanLogsNow" :disabled="cleaningLogs">
                {{ cleaningLogs ? '清理中…' : '立即清理日志' }}
              </button>
              <span v-if="cleaningLogs" class="muted small" style="margin-left:8px">正在清理，请稍候…</span>
            </div>
          </div>

          <div class="maint-item">
            <div>
              <div class="maint-name">导出备份</div>
              <div class="maint-desc muted">生成 zip 备份包，便于迁移、回滚或手动归档。</div>
            </div>
            <button class="btn btn-primary" @click="downloadBackup" :disabled="backupBusy">
              {{ backupBusy ? '导出中…' : '下载备份' }}
            </button>
          </div>

          <div class="maint-item">
            <div>
              <div class="maint-name">导入恢复</div>
              <div class="maint-desc muted">导入平台生成的 zip 备份包；重启后完整替换平台运行数据。</div>
            </div>
            <button class="btn" @click="openRestorePicker" :disabled="restoreBusy">
              {{ restoreBusy ? '恢复中…' : '选择备份包' }}
            </button>
            <input ref="restoreInput" type="file" accept=".zip,application/zip" style="display:none" @change="onRestoreFile" />
          </div>
        </div>
      </div>

      <div class="hint muted foot" v-if="tab === 'telegram'">提示：账号登录在「账号管理」页完成，账号列表会随登录自动写入。</div>
    </div>
  </div>

  <!-- 通知渠道配置弹窗 -->
  <div v-if="channelModalOpen" class="modal-mask" @click.self="channelModalOpen = false">
    <div class="modal-dialog channel-modal">
      <div class="modal-header">
        <h3>{{ channelModalMode === 'add' ? '添加通知渠道' : '编辑通知渠道' }}</h3>
        <button class="modal-close" @click="channelModalOpen = false">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 6 6 18M6 6l12 12"/>
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <!-- 弹窗副标题：显示渠道类型 -->
        <div class="channel-type-subtitle">
          <span class="channel-type-icon-sm">
            <svg v-if="channelForm.type === 'telegram'" viewBox="0 0 24 24" fill="currentColor" style="color:#0088cc;width:16px;height:16px;">
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/>
            </svg>
            <svg v-else-if="channelForm.type === 'wechat'" viewBox="0 0 24 24" fill="currentColor" style="color:#07c160;width:16px;height:16px;">
              <path d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 0 1 .213.665l-.39 1.48c-.019.07-.048.141-.048.213 0 .163.13.295.29.295a.326.326 0 0 0 .167-.054l1.903-1.114a.864.864 0 0 1 .717-.098 10.16 10.16 0 0 0 2.837.403c.276 0 .543-.027.811-.05-.857-2.578.157-4.972 1.932-6.446 1.703-1.415 3.882-1.98 5.853-1.838-.576-3.583-4.196-6.348-8.596-6.348zM5.785 5.991c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178A1.17 1.17 0 0 1 4.623 7.17c0-.651.52-1.18 1.162-1.18zm5.813 0c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178 1.17 1.17 0 0 1-1.162-1.178c0-.651.52-1.18 1.162-1.18zm5.34 2.867c-1.797-.052-3.746.512-5.28 1.786-1.72 1.428-2.687 3.72-1.78 6.22.942 2.453 3.666 4.229 6.884 4.229.826 0 1.622-.12 2.361-.336a.722.722 0 0 1 .598.082l1.584.926a.272.272 0 0 0 .14.045c.134 0 .24-.111.24-.247 0-.06-.023-.12-.038-.177l-.327-1.233a.582.582 0 0 1-.023-.156.49.49 0 0 1 .201-.398C23.024 18.48 24 16.82 24 14.98c0-3.21-2.931-5.837-6.656-6.088V8.89c-.135-.01-.27-.027-.407-.03zm-2.53 3.274c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982zm4.844 0c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982z"/>
            </svg>
            <svg v-else-if="channelForm.type === 'bark'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:var(--accent);width:16px;height:16px;">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
              <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
            </svg>
          </span>
          <span class="channel-type-name-sm">{{ getChannelTypeName(channelForm.type) }} 通知渠道</span>
        </div>

        <!-- 启用状态 -->
        <div class="field">
          <label class="checkbox-label">
            <input type="checkbox" v-model="channelForm.enabled" />
            <span>启用</span>
          </label>
        </div>

        <!-- 设为默认 -->
        <div class="field">
          <label class="checkbox-label">
            <input type="checkbox" v-model="channelForm.is_default" />
            <span>设为默认</span>
          </label>
          <div class="hint muted small">默认渠道用于接收平台通知，只能有一个默认渠道</div>
        </div>

        <!-- 名称 -->
        <div class="field">
          <label>名称</label>
          <input class="input" v-model="channelForm.name" placeholder="如：通知1、订单通知" />
          <div class="hint muted small">通知渠道名称</div>
        </div>

        <!-- 插件选择 -->
        <div class="field">
          <label>通知插件</label>
          <div class="hint muted small" style="margin-bottom:8px">选择该渠道接收哪些插件的通知</div>

          <!-- 全选复选框 -->
          <label class="plugin-checkbox-item">
            <input
              type="checkbox"
              :checked="isAllPluginsSelected(channelForm)"
              @change="toggleAllPlugins(channelForm)"
            />
            <span class="plugin-label-all">全部</span>
          </label>

          <!-- 插件列表 -->
          <div class="plugins-grid">
            <label
              v-for="p in getAvailablePluginsForChannel(channelForm.type)"
              :key="p.id"
              class="plugin-checkbox-item"
            >
              <input
                type="checkbox"
                :checked="Array.isArray(channelForm.plugins) && channelForm.plugins.includes(p.id)"
                @change="togglePlugin(channelForm, p.id)"
              />
              <span class="plugin-label">{{ p.name }}</span>
              <span v-if="(p.tags || []).includes('机器人') || (p.tags || []).includes('双账号')" class="plugin-bot-tag">Bot</span>
            </label>
          </div>
        </div>

        <!-- Telegram 配置 -->
        <template v-if="channelForm.type === 'telegram'">
          <div class="field">
            <label>Bot Token</label>
            <input class="input" v-model="channelForm.config.token"
                   placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" />
            <div class="hint muted small">Telegram机器人token，格式：123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11</div>
          </div>
          <div class="field">
            <label>Chat ID（可选）</label>
            <input class="input" v-model="channelForm.config.chat_id"
                   placeholder="接收消息的用户、群组或频道 Chat ID" />
            <div class="hint muted small">接收消息通知的用户、群组或频道Chat ID</div>
          </div>
        </template>

        <!-- 企业微信配置 -->
        <template v-if="channelForm.type === 'wechat'">
          <div class="field">
            <label>企业ID</label>
            <input class="input" v-model="channelForm.config.corpid"
                   placeholder="企业微信后台企业信息中的企业ID" />
            <div class="hint muted small">企业微信后台企业信息中的企业ID</div>
          </div>
          <div class="field">
            <label>应用AgentId</label>
            <input class="input" v-model="channelForm.config.agentid"
                   placeholder="企业微信自建应用的AgentId" />
            <div class="hint muted small">企业微信自建应用的AgentId</div>
          </div>
          <div class="field">
            <label>应用Secret</label>
            <input class="input" v-model="channelForm.config.secret"
                   placeholder="企业微信自建应用的Secret" />
            <div class="hint muted small">企业微信自建应用的Secret</div>
          </div>
          <div class="field">
            <label>代理地址（可选）</label>
            <input class="input" v-model="channelForm.config.proxy"
                   placeholder="https://qyapi.weixin.qq.com" />
            <div class="hint muted small">不填时使用企业微信官方地址；使用转发服务时填写服务根地址。</div>
          </div>
          <div class="field">
            <label>接收成员（可选）</label>
            <input class="input" v-model="channelForm.config.touser"
                   placeholder="@all" />
            <div class="hint muted small">默认发送给全部成员；指定多个成员时用竖线分隔。</div>
          </div>
        </template>

        <!-- Bark 配置 -->
        <template v-if="channelForm.type === 'bark'">
          <div class="field">
            <label>服务器地址</label>
            <input class="input" v-model="channelForm.config.server"
                   placeholder="https://api.day.app" />
            <div class="hint muted small">Bark 服务器地址，如 https://api.day.app</div>
          </div>
          <div class="field">
            <label>设备密钥</label>
            <input class="input" v-model="channelForm.config.device_key"
                   placeholder="从 Bark App 中获取" />
            <div class="hint muted small">从 Bark App 中获取的设备密钥</div>
          </div>
        </template>
      </div>
      <div class="modal-footer">
        <button class="btn" @click="channelModalOpen = false" :disabled="saving">取消</button>
        <button class="btn primary" @click="saveChannel" :disabled="saving">{{ saving ? '保存中…' : '确认' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page { position: relative; min-height: 100%; }
.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; gap: 16px; flex-wrap: wrap; }

/* Tab 切换（与插件管理一致） */
.tabs { display: flex; gap: 4px; background: var(--bg-elevated); padding: 4px; border-radius: 10px; flex-wrap: wrap; }
.tab {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 18px; border: none; background: transparent; cursor: pointer;
  color: var(--text-secondary); font-size: 14px; font-weight: 600; border-radius: 7px;
  transition: all 0.15s;
}
.tab:hover { color: var(--text-primary); }
.tab.active { background: var(--bg-card); color: var(--text-primary); box-shadow: 0 1px 3px rgba(0,0,0,0.25); }

.panel {
  max-width: 1200px;
  margin: 0 auto; /* 居中显示 */
  width: 100%; /* 确保在小屏幕上也能正常显示 */
}
.center { text-align: center; padding: 40px; }

/* 未保存改动小圆点 */
.dirty-dot { width: 7px; height: 7px; border-radius: 50%; background: #fff; margin-right: 2px; flex-shrink: 0; }

/* 保存后需重启横幅 */
.restart-banner {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  background: var(--accent-dim); color: var(--text-primary);
  border: 1px solid var(--accent); border-radius: var(--radius-sm);
  padding: 10px 14px; font-size: 13px; margin-bottom: 14px;
}

/* 连接测试行 */
.test-row { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.test-result { font-size: 12px; }
.test-result.ok { color: var(--accent-2); }
.test-result.bad { color: var(--danger); }
.alert { padding: 10px 14px; border-radius: var(--radius-sm); font-size: 13px; margin-bottom: 14px; }
.alert.err { background: var(--danger-dim); color: var(--danger); }
.alert.ok { background: var(--accent-2-dim); color: var(--accent-2); }
.card { display: flex; flex-direction: column; gap: 14px; }
.card-title { font-size: 14px; font-weight: 600; color: var(--accent); }
.hint { font-size: 12px; }
.foot { margin-top: 16px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 12px; color: var(--text-secondary); }
.actions { display: flex; justify-content: flex-end; gap: 10px; }
.row.between { display: flex; align-items: center; justify-content: space-between; }

/* 通知：Bot 卡片网格 + 推送路由表 */
.btn.sm { padding: 6px 12px; font-size: 13px; }
.btn.sm.danger { color: var(--danger); }

.bot-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.bot-card {
  position: relative;
  background: var(--bg-elevated); border: 1px solid var(--border-light);
  border-radius: var(--radius); padding: 14px;
  display: flex; flex-direction: column; gap: 12px;
}
.bot-card-head { display: flex; align-items: flex-start; gap: 10px; }
.bot-ava {
  flex-shrink: 0; width: 38px; height: 38px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-dim); color: var(--accent);
}
.bot-ava svg { width: 20px; height: 20px; }
.bot-id { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.bot-name-row { width: 100%; display: flex; align-items: center; gap: 8px; }
.bot-name-text { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.badge-default { background: var(--accent-dim); color: var(--accent); font-size: 10px; padding: 1px 8px; }
.bot-name-input { min-width: 0; flex: 1; padding: 6px 10px; font-size: 13px; }
.set-default-btn {
  flex: 0 0 auto; padding: 4px 8px; border: 1px solid var(--border-light); border-radius: 7px;
  color: var(--text-secondary); background: transparent; font-size: 11px; cursor: pointer;
}
.set-default-btn:hover { color: var(--accent); border-color: var(--accent); background: var(--accent-dim); }
.bot-chat { padding: 6px 10px; font-size: 12px; }
.bot-status { display: flex; align-items: center; gap: 6px; font-size: 11px; }
.bot-status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; }
.bot-status .dot.on { background: var(--success); box-shadow: 0 0 6px var(--success); }
.bot-status .mono { font-size: 11px; }
.bot-del {
  position: absolute; top: 8px; right: 8px;
  width: 22px; height: 22px; border-radius: 6px; border: none;
  background: transparent; color: var(--text-muted); cursor: pointer; font-size: 13px;
  display: flex; align-items: center; justify-content: center; transition: all 0.15s;
}
.bot-del:hover { background: var(--danger-dim); color: var(--danger); }
.bot-del .x-ico { width: 14px; height: 14px; }
.btn-ico { width: 14px; height: 14px; flex-shrink: 0; }
.bot-add {
  align-items: center; justify-content: center; gap: 6px;
  border-style: dashed; color: var(--text-secondary); cursor: pointer;
  min-height: 96px; font-size: 13px; font-weight: 600;
}
.bot-add:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.bot-add-plus { font-size: 22px; line-height: 1; }

.route-search { margin-bottom: 8px; }
.route-table { display: flex; flex-direction: column; gap: 8px; max-height: 360px; overflow-y: auto; }
.route-row { display: flex; align-items: center; gap: 10px; }
.route-name { flex: 1; font-size: 13px; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.route-sel { max-width: 220px; flex: 0 0 auto; }

/* 多选推送路由样式 */
.route-table-multi { display: flex; flex-direction: column; gap: 12px; max-height: 500px; overflow-y: auto; }
.route-row-multi {
  display: flex; flex-direction: column; gap: 8px;
  padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-elevated);
}
.route-plugin-info { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.route-tag {
  font-size: 10px; padding: 2px 6px; border-radius: 4px;
  background: var(--accent-dim); color: var(--accent);
}
.route-channels { display: flex; flex-wrap: wrap; gap: 8px; }
.channel-checkbox {
  display: flex; align-items: center; gap: 6px; padding: 6px 10px;
  border: 1px solid var(--border-light); border-radius: var(--radius-sm);
  background: var(--bg); cursor: pointer; transition: all 0.15s;
  font-size: 13px;
}
.channel-checkbox:hover { background: var(--bg-elevated); border-color: var(--accent); }
.channel-checkbox input[type="checkbox"] {
  width: 16px; height: 16px; cursor: pointer;
  accent-color: var(--accent);
}
.channel-label { display: flex; align-items: center; gap: 4px; color: var(--text-primary); }
.channel-type-badge {
  font-size: 10px; padding: 1px 5px; border-radius: 3px;
  background: var(--bg-elevated); color: var(--text-secondary);
}

.small { font-size: 12px; }
.maint-box { display: flex; flex-direction: column; gap: 12px; }
.maint-item {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-elevated);
}
.maint-name { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.maint-desc { font-size: 12px; margin-top: 4px; max-width: 520px; }
.maint-settings { align-items: stretch; flex-direction: column; }
.maint-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.grid3 { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr); gap: 14px; }
.grid3.disabled { opacity: 0.45; pointer-events: none; }
.time-fields { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 8px; }
.time-fields span { color: var(--text-muted); font-weight: 700; }

/* 平台 webhook 地址展示 */
.row.gap { display: flex; align-items: center; gap: 8px; }
.row.gap .input { flex: 1; }
.webhook-url {
  margin-top: 8px; font-size: 12px; word-break: break-all; padding: 8px 10px;
  background: #07090f; border-radius: var(--radius-sm); color: var(--text-primary);
}
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }

/* 通知渠道卡片（MP风格） */
.channel-grid-mp {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 12px;
}

.channel-card-mp {
  padding: 16px 18px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  transition: all 0.15s;
  cursor: pointer;
  position: relative;
  z-index: 1;
}

.channel-card-mp:hover {
  border-color: var(--accent-dim);
  background: var(--bg-hover);
  z-index: 2;
}

/* 顶部行：状态灯+名称+默认标识 | 删除按钮 */
.channel-top-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.channel-left-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.status-dot-mp {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}

.status-dot-mp.on {
  background: var(--accent-2);
  box-shadow: 0 0 8px var(--accent-2);
}

.channel-name-text {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.badge-default-mp {
  display: inline-block;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-dim);
  border-radius: 4px;
  flex-shrink: 0;
}

.delete-btn-mp {
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
  flex-shrink: 0;
}

.delete-btn-mp svg {
  width: 16px;
  height: 16px;
}

.delete-btn-mp:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* 底部行：类型标签（左） | 图标（右） */
.channel-bottom-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.channel-type-label {
  font-size: 13px;
  color: var(--text-secondary);
}

/* 弹窗渠道类型副标题 */
.channel-type-subtitle {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: var(--bg-elevated);
  border-radius: 6px;
  margin-bottom: 4px;
}
.channel-type-name-sm {
  font-size: 12px;
  color: var(--text-muted);
}

.channel-icon-mp {
  width: 64px;
  height: 64px;
  border-radius: 12px;
  background: var(--bg-card);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.channel-icon-mp svg {
  width: 40px;
  height: 40px;
}

.icon-telegram {
  color: #0088cc;
}

.icon-wechat {
  color: #07c160;
}

.icon-bark {
  color: var(--accent);
}

/* 添加按钮和下拉菜单 */
.channel-footer {
  display: flex;
  gap: 10px;
}

.add-channel-dropdown {
  position: relative;
}

.btn-add-mp {
  width: 48px;
  height: 48px;
  border: 2px dashed var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}

.btn-add-mp svg {
  width: 24px;
  height: 24px;
}

.btn-add-mp:hover {
  border-color: var(--accent);
  background: var(--accent-dim);
  color: var(--accent);
}

.add-menu-dropdown {
  position: absolute;
  top: calc(100% + 8px); /* 显示在按钮下方 */
  left: 0; /* 左对齐按钮，这样菜单在按钮正下方或偏左 */
  min-width: 180px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  padding: 6px;
  z-index: 1000; /* 提升层级 */
}

.add-menu-dropdown button {
  width: 100%;
  padding: 10px 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-primary);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 10px;
  transition: all 0.15s;
}

.add-menu-dropdown button:hover {
  background: var(--bg-hover);
}

.add-menu-dropdown svg {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
}

/* 通知渠道配置弹窗 */
.modal-mask {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.7);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000; /* 提升弹窗层级到最高 */
  padding: 20px;
}

.modal-dialog {
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: 12px;
  max-width: 600px;
  width: 100%;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  z-index: 10001; /* 弹窗内容层级更高 */
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px;
  border-bottom: 1px solid var(--border);
}

.modal-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.modal-close {
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}

.modal-close svg {
  width: 20px;
  height: 20px;
}

.modal-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* 插件选择样式 */
.plugins-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 8px;
  max-height: 240px;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}

.plugin-checkbox-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
  font-size: 13px;
}

.plugin-checkbox-item:hover {
  background: var(--bg-elevated);
}

.plugin-checkbox-item input[type="checkbox"] {
  width: 16px;
  height: 16px;
  cursor: pointer;
  accent-color: var(--accent);
}

.plugin-label {
  flex: 1;
  color: var(--text-primary);
}

.plugin-label-all {
  flex: 1;
  color: var(--text-primary);
  font-weight: 600;
}

.plugin-bot-tag {
  font-size: 9px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--accent-dim);
  color: var(--accent);
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 20px;
  border-top: 1px solid var(--border);
}

/* 平台 AI 服务 */
.ai-overview {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  overflow: hidden;
  background:
    radial-gradient(420px 160px at 15% 0%, rgba(48,128,240,.17), transparent 70%),
    var(--bg-card);
}
.ai-overview::after {
  content: '';
  position: absolute;
  width: 180px;
  height: 180px;
  right: -70px;
  bottom: -110px;
  border: 1px solid rgba(16,176,128,.22);
  border-radius: 50%;
}
.ai-overview-main { display: flex; align-items: center; gap: 14px; min-width: 0; }
.ai-mark {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  flex: 0 0 auto;
  border-radius: 14px;
  color: #fff;
  font-weight: 800;
  letter-spacing: -.04em;
  background: linear-gradient(135deg, #3080f0, #10b080);
  box-shadow: 0 10px 28px rgba(48,128,240,.28);
}
.ai-status-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  position: absolute;
  left: 82px;
  bottom: 12px;
  color: var(--text-muted);
  font-size: 11px;
}
.ai-status-strip b { color: var(--text-secondary); }
.ai-status-strip .ok b { color: var(--success); }
.ai-status-strip .bad b { color: var(--danger); }
.ai-section-head { margin-bottom: 14px; }
.ai-provider-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}
.ai-provider-card {
  display: flex;
  flex-direction: column;
  gap: 11px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: linear-gradient(145deg, rgba(32,34,46,.78), rgba(17,19,26,.72));
}
.ai-model-grid { display: flex; flex-direction: column; gap: 10px; }
.ai-model-empty {
  padding: 24px;
  border: 1px dashed var(--border-light);
  border-radius: var(--radius);
  background: rgba(255,255,255,.012);
}
.ai-model-card {
  display: flex;
  flex-direction: column;
  gap: 11px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elevated);
}
.ai-model-capabilities {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  padding-top: 2px;
}
.ai-model-capabilities label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  cursor: pointer;
}
.ai-model-capabilities input { accent-color: var(--accent); }
.ai-provider-enable { display: flex; align-items: center; gap: 7px; font-size: 12px; cursor: pointer; }
.ai-provider-enable input { accent-color: var(--accent-2); }
.ai-remove {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-muted);
  font-size: 20px;
  cursor: pointer;
}
.ai-remove:hover { color: var(--danger); background: var(--danger-dim); }
.ai-capability-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-top: 14px;
}
.ai-capability-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elevated);
}
.ai-capability-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-weight: 650; }
.ai-test-note { margin-top: 10px; }
.ai-permission-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 460px;
  margin-top: 14px;
  overflow-y: auto;
}
.ai-permission-row {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) auto repeat(3, auto);
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-elevated);
}
.ai-plugin-name { display: flex; min-width: 0; flex-direction: column; }
.ai-plugin-name .mono { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ai-permission-switch,
.ai-permission-cap {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  font-size: 12px;
  cursor: pointer;
}
.ai-permission-switch input,
.ai-permission-cap input { accent-color: var(--accent); }
.ai-permission-cap.disabled { opacity: .42; cursor: not-allowed; }

@media (max-width: 600px) {
  .grid2 { grid-template-columns: 1fr; }
  .bot-grid { grid-template-columns: 1fr; }
  .channel-grid { grid-template-columns: 1fr; }
  .ai-provider-grid,
  .ai-capability-grid { grid-template-columns: 1fr; }
  .ai-overview { align-items: flex-start; }
  .ai-status-strip { position: static; flex-basis: 100%; }
  .ai-overview { flex-wrap: wrap; }
}

/* 手机适配 */
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; gap: 12px; }
  /* tab 全部换行展示，避免靠右的「数据库」被裁掉看不全 */
  .tabs { flex-wrap: wrap; }
  .tab { white-space: nowrap; flex: 1 1 auto; justify-content: center; padding: 8px 12px; }
  .panel { max-width: 100%; }
  .maint-item { flex-direction: column; align-items: stretch; }
  .grid3 { grid-template-columns: 1fr; }
  .ai-capability-grid { grid-template-columns: 1fr; }
  .ai-permission-row { grid-template-columns: 1fr 1fr; }
  .ai-plugin-name { grid-column: 1 / -1; }
}
</style>
