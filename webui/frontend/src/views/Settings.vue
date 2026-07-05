<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { toast } from '../composables/toast'

const tab = ref('login')   // login | telegram | bots | web | proxy | db

const TABS = [
  { key: 'login',    label: '控制台登录' },
  { key: 'telegram', label: 'Telegram 凭据' },
  { key: 'bots',     label: '通知' },
  { key: 'web',      label: 'Web 控制台' },
  { key: 'proxy',    label: '运行代理' },
  { key: 'db',       label: '数据库' },
]

const s = ref(null)
const loading = ref(true)
const saving = ref(false)
const err = ref('')
const ok = ref('')

async function load() {
  loading.value = true; err.value = ''
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
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

async function save() {
  saving.value = true; err.value = ''; ok.value = ''
  try {
    const r = await api.saveSettings(s.value)
    ok.value = r.restart_required
      ? '已保存。凭据/代理/数据库/新增 Bot 等改动需重启平台生效。'
      : '已保存。'
    // Bot 列表可能变化 → 刷新推送路由的可选项
    if (tab.value === 'bots') loadRouting()
  } catch (e) { err.value = e.message } finally { saving.value = false }
}

// ── 多 Bot（额外 Bot 增删） ──
function addBot() {
  s.value.BOTS.push({ id: 'bot_' + Date.now().toString(36), name: '', token: '' })
}
function removeBot(i) { s.value.BOTS.splice(i, 1) }

// ── 平台 Webhook（密钥 + 随机按钮 + 地址展示） ──
function randomHex(bytesLen = 24) {
  const bytes = new Uint8Array(bytesLen)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}
function genWebhookSecret() { s.value.WEBHOOK_SECRET = randomHex(24) }
const platformWebhookUrl = computed(() => {
  if (!s.value?.WEBHOOK_SECRET) return ''
  return `${location.origin}/api/v1/webhook?apikey=${s.value.WEBHOOK_SECRET}`
})
async function copyPlatformWebhook() {
  if (!platformWebhookUrl.value) return
  try { await navigator.clipboard.writeText(platformWebhookUrl.value); toast.success('已复制平台 webhook 地址') }
  catch { toast.error('复制失败，请手动选择复制') }
}

// ── 通知推送路由（哪个插件推到哪个 Bot） ──
const routing = ref({ bots: [], plugins: [] })
const routingLoading = ref(false)

async function loadRouting() {
  routingLoading.value = true
  try { routing.value = await api.getBotsRouting() }
  catch (e) { toast.error('加载推送路由失败：' + e.message) }
  finally { routingLoading.value = false }
}

async function saveRouting(p) {
  try {
    await api.setBotRouting(p.id, p.bot || '')
    toast.success(`「${p.name}」推送 Bot 已更新`)
  } catch (e) { toast.error('保存失败：' + e.message); loadRouting() }
}

function goTab(k) {
  tab.value = k
  if (k === 'bots' && routing.value.plugins.length === 0) loadRouting()
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

onMounted(() => { load(); loadUsername() })
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
        <button class="btn" @click="load">重置</button>
        <button class="btn btn-primary" @click="save" :disabled="saving">{{ saving ? '保存中…' : '保存设置' }}</button>
      </div>
    </div>

    <div v-if="loading" class="muted center">加载中…</div>
    <div v-else-if="s" class="panel">
      <div v-if="err" class="alert err">{{ err }}</div>
      <div v-if="ok" class="alert ok">{{ ok }}</div>

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
      <div v-show="tab === 'telegram'" class="card">
        <div class="card-title">Telegram 凭据</div>
        <div class="hint muted">从 my.telegram.org 获取 API_ID / API_HASH。Bot Token 在「通知」页配置。敏感值显示为打码，不改就留着。</div>
        <div class="grid2">
          <div class="field"><label>API ID</label>
            <input class="input" type="number" v-model.number="s.API_ID" /></div>
          <div class="field"><label>API HASH</label>
            <input class="input" v-model="s.API_HASH" /></div>
        </div>
      </div>

      <!-- 通知（默认 Bot + 额外 Bot + 推送路由） -->
      <div v-show="tab === 'bots'" class="card">
        <div class="card-title">通知</div>
        <div class="hint muted">
          平台的插件通知（ctx.notify）与 bot 类插件都通过 Bot 发送。你可以配置多个 Bot，
          再在下方指定「每个插件推送到哪个 Bot」。默认 Bot 从 @BotFather 获取 Token；新增/删除 Bot 需重启平台生效。
        </div>

        <!-- 默认 Bot -->
        <div class="field">
          <label>默认 Bot · BOT TOKEN</label>
          <input class="input" v-model="s.BOT_TOKEN" placeholder="从 @BotFather 获取" />
        </div>

        <!-- 额外 Bot 列表 -->
        <div class="field" style="margin-top:6px">
          <label>额外 Bot</label>
          <div class="hint muted small" style="margin-bottom:8px">给不同插件分流通知时使用。名称仅用于识别，Token 从 @BotFather 获取。</div>
          <div v-for="(b, i) in s.BOTS" :key="i" class="bot-row">
            <input class="input bot-name" v-model="b.name" placeholder="名称（如 订单Bot）" />
            <input class="input" v-model="b.token" placeholder="Bot Token" />
            <button class="btn sm danger" @click="removeBot(i)">删除</button>
          </div>
          <button class="btn sm" @click="addBot">+ 添加 Bot</button>
        </div>

        <!-- 推送路由 -->
        <div class="field" style="margin-top:10px">
          <label>推送路由（哪个插件推到哪个 Bot）</label>
          <div class="hint muted small" style="margin-bottom:8px">选择每个插件的通知发到哪个 Bot；选择立即生效（无需保存设置）。默认 = 默认 Bot。</div>
          <div v-if="routingLoading" class="muted small">加载中…</div>
          <div v-else-if="routing.plugins.length === 0" class="muted small">还没有插件。</div>
          <div v-else class="route-table">
            <div v-for="p in routing.plugins" :key="p.id" class="route-row">
              <span class="route-name" :title="p.id">{{ p.name }}</span>
              <select class="select route-sel" v-model="p.bot" @change="saveRouting(p)">
                <option value="">默认 Bot</option>
                <option v-for="b in routing.bots.filter(x => !x.is_default)" :key="b.id" :value="b.id">
                  {{ b.name }}{{ b.online ? '' : '（离线）' }}
                </option>
              </select>
            </div>
          </div>
        </div>

        <!-- 平台 Webhook -->
        <div class="field" style="margin-top:10px">
          <label>平台 Webhook</label>
          <div class="hint muted small" style="margin-bottom:8px">
            外部服务 POST 到下面的地址（JSON 可含 text/title/category 字段，或直接发文本），
            平台会把内容作为通知推送给管理员。留空密钥=关闭。改动随「保存设置」生效。
          </div>
          <div class="row gap">
            <input class="input" v-model="s.WEBHOOK_SECRET" placeholder="点右侧随机生成，或自定义密钥（≥8 位）" />
            <button class="btn sm" @click="genWebhookSecret" title="随机生成密钥">🎲 随机</button>
          </div>
          <div v-if="platformWebhookUrl" class="webhook-url mono">{{ platformWebhookUrl }}</div>
          <button v-if="platformWebhookUrl" class="btn sm" style="align-self:flex-start" @click="copyPlatformWebhook">复制地址</button>
        </div>
      </div>

      <!-- Web 控制台 -->
      <div v-show="tab === 'web'" class="card">
        <div class="card-title">Web 控制台</div>
        <div class="grid2">
          <div class="field"><label>监听端口 (WEB_UI_PORT)</label>
            <input class="input" type="number" v-model.number="s.WEB_UI_PORT" /></div>
          <div class="field"><label>外部地址 (WEB_UI_URL，可空)</label>
            <input class="input" v-model="s.WEB_UI_URL" /></div>
        </div>
        <div class="row between">
          <span>启用 ngrok 公网映射</span>
          <div class="toggle" :class="{ on: s.NGROK_ENABLE }" @click="s.NGROK_ENABLE = !s.NGROK_ENABLE"></div>
        </div>
        <div class="field" v-if="s.NGROK_ENABLE"><label>ngrok Token</label>
          <input class="input" v-model="s.NGROK_TOKEN" /></div>
      </div>

      <!-- 运行代理 -->
      <div v-show="tab === 'proxy'" class="card">
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
        </template>
        <div class="field" style="margin-top:14px">
          <label>pip 镜像源 (插件装依赖用)</label>
          <input class="input" v-model="s.PIP_INDEX_URL"
                 placeholder="https://pypi.tuna.tsinghua.edu.cn/simple" />
          <div class="hint muted small">墙内建议填国内镜像（清华/阿里），境内直连不经墙。留空则走官方 pypi（此时若启用了上面的代理会自动用代理出墙）。</div>
        </div>
      </div>

      <!-- 数据库 -->
      <div v-show="tab === 'db'" class="card">
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
            <div class="field"><label>密码</label><input class="input" type="password" v-model="s.DB_INFO.password" /></div>
          </div>
        </template>
      </div>

      <div class="hint muted foot" v-if="tab === 'telegram'">提示：账号登录在「账号管理」页完成，账号列表会随登录自动写入。</div>
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

.panel { max-width: 760px; }
.center { text-align: center; padding: 40px; }
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

/* 通知：额外 Bot 行 + 推送路由表 */
.bot-row { display: flex; gap: 8px; margin-bottom: 8px; }
.bot-row .input { flex: 1; }
.bot-row .bot-name { max-width: 200px; flex: 0 0 auto; }
.btn.sm { padding: 6px 12px; font-size: 13px; }
.btn.sm.danger { color: var(--danger); }
.route-table { display: flex; flex-direction: column; gap: 8px; }
.route-row { display: flex; align-items: center; gap: 10px; }
.route-name { flex: 1; font-size: 13px; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.route-sel { max-width: 220px; flex: 0 0 auto; }
.small { font-size: 12px; }

/* 平台 webhook 地址展示 */
.row.gap { display: flex; align-items: center; gap: 8px; }
.row.gap .input { flex: 1; }
.webhook-url {
  margin-top: 8px; font-size: 12px; word-break: break-all; padding: 8px 10px;
  background: #07090f; border-radius: var(--radius-sm); color: var(--text-primary);
}
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }
@media (max-width: 600px) { .grid2 { grid-template-columns: 1fr; } }

/* 手机适配 */
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; gap: 12px; }
  /* tab 全部换行展示，避免靠右的「数据库」被裁掉看不全 */
  .tabs { flex-wrap: wrap; }
  .tab { white-space: nowrap; flex: 1 1 auto; justify-content: center; padding: 8px 12px; }
  .panel { max-width: 100%; }
}
</style>
