<script setup>
import { useRoute, useRouter } from 'vue-router'
import { ref, onMounted } from 'vue'
import { api, getToken, setToken, setUnauthorizedHandler } from './api'
import Login from './views/Login.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import Toast from './components/Toast.vue'
import logoWhite from './assets/logo-white.png'
import { confirm } from './composables/confirm'
import { toast } from './composables/toast'

const route = useRoute()
const router = useRouter()
const online = ref(false)
const version = ref('')
const latestVersion = ref('')   // GitHub 最新发布版本
const latestNote = ref('')      // 新版本的一句更新说明（hover 提示用）
const hasUpdate = ref(false)    // 是否有新版本
const RELEASE_URL = 'https://github.com/AWdress/AWBotNest/releases/latest'

// 鉴权门：未登录显示 Login，登录后显示主界面
const authed = ref(false)
const mustChangePwd = ref(false)   // 仍是默认密码：强制改密后才放进主界面
const npwd = ref('')
const npwd2 = ref('')
const pwdBusy = ref(false)
const pwdErr = ref('')

async function onAuthed() {
  authed.value = true
  try {
    const st = await api.authStatus()
    mustChangePwd.value = !!st.must_change_password
  } catch { /* ignore */ }
  ping()
  checkUpdate()
}
function logout() { setToken(''); authed.value = false; mustChangePwd.value = false }

async function submitNewPwd() {
  pwdErr.value = ''
  if (!npwd.value || npwd.value.length < 4) { pwdErr.value = '新密码至少 4 位'; return }
  if (npwd.value !== npwd2.value) { pwdErr.value = '两次输入不一致'; return }
  pwdBusy.value = true
  try {
    // 首次强制改密：旧密码就是默认 password，用户名保持不变
    await api.changeCredentials('password', '', npwd.value)
    // 改密后令牌失效，需重新登录
    setToken('')
    mustChangePwd.value = false
    authed.value = false
    toast.success('密码已修改，请用新密码重新登录')
  } catch (e) { pwdErr.value = e.message }
  finally { pwdBusy.value = false }
}

const restarting = ref(false)
async function restart() {
  const ok = await confirm({
    title: '重启平台',
    message: '确定重启平台？重启期间控制台会短暂不可用，约十几秒后自动恢复。',
    confirmText: '重启', danger: true,
  })
  if (!ok) return
  restarting.value = true
  try {
    await api.restartPlatform()
    toast.success('平台正在重启，请稍候刷新页面')
    // 轮询直到平台重新可用，自动刷新
    let tries = 0
    const timer = setInterval(async () => {
      tries++
      try { await api.status(); clearInterval(timer); location.reload() }
      catch { if (tries > 30) clearInterval(timer) }
    }, 2000)
  } catch (e) {
    toast.error('重启请求失败：' + e.message)
    restarting.value = false
  }
}
setUnauthorizedHandler(() => { authed.value = false })

// 刷新后恢复登录态：localStorage 有令牌就乐观进主界面，
// 令牌失效时 ping() 的受保护接口会返回 401，由上面的 unauthorized 回调踢回登录页。
if (getToken()) { authed.value = true }

async function ping() {
  try {
    const s = await api.status()
    online.value = true
    version.value = s.version || ''
    // 查更新走独立的低频节奏，不跟 10 秒心跳，否则会打满 GitHub 未鉴权限流(60次/小时)
  } catch { online.value = false }
}

// 把 "v1.2.3"/"1.2.3" 转成可比较的数字数组
function parseVer(v) {
  return String(v).replace(/^v/i, '').split('.').map((x) => parseInt(x, 10) || 0)
}
function isNewer(remote, local) {
  const a = parseVer(remote), b = parseVer(local)
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if ((a[i] || 0) > (b[i] || 0)) return true
    if ((a[i] || 0) < (b[i] || 0)) return false
  }
  return false
}

