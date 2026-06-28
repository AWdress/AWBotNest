<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'
import ConfigForm from '../components/ConfigForm.vue'
import { confirm } from '../composables/confirm'
import { toast } from '../composables/toast'
import logo from '../assets/logo.png'

const tab = ref('mine')   // mine | store

const plugins = ref([])
const loading = ref(true)
const error = ref('')
const busy = ref({})
const fileInput = ref(null)

// 配置弹窗
const configOpen = ref(false)
const configTarget = ref(null)
const configSchema = ref({})
const configValues = ref({})
const configSaving = ref(false)

// 应用账号弹窗（多账号下按账号选择插件）
const acctOpen = ref(false)
const acctTarget = ref(null)
const acctOptions = ref([])    // [{session,name}]
const acctSelected = ref([])   // 勾选的 session
const acctAllMode = ref(true)  // 应用到全部账号
const acctSaving = ref(false)

const scopeLabel = { user: '用户账号', bot: '机器人', both: '双账号' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.listPlugins()
    plugins.value = data.plugins
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function toggle(p) {
  if (p.error) return
  busy.value[p.id] = true
  try {
    const data = p.enabled ? await api.disablePlugin(p.id) : await api.enablePlugin(p.id)
    Object.assign(p, data.plugin)
  } catch (e) {
    error.value = `${p.name}: ${e.message}`
  } finally {
    busy.value[p.id] = false
  }
}

async function reload(p) {
  busy.value[p.id] = true
  try {
    const data = await api.reloadPlugin(p.id)
    Object.assign(p, data.plugin)
  } catch (e) {
    error.value = `${p.name}: ${e.message}`
  } finally {
    busy.value[p.id] = false
  }
}

async function remove(p) {
  const ok = await confirm({
    title: '删除插件',
    message: `确定删除插件「${p.name}」？\n此操作不可恢复。`,
    confirmText: '删除', danger: true,
  })
  if (!ok) return
  busy.value[p.id] = true
  try {
    await api.deletePlugin(p.id)
    plugins.value = plugins.value.filter((x) => x.id !== p.id)
    await loadStore(false)
  } catch (e) {
    error.value = `${p.name}: ${e.message}`
  } finally {
    busy.value[p.id] = false
  }
}

async function openConfig(p) {
  configTarget.value = p
  try {
    const data = await api.getPluginConfig(p.id)
    configSchema.value = data.schema || {}
    configValues.value = data.values || {}
    configOpen.value = true
  } catch (e) {
    error.value = e.message
  }
}

async function saveConfig() {
  configSaving.value = true
  try {
    await api.setPluginConfig(configTarget.value.id, configValues.value)
    configOpen.value = false
  } catch (e) {
    error.value = e.message
  } finally {
    configSaving.value = false
  }
}

async function openAccounts(p) {
  acctTarget.value = p
  try {
    const data = await api.getPluginAccounts(p.id)
    acctOptions.value = data.accounts || []
    acctSelected.value = [...(data.selected || [])]
    acctAllMode.value = acctSelected.value.length === 0
    acctOpen.value = true
  } catch (e) { error.value = e.message }
}
function toggleAcct(session) {
  const i = acctSelected.value.indexOf(session)
  if (i >= 0) acctSelected.value.splice(i, 1)
  else acctSelected.value.push(session)
}
async function saveAccounts() {
  acctSaving.value = true
  try {
    const sessions = acctAllMode.value ? [] : acctSelected.value
    await api.setPluginAccounts(acctTarget.value.id, sessions)
    acctOpen.value = false
  } catch (e) { error.value = e.message } finally { acctSaving.value = false }
}

function triggerUpload() { fileInput.value?.click() }
async function onFile(e) {
  const file = e.target.files?.[0]
  if (!file) return
  try {
    await api.uploadPlugin(file)
    await load()
    toast.success(`插件「${file.name}」安装完成`)
  } catch (err) {
    error.value = `上传失败: ${err.message}`
    toast.error('插件上传失败')
  } finally {
    e.target.value = ''
  }
}

// 拖拽上传
const dragging = ref(false)
async function onDrop(e) {
  dragging.value = false
  if (tab.value !== 'mine') return
  const file = e.dataTransfer.files?.[0]
  if (!file || !file.name.endsWith('.py')) {
    error.value = '只接受 .py 文件'
    return
  }
  try {
    await api.uploadPlugin(file)
    await load()
    toast.success(`插件「${file.name}」安装完成`)
  } catch (err) {
    error.value = `上传失败: ${err.message}`
    toast.error('插件上传失败')
  }
}

const stats = computed(() => ({
  total: plugins.value.length,
  enabled: plugins.value.filter((p) => p.enabled).length,
  error: plugins.value.filter((p) => p.error).length,
}))

// ── 插件市场（多仓库聚合） ──
const store = ref([])
const officialIds = ref([])
const storeBusy = ref(false)
const storeErr = ref('')
const storeLastSync = ref(null)
const dlBusy = ref({})

const storeAvailable = computed(() => store.value.filter((p) => !p.installed))
const officialSet = computed(() => new Set(officialIds.value))
function isOfficial(p) { return p.official || officialSet.value.has(p.id) }

// 仓库来源只显示 owner/repo（去掉 https://github.com/、.git、/tree/分支/子目录 等）
function shortRepo(url) {
  if (!url) return ''
  let s = String(url).trim()
  s = s.replace(/^https?:\/\/(www\.)?github\.com\//i, '')  // 去协议+域名
  s = s.replace(/^https?:\/\/[^/]+\//i, '')                 // 其它主机也去掉
  s = s.replace(/\.git$/i, '')
  const parts = s.split('/').filter(Boolean)
  return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : s
}

async function loadStore(refresh = false) {
  storeBusy.value = true; storeErr.value = ''
  try {
    const d = await api.pluginStore(refresh)
    store.value = d.plugins || []
    officialIds.value = d.official_ids || []
    storeLastSync.value = d.last_sync
    if (d.errors && d.errors.length) storeErr.value = d.errors.join('；')
  } catch (e) {
    storeErr.value = e.message
  } finally {
    storeBusy.value = false
  }
}

async function download(p) {
  dlBusy.value[p.id] = true; storeErr.value = ''
  try {
    const r = await api.storeDownload([p])
    const res = r.result || {}
    if (res.errors && res.errors.length) {
      storeErr.value = res.errors.join('；')
      toast.error(`${p.name} 安装失败`)
    } else {
      p.installed = true
      await load()
      toast.success(`插件「${p.name}」安装完成`)
    }
  } catch (e) {
    storeErr.value = `${p.name}: ${e.message}`
    toast.error(`${p.name} 安装失败`)
  } finally {
    dlBusy.value[p.id] = false
  }
}

// ── 设置 GitHub 仓库地址（多仓库） ──
const repoOpen = ref(false)
const repoInterval = ref(20)
const repoList = ref([])
const repoSaving = ref(false)
const repoErr = ref('')

async function openRepos() {
  repoErr.value = ''
  try {
    const d = await api.getSettings()
    const s = d.settings || {}
    repoInterval.value = s.PLUGIN_REPO_INTERVAL || 20
    repoList.value = (s.PLUGIN_REPOS || []).map((r) => ({ url: r.url || '', token: r.token || '' }))
    repoOpen.value = true
  } catch (e) {
    repoErr.value = e.message
  }
}

function addRepo() { repoList.value.push({ url: '', token: '' }) }
function delRepo(i) { repoList.value.splice(i, 1) }

async function saveRepos() {
  repoSaving.value = true; repoErr.value = ''
  try {
    const repos = repoList.value
      .map((r) => ({ url: (r.url || '').trim(), token: (r.token || '').trim() }))
      .filter((r) => r.url)
    await api.saveSettings({
      PLUGIN_REPO_ENABLE: true,
      PLUGIN_REPO_INTERVAL: Number(repoInterval.value) || 20,
      PLUGIN_REPOS: repos,
    })
    repoOpen.value = false
    await loadStore(true)
  } catch (e) {
    repoErr.value = e.message
  } finally {
    repoSaving.value = false
  }
}

function goStore() { tab.value = 'store'; if (store.value.length === 0) loadStore(true) }

onMounted(() => { load(); loadStore(false) })
</script>

<template>
  <div
    class="plugins"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDrop"
  >
    <!-- 顶部：Tab 切换 + 操作 -->
    <div class="toolbar">
      <div class="tabs">
        <button class="tab" :class="{ active: tab === 'mine' }" @click="tab = 'mine'">
          我的插件 <span class="tab-count">{{ stats.total }}</span>
        </button>
        <button class="tab" :class="{ active: tab === 'store' }" @click="goStore">
          插件市场 <span class="tab-count" v-if="storeAvailable.length">{{ storeAvailable.length }}</span>
        </button>
      </div>
      <div class="row gap">
        <template v-if="tab === 'mine'">
          <span class="stats">已启用 <b style="color:var(--accent-2)">{{ stats.enabled }}</b><template v-if="stats.error"> · <span style="color:var(--danger)">异常 {{ stats.error }}</span></template></span>
          <button class="btn" @click="load">刷新</button>
          <button class="btn btn-primary" @click="triggerUpload">+ 上传插件</button>
          <input ref="fileInput" type="file" accept=".py" hidden @change="onFile" />
        </template>
        <template v-else>
          <span class="stats muted small" v-if="storeLastSync">上次刷新 {{ storeLastSync }}</span>
          <button class="btn" @click="openRepos">设置仓库地址</button>
          <button class="btn btn-primary" @click="loadStore(true)" :disabled="storeBusy">
            {{ storeBusy ? '刷新中…' : '刷新市场' }}
          </button>
        </template>
      </div>
    </div>

    <div v-if="error" class="alert">{{ error }} <span @click="error=''" class="close">×</span></div>

    <!-- ════ 我的插件 ════ -->
    <template v-if="tab === 'mine'">
      <div v-if="loading" class="muted center">加载中…</div>
      <div v-else-if="plugins.length === 0" class="empty card">
        <p>还没有插件。</p>
        <p class="muted">去「插件市场」安装，或把 .py 文件拖到这里 / 点「上传插件」。</p>
      </div>
      <div v-else class="grid">
        <div v-for="p in plugins" :key="p.id" class="card plugin-card" :class="{ err: p.error }">
          <div class="card-head">
            <div class="store-title">
              <img :src="p.icon || logo" class="store-icon" :class="{ 'store-icon-fallback': !p.icon }" alt="" />
              <div class="card-title">
                <span class="name">{{ p.name }}
                  <span v-if="isOfficial(p)" class="badge-official">官方</span>
                </span>
                <span class="badge" :class="p.error ? 'badge-err' : (p.enabled ? 'badge-on' : 'badge-off')">
                  {{ p.error ? '异常' : (p.enabled ? '已启用' : '未启用') }}
                </span>
              </div>
            </div>
            <div class="toggle" :class="{ on: p.enabled, disabled: p.error || busy[p.id] }"
                 @click="toggle(p)"></div>
          </div>

          <p class="desc">{{ p.description || '（无描述）' }}</p>
          <div v-if="p.error" class="err-msg">{{ p.error }}</div>

          <div class="card-meta">
            <span class="meta-item">{{ scopeLabel[p.scope] || p.scope }}</span>
            <span class="meta-item">v{{ p.version }}</span>
            <span v-if="p.author" class="meta-item">{{ p.author }}</span>
          </div>

          <div class="card-actions">
            <button class="btn sm" @click="openConfig(p)"
                    :disabled="Object.keys(p.config_schema || {}).length === 0">配置</button>
            <button class="btn sm" v-if="p.scope === 'user' || p.scope === 'both'"
                    @click="openAccounts(p)">账号</button>
            <button class="btn sm" @click="reload(p)" :disabled="busy[p.id]">重载</button>
            <button class="btn sm btn-danger" @click="remove(p)" :disabled="busy[p.id]">删除</button>
          </div>
        </div>
      </div>
    </template>

    <!-- ════ 插件市场 ════ -->
    <template v-else>
      <div class="hint muted store-hint">来自官方仓库与你配置的 GitHub 仓库。点「安装」拉到本地（不自动启用），安装后到「我的插件」开启。</div>
      <div v-if="storeErr" class="alert">{{ storeErr }} <span @click="storeErr=''" class="close">×</span></div>

      <div v-if="storeBusy && store.length === 0" class="muted center">加载市场…</div>
      <div v-else-if="storeAvailable.length === 0" class="empty card">
        <p class="muted">市场里没有可安装的新插件（仓库里的都已安装），或还没配置额外仓库。</p>
      </div>
      <div v-else class="grid">
        <div v-for="p in storeAvailable" :key="p.id" class="card plugin-card store-card">
          <div class="card-head">
            <div class="store-title">
              <img :src="p.icon || logo" class="store-icon" :class="{ 'store-icon-fallback': !p.icon }" alt="" />
              <div class="card-title">
                <span class="name">{{ p.name }}
                  <span v-if="isOfficial(p)" class="badge-official">官方</span>
                </span>
                <span class="badge badge-off" v-if="p.version">v{{ p.version }}</span>
              </div>
            </div>
          </div>

          <p class="desc">{{ p.description || '（无描述）' }}</p>
          <div class="card-meta">
            <span v-if="p.author" class="meta-item">{{ p.author }}</span>
            <span class="meta-item mono">{{ shortRepo(p.repo_url) }}</span>
          </div>

          <div class="card-actions">
            <button class="btn sm btn-primary" @click="download(p)" :disabled="dlBusy[p.id]">
              {{ dlBusy[p.id] ? '安装中…' : '⬇ 安装' }}
            </button>
          </div>
        </div>
      </div>
    </template>

    <!-- 拖拽遮罩 -->
    <div v-if="dragging && tab === 'mine'" class="drag-overlay">松手上传 .py 插件</div>

    <!-- 配置弹窗 -->
    <div v-if="configOpen" class="modal-mask" @click.self="configOpen=false">
      <div class="modal card">
        <div class="modal-head">
          <h2>{{ configTarget?.name }} · 配置</h2>
          <span class="close" @click="configOpen=false">×</span>
        </div>
        <ConfigForm v-model="configValues" :schema="configSchema" />
        <div class="modal-foot">
          <button class="btn" @click="configOpen=false">取消</button>
          <button class="btn btn-primary" @click="saveConfig" :disabled="configSaving">
            {{ configSaving ? '保存中…' : '保存并应用' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 设置仓库地址弹窗 -->
    <!-- 应用账号弹窗 -->
    <div v-if="acctOpen" class="modal-mask" @click.self="acctOpen=false">
      <div class="modal card">
        <div class="modal-head">
          <h2>{{ acctTarget?.name }} · 应用账号</h2>
          <span class="close" @click="acctOpen=false">×</span>
        </div>
        <div class="form">
          <div class="hint muted">选择这个插件在哪些账号上生效。多账号时可让不同号开不同插件。</div>
          <label class="acct-row">
            <input type="radio" :checked="acctAllMode" @change="acctAllMode = true" />
            <span>全部账号（默认）</span>
          </label>
          <label class="acct-row">
            <input type="radio" :checked="!acctAllMode" @change="acctAllMode = false" />
            <span>仅指定账号</span>
          </label>
          <div v-if="!acctAllMode" class="acct-list">
            <div v-if="acctOptions.length === 0" class="muted small">还没有账号，去「账号管理」登录。</div>
            <label v-for="a in acctOptions" :key="a.session" class="acct-item">
              <input type="checkbox" :checked="acctSelected.includes(a.session)" @change="toggleAcct(a.session)" />
              <span>{{ a.name }}</span>
              <span class="muted mono small">{{ a.session }}</span>
            </label>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="acctOpen=false">取消</button>
          <button class="btn btn-primary" @click="saveAccounts" :disabled="acctSaving">
            {{ acctSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="repoOpen" class="modal-mask" @click.self="repoOpen=false">
      <div class="modal card">
        <div class="modal-head">
          <h2>设置 GitHub 仓库地址</h2>
          <span class="close" @click="repoOpen=false">×</span>
        </div>
        <div v-if="repoErr" class="alert">{{ repoErr }}</div>
        <div class="form">
          <div class="muted small">官方仓库已内置，无需添加。这里配置你自己的额外仓库。</div>
          <div class="muted small">自动轮询已常开：每隔下面的间隔自动刷新市场并更新已安装插件（仍不自动启用）。</div>
          <div class="field">
            <label>轮询间隔（分钟）</label>
            <input class="input" type="number" min="1" v-model.number="repoInterval" style="max-width:160px" />
          </div>
          <div class="field">
            <label>额外插件仓库（可加多个）</label>
            <div v-for="(r, i) in repoList" :key="i" class="repo-row">
              <input class="input" v-model="r.url"
                     placeholder="owner/repo 或 https://github.com/owner/repo（可带 /tree/分支/子目录）" />
              <input class="input repo-token" type="password" v-model="r.token" placeholder="token（私有库才填）" />
              <button class="btn sm btn-danger" @click="delRepo(i)">×</button>
            </div>
            <button class="btn sm" @click="addRepo">+ 添加仓库</button>
            <div class="hint muted">推荐仓库带 manifest.json 并写好 version，平台才能识别「更新」。</div>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="repoOpen=false">取消</button>
          <button class="btn btn-primary" @click="saveRepos" :disabled="repoSaving">
            {{ repoSaving ? '保存中…' : '保存并刷新市场' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.plugins { position: relative; min-height: 100%; }

.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; gap: 16px; }

/* Tab 切换 */
.tabs { display: flex; gap: 4px; background: var(--bg-elevated); padding: 4px; border-radius: 10px; }
.tab {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 18px; border: none; background: transparent; cursor: pointer;
  color: var(--text-secondary); font-size: 14px; font-weight: 600; border-radius: 7px;
  transition: all 0.15s;
}
.tab:hover { color: var(--text-primary); }
.tab.active { background: var(--bg-card); color: var(--text-primary); box-shadow: 0 1px 3px rgba(0,0,0,0.25); }
.tab-count {
  font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 10px;
  background: var(--bg-elevated); color: var(--text-muted);
}
.tab.active .tab-count { background: var(--accent-dim); color: var(--accent); }

.stats { color: var(--text-secondary); font-size: 13px; }
.stats b { color: var(--text-primary); }
.small { font-size: 12px; }

.alert {
  background: var(--danger-dim); color: var(--danger);
  padding: 10px 14px; border-radius: var(--radius-sm);
  margin-bottom: 16px; font-size: 13px;
  display: flex; justify-content: space-between;
}
.alert .close { cursor: pointer; font-size: 16px; }

.store-hint { font-size: 12px; margin-bottom: 16px; }
.center { text-align: center; padding: 40px; }
.empty { text-align: center; padding: 48px; }
.empty p { margin-bottom: 8px; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--gap);
}
.plugin-card { display: flex; flex-direction: column; gap: 12px; transition: border-color 0.15s; }
.plugin-card:hover { border-color: var(--border-light); }
.plugin-card.err { border-color: var(--danger-dim); }

.card-head { display: flex; align-items: flex-start; justify-content: space-between; }
.card-title { display: flex; flex-direction: column; gap: 6px; }
.name { font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; }

.badge-official {
  font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 10px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff; letter-spacing: 0.5px;
}

.store-title { display: flex; align-items: center; gap: 10px; }
.store-icon { width: 38px; height: 38px; border-radius: 8px; object-fit: cover; flex-shrink: 0; }
.store-icon-fallback { object-fit: contain; padding: 4px; background: var(--bg-elevated); }
.store-card:hover { border-color: var(--accent-dim); }

.desc { color: var(--text-secondary); font-size: 13px; min-height: 38px; }
.err-msg {
  background: var(--danger-dim); color: var(--danger);
  padding: 8px 10px; border-radius: var(--radius-sm);
  font-size: 12px; font-family: monospace;
}

.card-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: auto; }
.meta-item {
  font-size: 11px; color: var(--text-muted);
  background: var(--bg-elevated); padding: 2px 8px; border-radius: 4px;
}

.card-actions { display: flex; gap: 8px; margin-top: 4px; }
.btn.sm { padding: 6px 12px; font-size: 12px; }

.drag-overlay {
  position: fixed; inset: 0;
  background: rgba(48, 128, 240, 0.12);
  border: 2px dashed var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; color: var(--accent);
  z-index: 100; pointer-events: none;
}

.modal-mask {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
.modal { width: 540px; max-width: 90vw; max-height: 85vh; overflow-y: auto; }
.modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.modal-head h2 { font-size: 16px; }
.modal-head .close { cursor: pointer; font-size: 22px; color: var(--text-muted); }
.modal-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 24px; }
.form { display: flex; flex-direction: column; gap: 16px; }
.form .field { display: flex; flex-direction: column; gap: 8px; }
.form .field label { font-size: 13px; color: var(--text-secondary); }
.row.between { display: flex; align-items: center; justify-content: space-between; }
.hint { font-size: 12px; }
.repo-row { display: flex; gap: 8px; margin-bottom: 8px; }
.repo-row .input { flex: 1; }
.repo-row .repo-token { max-width: 180px; flex: 0 0 auto; }
.acct-row { display: flex; align-items: center; gap: 8px; font-size: 14px; cursor: pointer; }
.acct-list { display: flex; flex-direction: column; gap: 8px; max-height: 240px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px; }
.acct-item { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }
.acct-item .small { margin-left: auto; }

/* 手机适配 */
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; gap: 12px; }
  .tabs { width: 100%; }
  .tab { flex: 1; justify-content: center; padding: 9px 8px; }
  .grid { grid-template-columns: 1fr; }
  .repo-row { flex-wrap: wrap; }
  .repo-row .repo-token { max-width: none; flex: 1 1 100%; }
}
</style>
