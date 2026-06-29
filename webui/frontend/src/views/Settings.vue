<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const tab = ref('login')   // login | telegram | web | proxy | db

const TABS = [
  { key: 'login',    label: '控制台登录' },
  { key: 'telegram', label: 'Telegram 凭据' },
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
    s.value.DB_INFO = s.value.DB_INFO || {}
    s.value.ACCOUNTS = s.value.ACCOUNTS || []
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

async function save() {
  saving.value = true; err.value = ''; ok.value = ''
  try {
    const r = await api.saveSettings(s.value)
    ok.value = r.restart_required
      ? '已保存。凭据/代理/数据库等改动需重启平台生效。'
      : '已保存。'
  } catch (e) { err.value = e.message } finally { saving.value = false }
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
                @click="tab = t.key">{{ t.label }}</button>
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
        <div class="hint muted">从 my.telegram.org 获取 API_ID / API_HASH；BOT_TOKEN 从 @BotFather 获取。敏感值显示为打码，不改就留着。</div>
        <div class="grid2">
          <div class="field"><label>API ID</label>
            <input class="input" type="number" v-model.number="s.API_ID" /></div>
          <div class="field"><label>API HASH</label>
            <input class="input" v-model="s.API_HASH" /></div>
        </div>
        <div class="field"><label>BOT TOKEN</label>
          <input class="input" v-model="s.BOT_TOKEN" /></div>
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