// 查 GitHub 最新发布版本，与当前对比（失败静默，不影响使用）
// 注意：GitHub 未鉴权接口限流 60 次/小时/IP，必须低频调用，不能跟随心跳
async function checkUpdate() {
  if (!version.value) {
    // 还没拿到本地版本就先取一次，避免 onMounted 时序导致跳过
    try { const s = await api.status(); version.value = s.version || '' } catch { return }
    if (!version.value) return
  }
  try {
    const r = await fetch('https://api.github.com/repos/AWdress/AWBotNest/releases/latest', {
      headers: { Accept: 'application/vnd.github+json' },
    })
    if (!r.ok) return
    const data = await r.json()
    const tag = data.tag_name || ''
    if (tag) {
      latestVersion.value = tag.replace(/^v/i, '')
      hasUpdate.value = isNewer(latestVersion.value, version.value)
      // 取一句更新说明：优先 release 标题（非纯版本号），否则取正文首个非空行，去掉 markdown 记号
      let note = (data.name || '').trim()
      if (!note || /^v?[\d.]+$/i.test(note)) {
        note = ((data.body || '').split(/\r?\n/).map(l => l.trim()).find(Boolean) || '')
          .replace(/^[#>\-*\s]+/, '').replace(/\*\*/g, '').trim()
      }
      latestNote.value = note.slice(0, 80)
    }
  } catch { /* 离线/限流忽略 */ }
}

// 导航项：内联 SVG 图标 + 文字
const nav = [
  { to: '/status', label: '系统状态', icon: 'pulse' },
  { to: '/plugins', label: '插件管理', icon: 'grid' },
  { to: '/accounts', label: '账号管理', icon: 'user' },
  { to: '/logs', label: '运行日志', icon: 'list' },
  { to: '/settings', label: '系统设置', icon: 'gear' },
]

const icons = {
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z',
  user: 'M12 12a5 5 0 100-10 5 5 0 000 10zM4 21a8 8 0 0116 0',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  pulse: 'M3 12h4l3 8 4-16 3 8h4',
  gear: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z',
}

onMounted(() => {
  if (authed.value) { ping(); checkUpdate() }   // 恢复登录态后立即校验令牌 + 查一次更新
  setInterval(() => { if (authed.value) ping() }, 10000)
  // 查更新独立低频：每 6 小时一次，避免打满 GitHub 限流
  setInterval(() => { if (authed.value) checkUpdate() }, 6 * 3600 * 1000)
})
</script>

<template>
  <Login v-if="!authed" @authed="onAuthed" />

  <!-- 强制首次改密：仍是默认密码时，必须改密才能进主界面 -->
  <div v-else-if="mustChangePwd" class="force-bg">
    <div class="force-card">
      <img :src="logoWhite" class="force-logo" alt="" />
      <div class="force-title">首次使用，请修改默认密码</div>
      <div class="force-sub">为安全起见，必须先设置新密码才能使用控制台。</div>
      <div v-if="pwdErr" class="force-alert">{{ pwdErr }}</div>
      <input class="force-input" type="password" v-model="npwd" placeholder="新密码（至少 4 位）" @keyup.enter="submitNewPwd" />
      <input class="force-input" type="password" v-model="npwd2" placeholder="再次输入新密码" @keyup.enter="submitNewPwd" />
      <button class="force-btn" @click="submitNewPwd" :disabled="pwdBusy">{{ pwdBusy ? '提交中…' : '设置新密码' }}</button>
      <div class="force-foot" @click="logout">退出登录</div>
    </div>
  </div>

  <div v-else class="layout">
    <!-- 侧边栏 -->
    <aside class="sidebar">
      <div class="brand">
        <img :src="logoWhite" class="logo-img" alt="AWBotNest" />
        <div class="brand-text">
          <div class="brand-name">AWBotNest</div>
          <div class="brand-sub">插件化机器人平台</div>
        </div>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path === item.to }"
        >
          <svg class="nav-icon" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path :d="icons[item.icon]" />
          </svg>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <div class="foot-row">
          <span class="ver" v-if="version">
            v{{ version }}
            <span v-if="hasUpdate" class="update-wrap">
              <a :href="RELEASE_URL" target="_blank" rel="noopener" class="update-arrow">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 19V5M5 12l7-7 7 7" />
                </svg>
              </a>
              <div class="update-pop">
                <div class="update-pop-head">
                  <span class="update-pop-title">发现新版本</span>
                  <span class="update-pop-ver">v{{ latestVersion }}</span>
                </div>
                <div class="update-pop-note" v-if="latestNote">{{ latestNote }}</div>
              </div>
            </span>
          </span>
          <div class="footer-status">
            <div class="status-dot" :class="{ online }"></div>
            <span class="muted">{{ online ? '在线' : '连接中…' }}</span>
          </div>
        </div>
        <button class="restart-btn" @click="restart" :disabled="restarting">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" class="logout-icon">
            <path d="M23 4v6h-6M1 20v-6h6" />
            <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
          </svg>
          {{ restarting ? '重启中…' : '重启平台' }}
        </button>
        <button class="logout-btn" @click="logout">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" class="logout-icon">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
          </svg>
          退出登录
        </button>
      </div>
    </aside>

    <!-- 主区 -->
    <main class="main">
      <header class="topbar">
        <img :src="logoWhite" class="topbar-logo" alt="" />
        <h1>{{ route.meta.title || '控制台' }}</h1>
        <div class="topbar-actions">
          <button class="icon-btn" @click="restart" :disabled="restarting" title="重启平台">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M23 4v6h-6M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
            </svg>
          </button>
          <button class="icon-btn" @click="logout" title="退出登录">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
            </svg>
          </button>
        </div>
      </header>
      <div class="content">
        <RouterView />
      </div>
    </main>

    <!-- 手机底部标签栏（仅窄屏显示） -->
    <nav class="tabbar">
      <RouterLink v-for="item in nav" :key="item.to" :to="item.to"
                  class="tab-item" :class="{ active: route.path === item.to }">
        <svg class="tab-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path :d="icons[item.icon]" />
        </svg>
        <span>{{ item.label }}</span>
      </RouterLink>
    </nav>
  </div>

  <!-- 全局确认弹窗 -->
  <ConfirmDialog />

  <!-- 全局悬浮提示 -->
  <Toast />
</template>

<style scoped>
.layout { display: flex; height: 100vh; overflow: hidden; }

.sidebar {
  width: var(--sidebar-width);
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 20px 14px;
  flex-shrink: 0;
}

.brand { display: flex; align-items: center; gap: 12px; padding: 10px 10px 24px; }
.logo-img { width: 40px; height: 40px; object-fit: contain; flex-shrink: 0; }
.brand-name {
  font-weight: 700; font-size: 19px; color: #fff;
  letter-spacing: 0.5px; line-height: 1.1;
  font-family: 'Segoe UI', system-ui, sans-serif;
  text-shadow: 0 1px 8px rgba(48,128,240,0.35);
}
.brand-sub { font-size: 11px; color: var(--text-muted); margin-top: 3px; letter-spacing: 0.5px; }

.nav { display: flex; flex-direction: column; gap: 4px; flex: 1; }
.nav-item {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  color: #ffffff;
  transition: all 0.15s ease;
  font-size: 14px;
  font-weight: 600;
}
.nav-item:hover { background: var(--bg-hover); color: #ffffff; }
.nav-item.active {
  background: var(--accent-dim);
  color: var(--accent);
}
.nav-icon { width: 18px; height: 18px; flex-shrink: 0; }

.sidebar-footer {
  display: flex; flex-direction: column; gap: 10px;
  padding: 14px 6px 4px;
  border-top: 1px solid var(--border);
  font-size: 12px;
}
.foot-row { display: flex; align-items: center; justify-content: space-between; padding: 0 6px; }
.footer-status { display: flex; align-items: center; gap: 8px; }
.ver { color: var(--text-muted); font-size: 11px; font-family: monospace; display: inline-flex; align-items: center; gap: 6px; }
.update-arrow {
  display: inline-flex; align-items: center; gap: 3px; text-decoration: none;
  color: var(--success); font-size: 10px; font-weight: 600;
  animation: update-pulse 2s ease-in-out infinite;
}
.update-arrow svg { width: 13px; height: 13px; }
.update-arrow:hover { opacity: 0.8; }
@keyframes update-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.55; } }
.update-wrap { position: relative; display: inline-flex; align-items: center; }
.update-pop {
  position: absolute; bottom: calc(100% + 8px); left: 0;
  transform: translateY(4px);
  min-width: 176px; max-width: 240px; padding: 10px 12px;
  background: var(--bg-elevated); border: 1px solid var(--border-light);
  border-radius: var(--radius-sm); box-shadow: var(--shadow);
  font-family: initial; z-index: 50;
  opacity: 0; visibility: hidden; pointer-events: none;
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.update-wrap:hover .update-pop {
  opacity: 1; visibility: visible; transform: translateY(0);
}
.update-pop-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.update-pop-title { color: var(--accent); font-size: 12px; font-weight: 600; white-space: nowrap; }
.update-pop-ver {
  font-family: monospace; font-size: 11px; font-weight: 600; color: var(--text-primary);
  background: var(--bg-base); border: 1px solid var(--border-light);
  border-radius: 6px; padding: 1px 7px; white-space: nowrap;
}
.update-pop-note { margin-top: 6px; color: var(--text-secondary); font-size: 11px; line-height: 1.5; }
.logout-btn {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  width: 100%; padding: 9px; cursor: pointer;
  background: transparent; border: 1px solid var(--border-light); border-radius: var(--radius-sm);
  color: var(--text-secondary); font-size: 13px; transition: all 0.15s ease;
}
.restart-btn {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  width: 100%; padding: 9px; margin-bottom: 8px; cursor: pointer;
  background: transparent; border: 1px solid var(--border-light); border-radius: var(--radius-sm);
  color: var(--text-secondary); font-size: 13px; transition: all 0.15s ease;
}
.restart-btn:hover:not(:disabled) { color: var(--accent); border-color: var(--accent); background: var(--accent-dim); }
.restart-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.logout-btn:hover { color: var(--danger); border-color: var(--danger); background: var(--danger-dim); }
.logout-icon { width: 16px; height: 16px; }
.status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-muted);
}
.status-dot.online { background: var(--success); box-shadow: 0 0 8px var(--success); }

