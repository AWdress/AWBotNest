<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { confirm } from '../composables/confirm'

const accounts = ref([])
const loading = ref(true)
const error = ref('')
const busy = ref({})

// 登录向导状态
const wizardOpen = ref(false)
const step = ref('phone')        // phone | code | password | done
const form = ref({ session: '', phone: '', code: '', password: '' })
const wizardBusy = ref(false)
const wizardErr = ref('')
const doneInfo = ref(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const d = await api.listAccounts()
    accounts.value = d.accounts
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function toggle(acc) {
  busy.value[acc.session] = true
  error.value = ''
  try {
    if (acc.online) await api.accountOffline(acc.session)
    else await api.accountOnline(acc.session)
    await load()
  } catch (e) {
    error.value = `${acc.name}: ${e.message}`
  } finally {
    busy.value[acc.session] = false
  }
}

async function remove(acc) {
  const ok = await confirm({
    title: '删除账号',
    message: `确定删除账号「${acc.name}」？\n会断开连接、删除 session 文件并从配置移除，不可恢复。`,
    confirmText: '删除', danger: true,
  })
  if (!ok) return
  busy.value[acc.session] = true
  error.value = ''
  try {
    await api.deleteAccount(acc.session)
    await load()
  } catch (e) {
    error.value = `${acc.name}: ${e.message}`
  } finally {
    busy.value[acc.session] = false
  }
}

// ── 登录向导 ──
function openWizard() {
  form.value = { session: '', phone: '', code: '', password: '' }
  step.value = 'phone'
  wizardErr.value = ''
  doneInfo.value = null
  wizardOpen.value = true
}

async function sendCode() {
  if (!form.value.session.trim()) { wizardErr.value = '请填写账号标识（session 名）'; return }
  if (!form.value.phone.trim()) { wizardErr.value = '请填写手机号'; return }
  wizardBusy.value = true; wizardErr.value = ''
  try {
    await api.loginSendCode(form.value.session.trim(), form.value.phone.trim())
    step.value = 'code'
  } catch (e) {
    wizardErr.value = e.message
  } finally {
    wizardBusy.value = false
  }
}

async function submitCode() {
  if (!form.value.code.trim()) { wizardErr.value = '请输入验证码'; return }
  wizardBusy.value = true; wizardErr.value = ''
  try {
    const r = await api.loginSubmitCode(form.value.session.trim(), form.value.code.trim())
    if (r.need === 'password') step.value = 'password'
    else if (r.ok) { doneInfo.value = r; step.value = 'done'; await load() }
  } catch (e) {
    wizardErr.value = e.message
  } finally {
    wizardBusy.value = false
  }
}

async function submitPassword() {
  if (!form.value.password) { wizardErr.value = '请输入两步验证密码'; return }
  wizardBusy.value = true; wizardErr.value = ''
  try {
    const r = await api.loginSubmitPassword(form.value.session.trim(), form.value.password)
    if (r.ok) { doneInfo.value = r; step.value = 'done'; await load() }
  } catch (e) {
    wizardErr.value = e.message
  } finally {
    wizardBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="toolbar">
      <div class="muted">用户账号登录、上线/下线。登录后已启用的插件会自动挂到新账号上。</div>
      <div class="row gap">
        <button class="btn" @click="load">刷新</button>
        <button class="btn btn-primary" @click="openWizard">+ 登录新账号</button>
      </div>
    </div>

    <div v-if="error" class="alert">{{ error }} <span @click="error=''" class="close"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span></div>

    <div v-if="loading" class="center muted">加载中…</div>

    <div v-else-if="accounts.length === 0" class="card center">
      <p>还没有账号。</p>
      <p class="muted">点右上角「登录新账号」开始。</p>
    </div>

    <div v-else class="grid">
      <div v-for="acc in accounts" :key="acc.session" class="card acc-card">
        <div class="acc-head">
          <div>
            <div class="acc-name">{{ acc.name }}</div>
            <div class="acc-meta mono">
              {{ acc.session }}<template v-if="acc.tgid"> · {{ acc.tgid }}</template>
            </div>
            <div class="acc-status-row">
              <span class="health-pill" :class="`health-${acc.health || 'info'}`">{{ acc.status_text || (acc.online ? '在线' : '离线') }}</span>
              <span class="muted small">{{ acc.session_exists ? '已有 session' : '还未完成登录' }}</span>
            </div>
          </div>
          <span class="badge" :class="acc.online ? 'badge-on' : 'badge-off'">
            {{ acc.online ? '在线' : '离线' }}
          </span>
        </div>
        <div class="acc-actions">
          <button class="btn sm" :class="{ 'btn-primary': !acc.online }"
                  @click="toggle(acc)" :disabled="busy[acc.session]">
            {{ acc.online ? '下线' : '上线' }}
          </button>
          <button class="btn sm btn-danger" @click="remove(acc)" :disabled="busy[acc.session]">
            删除
          </button>
        </div>
      </div>
    </div>

    <!-- 登录向导 -->
    <div v-if="wizardOpen" class="modal-mask" @click.self="wizardOpen=false">
      <div class="modal card">
        <div class="modal-head">
          <h2>登录账号</h2>
          <span class="close" @click="wizardOpen=false"><svg class="x-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></span>
        </div>

        <!-- 步骤指示 -->
        <div class="steps">
          <span :class="{ active: step==='phone', done: step!=='phone' }">手机号</span>
          <span class="arr">→</span>
          <span :class="{ active: step==='code', done: ['password','done'].includes(step) }">验证码</span>
          <span class="arr">→</span>
          <span :class="{ active: step==='password', done: step==='done' }">两步验证<small>（可选）</small></span>
        </div>

        <div v-if="wizardErr" class="alert">{{ wizardErr }}</div>

        <!-- 手机号 -->
        <div v-if="step==='phone'" class="form">
          <div class="field">
            <label>账号标识（session 名，英文，自定义）</label>
            <input class="input" v-model="form.session" placeholder="如 user_account" />
          </div>
          <div class="field">
            <label>手机号（带国家码）</label>
            <input class="input" v-model="form.phone" placeholder="+8615012345678" />
          </div>
          <div class="modal-foot">
            <button class="btn" @click="wizardOpen=false">取消</button>
            <button class="btn btn-primary" @click="sendCode" :disabled="wizardBusy">
              {{ wizardBusy ? '发送中…' : '发送验证码' }}
            </button>
          </div>
        </div>

        <!-- 验证码 -->
        <div v-else-if="step==='code'" class="form">
          <div class="field">
            <label>验证码（Telegram 发来的 6 位数字）</label>
            <input class="input" v-model="form.code" placeholder="12345" />
          </div>
          <div class="modal-foot">
            <button class="btn" @click="step='phone'">上一步</button>
            <button class="btn btn-primary" @click="submitCode" :disabled="wizardBusy">
              {{ wizardBusy ? '验证中…' : '确认' }}
            </button>
          </div>
        </div>

        <!-- 两步密码 -->
        <div v-else-if="step==='password'" class="form">
          <p class="muted hint-text">该账号开启了两步验证（2FA），请输入你设置的两步验证密码。<br>普通账号不会走到这一步。</p>
          <div class="field">
            <label>两步验证密码</label>
            <input class="input" type="password" v-model="form.password" @keyup.enter="submitPassword" />
          </div>
          <div class="modal-foot">
            <button class="btn btn-primary" @click="submitPassword" :disabled="wizardBusy">
              {{ wizardBusy ? '验证中…' : '完成登录' }}
            </button>
          </div>
        </div>

        <!-- 完成 -->
        <div v-else-if="step==='done'" class="done">
          <div class="done-icon"></div>
          <p><b>{{ doneInfo?.name }}</b> 登录成功并已上线</p>
          <p class="muted mono">{{ doneInfo?.session }}<template v-if="doneInfo?.tgid"> · {{ doneInfo.tgid }}</template></p>
          <button class="btn btn-primary" @click="wizardOpen=false">完成</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.center { text-align: center; padding: 40px; }
.alert { background: var(--danger-dim); color: var(--danger); padding: 10px 14px; border-radius: var(--radius-sm); margin-bottom: 16px; display: flex; justify-content: space-between; }
.alert .close { cursor: pointer; display: inline-flex; align-items: center; }
.close .x-ico { width: 16px; height: 16px; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--gap); }
.acc-card { min-height: 176px; display: flex; flex-direction: column; gap: 16px; }
.acc-head { display: flex; justify-content: space-between; align-items: flex-start; }
.acc-name { font-size: 15px; font-weight: 600; }
.acc-meta { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.acc-status-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.small { font-size: 12px; }
.health-pill {
  display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 600;
}
.health-ok { background: var(--accent-2-dim); color: var(--accent-2); }
.health-warn { background: var(--danger-dim); color: var(--danger); }
.health-info { background: var(--accent-dim); color: var(--accent); }
.acc-actions { display: flex; gap: 8px; margin-top: auto; }
.btn.sm { padding: 6px 14px; font-size: 12px; }

.modal-mask { position: fixed; inset: 0; background: rgba(3,6,12,.7); backdrop-filter: blur(5px); display: flex; align-items: center; justify-content: center; z-index: 200; }
.modal { --modal-pad: var(--gap-lg); width: 440px; max-width: 90vw; max-height: 86vh; overflow-y: auto; box-shadow: var(--shadow-float); }
.modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.modal-head h2 { font-size: 16px; }
.modal-head .close { cursor: pointer; font-size: 22px; color: var(--text-muted); display: inline-flex; align-items: center; }
.modal-head .close .x-ico { width: 20px; height: 20px; }
.modal-foot {
  position: sticky; bottom: calc(0px - var(--modal-pad));
  display: flex; justify-content: flex-end; gap: 10px;
  margin: 24px calc(0px - var(--modal-pad)) calc(0px - var(--modal-pad)); padding: 14px var(--modal-pad);
  border-top: 1px solid var(--border); background: rgba(17,19,26,.95); backdrop-filter: blur(16px);
}

.steps { display: flex; align-items: center; gap: 8px; margin-bottom: 20px; font-size: 13px; color: var(--text-muted); }
.steps .active { color: var(--accent); font-weight: 600; }
.steps .done { color: var(--accent-2); }
.steps .arr { color: var(--border-light); }
.steps small { font-size: 11px; opacity: 0.7; }
.hint-text { font-size: 12px; line-height: 1.6; }

.form { display: flex; flex-direction: column; gap: 16px; }
.field { display: flex; flex-direction: column; gap: 8px; }
.field label { font-size: 13px; color: var(--text-secondary); }

.done { text-align: center; padding: 20px 0; display: flex; flex-direction: column; align-items: center; gap: 10px; }
.done-icon { width: 48px; height: 48px; border-radius: 50%; background: var(--accent-2-dim); color: var(--accent-2); display: flex; align-items: center; justify-content: center; font-size: 24px; }
.done .btn { margin-top: 12px; }

/* 手机适配 */
@media (max-width: 768px) {
  .grid { grid-template-columns: 1fr; }
  .modal-mask { align-items: flex-end; }
  .modal { --modal-pad: 18px; width: 100%; max-width: 100%; max-height: 92dvh; border-radius: 18px 18px 0 0; padding: 20px 18px calc(18px + env(safe-area-inset-bottom)); }
  .steps { gap: 5px; font-size: 12px; }
  .steps small { display: none; }
}
</style>