.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.topbar {
  height: 64px;
  display: flex; align-items: center;
  padding: 0 32px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.topbar h1 { font-size: 18px; font-weight: 600; }
.content { flex: 1; overflow-y: auto; padding: 32px; }

/* 顶栏 logo / 操作按钮：默认(桌面)隐藏，手机显示 */
.topbar-logo { display: none; width: 28px; height: 28px; object-fit: contain; }
.topbar-actions { display: none; gap: 8px; margin-left: auto; }
.icon-btn {
  display: flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; border-radius: var(--radius-sm);
  background: transparent; border: 1px solid var(--border-light);
  color: var(--text-secondary); cursor: pointer;
}
.icon-btn svg { width: 18px; height: 18px; }
.icon-btn:disabled { opacity: 0.5; }

/* 手机底部标签栏：默认隐藏 */
.tabbar { display: none; }

/* ───────── 手机版（窄屏）───────── */
@media (max-width: 768px) {
  .layout { flex-direction: column; height: 100vh; height: 100dvh; }
  /* 侧边栏隐藏，导航走底部标签栏 */
  .sidebar { display: none; }
  .main { flex: 1; min-height: 0; }
  /* 顶栏:logo + 标题 + 右侧操作 */
  .topbar {
    height: 54px; padding: 0 16px; gap: 10px;
    position: sticky; top: 0; z-index: 10;
    background: var(--bg-sidebar);
  }
  .topbar-logo { display: block; }
  .topbar h1 { font-size: 16px; }
  .topbar-actions { display: flex; }
  /* 内容区留出底部悬浮标签栏高度，避免被遮 */
  .content { padding: 16px 14px calc(86px + env(safe-area-inset-bottom)); }
  /* 底部标签栏：悬浮胶囊，居中不拉满 */
  .tabbar {
    display: flex; position: fixed;
    bottom: calc(12px + env(safe-area-inset-bottom));
    left: 50%; transform: translateX(-50%);
    z-index: 20;
    background: rgba(20, 23, 31, 0.55);
    -webkit-backdrop-filter: blur(20px) saturate(160%); backdrop-filter: blur(20px) saturate(160%);
    border: 1px solid var(--border-light);
    border-radius: 999px;
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
    padding: 5px 6px;
  }
  .tab-item {
    display: flex; flex-direction: column; align-items: center; gap: 2px;
    padding: 6px 12px; color: var(--text-muted);
    font-size: 10px; font-weight: 600; white-space: nowrap;
    border-radius: 999px;
    transition: color 0.15s, background 0.15s;
  }
  .tab-item.active { color: var(--accent); background: var(--accent-dim); }
  .tab-icon { width: 20px; height: 20px; }
}

/* 强制首次改密界面 */
.force-bg { height: 100vh; display: flex; align-items: center; justify-content: center;
  background: radial-gradient(1200px 600px at 50% 0%, #0d1426 0%, #0a0e17 55%, #07090f 100%); }
.force-card { width: 360px; max-width: 90vw; background: rgba(17,19,26,0.95);
  border: 1px solid var(--border-light); border-radius: 16px; padding: 36px 32px;
  display: flex; flex-direction: column; align-items: center; box-shadow: 0 20px 60px rgba(0,0,0,0.5); }
.force-logo { width: 52px; height: 52px; object-fit: contain; margin-bottom: 14px; }
.force-title { font-size: 18px; font-weight: 700; color: #fff; }
.force-sub { font-size: 12px; color: var(--text-muted); margin: 8px 0 18px; text-align: center; }
.force-alert { width: 100%; background: var(--danger-dim); color: var(--danger); padding: 8px 12px;
  border-radius: 8px; font-size: 13px; margin-bottom: 12px; text-align: center; }
.force-input { width: 100%; padding: 11px 14px; margin-bottom: 12px; font-size: 16px;
  background: var(--bg-elevated); border: 1px solid var(--border-light); border-radius: 10px; color: var(--text-primary); }
.force-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.force-btn { width: 100%; padding: 12px; cursor: pointer; background: linear-gradient(135deg,#3080f0,#2566d8);
  color: #fff; border: none; border-radius: 10px; font-size: 15px; font-weight: 600; }
.force-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.force-foot { margin-top: 16px; font-size: 12px; color: var(--text-muted); cursor: pointer; }
.force-foot:hover { color: var(--danger); }
</style>
