<script setup>
import { useRoute, useRouter } from 'vue-router'
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { api, getToken, setToken, setUnauthorizedHandler } from './api'
import Login from './views/Login.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import Toast from './components/Toast.vue'
import TopbarControlCenter from './components/TopbarControlCenter.vue'
import logoWhite from './assets/logo-white.png'
import { confirm } from './composables/confirm'
import { toast } from './composables/toast'
import { clearUiProfile, loadUiProfile } from './composables/uiProfile'
import { clearAccountAvatarCache, preloadAccountAvatars } from './composables/accountAvatars'
import { preloadRoute, scheduleDeferredRoutePreload } from './routePreload'
import {
  platformStatus,
  platformStatusError,
  refreshPlatformStatus,
  startPlatformStatusPolling,
  stopPlatformStatusPolling,
} from './composables/platformStatus'

const route = useRoute()
const router = useRouter()
const online = ref(false)
const version = ref('')
const latestVersion = ref('')   // GitHub 最新发布版本
const latestNote = ref('')      // 新版本的一句更新说明（hover 提示用）
const hasUpdate = ref(false)    // 是否有新版本
const RELEASE_URL = 'https://github.com/AWdress/AWBotNest/releases/latest'
const connectionLabel = computed(() => online.value
  ? '连接正常'
  : (platformStatusError.value ? '连接异常' : '正在连接'))

function applyAppearance() {
  document.documentElement.dataset.theme = localStorage.getItem('awbotnest-theme') || 'dark'
  // 透明主题提供电影质感默认背景；用户填写的图片/API 地址优先。
  const image = localStorage.getItem('awbotnest-bg-image') || (document.documentElement.dataset.theme === 'transparent'
    ? `https://www.loliapi.com/acg/?t=${Date.now()}`
    : '')
  document.documentElement.style.setProperty('--app-bg-image', image ? `url("${image.replace(/"/g, '')}")` : 'none')
  if (image) {
    const preload = new Image()
    preload.src = image
    // 随机图接口偶尔返回网关错误或空响应；重试一次，失败后清除无效背景，
    // 避免保留 broken-image 状态导致后续导航一直没有背景。
    preload.onerror = () => {
      if (!localStorage.getItem('awbotnest-bg-image') && image.includes('loliapi.com')) {
        const retry = `${image.split('?')[0]}?t=${Date.now()}&retry=1`
        document.documentElement.style.setProperty('--app-bg-image', `url("${retry}")`)
        const second = new Image()
        second.onerror = () => document.documentElement.style.setProperty('--app-bg-image', 'none')
        second.src = retry
      } else {
        document.documentElement.style.setProperty('--app-bg-image', 'none')
      }
    }
  }
}

// 鉴权门：未登录显示 Login，登录后显示主界面
const authed = ref(false)
const restoringSession = ref(!!getToken())
let cancelDeferredRoutePreload = null
let restartTimer = null
let appearanceRotationTimer = null

async function onAuthed() {
  restoringSession.value = true
  api.ensureResourceToken().catch(() => {})   // 确保资源 Cookie 就绪（加载 vue 模式插件前端用）
  try {
    const [st, , status] = await Promise.all([
      api.authStatus(),
      loadUiProfile(true),
      refreshPlatformStatus(true),
    ])
    if (st.needs_setup || st.must_change_password) {
      logout()
      return
    }
    // 启动页期间并行准备当前路由与账号头像，避免进入界面后再出现二次加载。
    await Promise.all([
      preloadRoute(route.path),
      preloadAccountAvatars(status?.accounts || []),
    ])
    authed.value = true
    cancelDeferredRoutePreload?.()
    cancelDeferredRoutePreload = scheduleDeferredRoutePreload()
    // 状态轮询会持续运行，不会自然 resolve；更新检查必须立即独立触发，
    // 否则底部和“关于”里的新版本提示永远等不到第一次检查。
    startPlatformStatusPolling().catch(() => {})
    checkUpdate().catch(() => {})
  } catch (error) {
    authed.value = false
    if (getToken()) toast.error(`读取管理员资料失败：${error.message}`)
  } finally {
    restoringSession.value = false
  }
}
function logout() {
  stopPlatformStatusPolling()
  cancelDeferredRoutePreload?.()
  cancelDeferredRoutePreload = null
  setToken('')
  clearUiProfile()
  clearAccountAvatarCache()
  authed.value = false
  restoringSession.value = false
}

const restarting = ref(false)
async function restart() {
  const ok = await confirm({
    title: '重启',
    message: '确定重启？重启期间网页会短暂不可用，约十几秒后自动恢复。',
    confirmText: '重启', danger: true,
  })
  if (!ok) return
  restarting.value = true
  try {
    await api.restartPlatform()
    toast.success('正在重启，请稍候刷新页面')
    // 轮询直到服务重新可用，自动刷新
    let tries = 0
    if (restartTimer) clearInterval(restartTimer)
    restartTimer = setInterval(async () => {
      tries++
      try {
        await api.status(true)
        clearInterval(restartTimer)
        restartTimer = null
        location.reload()
      } catch {
        if (tries > 30) {
          clearInterval(restartTimer)
          restartTimer = null
          restarting.value = false
          toast.error('服务重启超时，请稍后手动刷新页面')
        }
      }
    }, 2000)
  } catch (e) {
    toast.error('重启请求失败：' + e.message)
    restarting.value = false
  }
}
setUnauthorizedHandler(() => {
  stopPlatformStatusPolling()
  clearUiProfile()
  clearAccountAvatarCache()
  authed.value = false
  restoringSession.value = false
})

watch(platformStatus, (status) => {
  if (!status) return
  online.value = true
  version.value = status.version || ''
})

watch(platformStatusError, (message) => {
  if (message) online.value = false
})

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
  // 开发版与同数字的正式版相比仍然是旧版本，例如
  // 2.0.0.0_dev 应提示升级到 2.0.0.0。
  return /(?:_dev|[-.]dev)/i.test(String(local)) && !/(?:_dev|[-.]dev)/i.test(String(remote))
}

// 查 GitHub 最新发布版本，与当前对比（失败静默，不影响使用）
// 注意：GitHub 未鉴权接口限流 60 次/小时/IP，必须低频调用，不能跟随心跳
async function checkUpdate(includeHistory = false) {
  if (!version.value) {
    // 还没拿到本地版本就先取一次，避免 onMounted 时序导致跳过
    try { const s = await refreshPlatformStatus(true); version.value = s.version || '' } catch { return }
    if (!version.value) return
  }
  try {
    const url = includeHistory
      ? 'https://api.github.com/repos/AWdress/AWBotNest/releases?per_page=30'
      : 'https://api.github.com/repos/AWdress/AWBotNest/releases/latest'
    const r = await fetch(url, {
      headers: { Accept: 'application/vnd.github+json' },
      cache: 'no-store',
    })
    if (!r.ok) return []
    const data = await r.json()
    const releases = (Array.isArray(data) ? data : [data])
      .filter(item => item && !item.draft)
      .map(item => {
        const match = String(item.tag_name || '').match(/^v?(\d+(?:\.\d+)+)$/i)
        if (!match) return null
        return {
          version: match[1],
          name: item.name || '',
          notes: String(item.body || '').slice(0, 12000),
          url: item.html_url || '',
          published_at: item.published_at || null,
        }
      })
      .filter(Boolean)
    if (releases[0]) syncVersionCheck(releases[0])
    return releases
  } catch {
    return []
  }
}

function syncVersionCheck(result = {}) {
  const remote = String(result.version || '').replace(/^v/i, '')
  if (!remote) return
  latestVersion.value = remote
  hasUpdate.value = isNewer(remote, version.value)
  let note = String(result.name || '').trim()
  if (!note || /^v?[\d.]+$/i.test(note)) {
    note = (String(result.notes || '').split(/\r?\n/).map(line => line.trim()).find(Boolean) || '')
      .replace(/^[#>\-*\s]+/, '').replace(/\*\*/g, '').trim()
  }
  latestNote.value = note.slice(0, 80)
}

// 导航项：内联 SVG 图标 + 文字
const nav = [
  { to: '/status', label: '运行概览', icon: 'pulse' },
  { to: '/plugins', label: '插件管理', icon: 'grid' },
  { to: '/accounts', label: '账号管理', icon: 'user' },
  { to: '/logs', label: '运行日志', icon: 'list' },
  { to: '/settings', label: '系统设置', icon: 'gear' },
]

const pageContext = {
  '/status': { kicker: '运行总览', title: '运行概览' },
  '/plugins': { kicker: '插件生态', title: '插件管理' },
  '/accounts': { kicker: '会话接入', title: '账号管理' },
  '/logs': { kicker: '诊断中心', title: '运行日志' },
  '/settings': { kicker: '平台配置', title: '系统设置' },
}
const currentPage = computed(() => pageContext[route.path] || {
  kicker: 'AWBotNest',
  title: route.meta.title || 'AWBotNest',
})

const icons = {
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z',
  user: 'M12 12a5 5 0 100-10 5 5 0 000 10zM4 21a8 8 0 0116 0',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  pulse: 'M3 12h4l3 8 4-16 3 8h4',
  gear: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z',
}

let updateTimer = null

onMounted(async () => {
  applyAppearance()
  window.addEventListener('awbotnest-appearance', applyAppearance)
  appearanceRotationTimer = window.setInterval(() => {
    const theme = localStorage.getItem('awbotnest-theme') || 'dark'
    if (theme === 'transparent' && !localStorage.getItem('awbotnest-bg-image')) applyAppearance()
  }, 30 * 60 * 1000)
  // 恢复登录态后补种资源 Cookie，并确认账号已经完成首次设置。
  if (getToken()) {
    await onAuthed()
  }
  if (authed.value) startPlatformStatusPolling().catch(() => {})
  // 查更新独立低频：每 6 小时一次，避免打满 GitHub 限流
  updateTimer = setInterval(() => { if (authed.value) checkUpdate() }, 6 * 3600 * 1000)
})

onUnmounted(() => {
  window.removeEventListener('awbotnest-appearance', applyAppearance)
  if (appearanceRotationTimer) window.clearInterval(appearanceRotationTimer)
  stopPlatformStatusPolling()
  cancelDeferredRoutePreload?.()
  clearInterval(updateTimer)
  if (restartTimer) clearInterval(restartTimer)
})
</script>

<template>
  <div v-if="restoringSession" class="profile-loading-screen" role="status" aria-live="polite">
    <div class="profile-loading-aura" aria-hidden="true"></div>
    <div class="profile-loading-content">
      <div class="profile-loading-mark">
        <span class="profile-loading-ring" aria-hidden="true"></span>
        <img :src="logoWhite" class="profile-loading-logo" alt="AWBotNest" />
      </div>
      <div class="profile-loading-title">
        加载中<span class="profile-loading-dots" aria-hidden="true"><i></i><i></i><i></i></span>
      </div>
      <div class="profile-loading-track" aria-hidden="true"><span></span></div>
    </div>
  </div>

  <Login v-else-if="!authed" @authed="onAuthed" />

  <div v-else class="layout">
    <!-- 侧边栏 -->
    <aside class="sidebar">
      <div class="brand">
        <img :src="logoWhite" class="logo-img" alt="AWBotNest" />
        <div class="brand-text">
          <div class="brand-name">
            <span>AWBotNest</span>
            <span class="brand-badge-v2">V2</span>
          </div>
          <div class="brand-sub">插件化机器人</div>
        </div>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path === item.to }"
          @pointerenter="preloadRoute(item.to)"
          @focus="preloadRoute(item.to)"
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
              <svg class="version-icon" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" stroke-width="1.8"
                   stroke-linecap="round" stroke-linejoin="round">
                <path d="M5 5h13a1 1 0 0 1 1 1v12H6a2 2 0 0 1-2-2V6a1 1 0 0 1 1-1Z" />
                <path d="M6 15h13" />
              </svg>
              v{{ version }}
              <span v-if="hasUpdate" class="update-wrap">
                <a :href="RELEASE_URL" target="_blank" rel="noopener" class="update-arrow"
                   title="发现新版本">
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
            <div class="footer-status" :class="{ online }">
              <span>{{ connectionLabel }}</span>
              <div class="status-dot" :class="{ online }"></div>
            </div>
          </div>
          <a class="footer-repo" href="https://github.com/AWdress/AWBotNest"
             target="_blank" rel="noopener" title="打开 AWBotNest 项目主页">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M12 .8a11.4 11.4 0 0 0-3.6 22.2c.6.1.8-.2.8-.5v-2.2c-3.3.7-4-1.4-4-1.4-.5-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.6.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0c2.3-1.5 3.3-1.2 3.3-1.2.6 1.6.2 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.6.8.5A11.4 11.4 0 0 0 12 .8Z" />
            </svg>
            <span>AWdress/AWBotNest</span>
          </a>
      </div>
    </aside>

    <!-- 主区 -->
    <main class="main">
      <header class="topbar">
        <img :src="logoWhite" class="topbar-logo" alt="" />
        <div class="page-heading">
          <span>{{ currentPage.kicker }}</span>
          <h1>{{ currentPage.title }}</h1>
        </div>
        <TopbarControlCenter
          :online="online"
          :connection-label="connectionLabel"
          :version="version"
          :latest-version="latestVersion"
          :check-releases="checkUpdate"
          :restarting="restarting"
          @restart="restart"
          @logout="logout"
        />
      </header>
      <div class="content" :class="`route-${route.path.slice(1) || 'status'}`">
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
.layout { display: flex; height: 100vh; overflow: hidden; position: relative; }

.sidebar {
  width: var(--sidebar-width);
  background: rgba(7, 11, 18, .94);
  backdrop-filter: blur(18px);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 18px 14px 14px;
  flex-shrink: 0;
}

.brand { display: flex; align-items: center; gap: 12px; padding: 8px 10px 26px; }
.logo-img { width: 40px; height: 40px; object-fit: contain; flex-shrink: 0; }
.brand-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 700; font-size: 19px; color: #fff;
  letter-spacing: 0.5px; line-height: 1.1;
  font-family: 'Segoe UI', system-ui, sans-serif;
  text-shadow: 0 1px 8px rgba(48,128,240,0.35);
}
.brand-badge-v2 {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 800;
  line-height: 1;
  padding: 2px 6px;
  border-radius: 999px;
  background: linear-gradient(135deg, rgba(48,128,240,.24), rgba(16,176,128,.16));
  color: #93c5fd;
  border: 1px solid rgba(96,165,250,.42);
  box-shadow: 0 0 10px rgba(48,128,240,.18);
  letter-spacing: 0.5px;
}
.brand-sub { font-size: 11px; color: var(--text-muted); margin-top: 3px; letter-spacing: 0.5px; }

.nav { display: flex; flex-direction: column; gap: 4px; flex: 1; }
.nav-item {
  display: flex; align-items: center; gap: 12px;
  min-height: 46px; padding: 10px 13px;
  border-radius: var(--radius-sm);
  color: #ffffff;
  transition: all 0.15s ease;
  font-size: 14px;
  font-weight: 600;
}
.nav-item:hover { background: var(--bg-hover); color: #ffffff; }
.nav-item.active {
  background: linear-gradient(90deg, rgba(48,128,240,.22), rgba(48,128,240,.07));
  color: var(--accent);
  box-shadow: inset 3px 0 0 var(--accent), 0 8px 24px rgba(10, 83, 180, .08);
}
.nav-icon { width: 18px; height: 18px; flex-shrink: 0; }

.sidebar-footer {
  padding: 12px 8px 0;
  border-top: 1px solid var(--border);
  font-size: 12px;
}
.foot-row {
  min-height: 32px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; padding: 0 2px;
}
.footer-status {
  display: flex; align-items: center; gap: 7px;
  color: var(--text-muted); white-space: nowrap;
}
.footer-status.online { color: var(--success); }
.ver {
  min-width: 0;
  color: var(--text-muted); font-size: 11px; font-family: monospace;
  display: inline-flex; align-items: center; gap: 6px;
}
.version-icon { width: 16px; height: 16px; flex: 0 0 16px; }
.update-arrow {
  width: 16px; height: 16px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%; color: var(--accent); background: var(--accent-dim);
  animation: update-pulse 2s ease-in-out infinite;
}
.update-arrow svg { width: 10px; height: 10px; }
.update-arrow:hover { color: var(--accent-hover); }
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
.footer-repo {
  min-height: 34px; padding: 0 6px;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  color: var(--text-muted);
  font-family: monospace; font-size: 11px;
  transition: color .16s ease;
}
.footer-repo:hover { color: var(--text-primary); }
.footer-repo svg { width: 16px; height: 16px; flex: 0 0 16px; }
.footer-repo span { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-muted);
}
.status-dot.online {
  background: var(--success); box-shadow: 0 0 8px var(--success);
  animation: status-breathe 2.4s ease-in-out infinite;
}
@keyframes status-breathe {
  50% { opacity: .55; box-shadow: 0 0 4px rgba(16, 176, 128, .35); }
}

.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.topbar {
  height: 72px;
  display: flex; align-items: center;
  padding: 0 32px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  position: relative;
  z-index: 40;
  background: linear-gradient(100deg, rgba(8,13,22,.94), rgba(14,25,40,.84) 66%, rgba(8,13,22,.88));
  backdrop-filter: blur(18px);
}
.page-heading { min-width: 0; display: flex; flex-direction: column; justify-content: center; position: relative; padding-bottom: 5px; }
.page-heading::after { content: ''; position: absolute; left: 0; bottom: 0; width: 30px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, #60a5fa, #10b080); box-shadow: 0 0 10px rgba(48,128,240,.28); }
.page-heading > span { color: #60a5fa; font-size: 10px; line-height: 1; letter-spacing: .11em; font-weight: 700; }
.topbar h1 { margin-top: 6px; font-size: 21px; line-height: 1; font-weight: 780; letter-spacing: .1px; }
.content {
  flex: 1; overflow-y: auto; padding: 28px 32px 34px; position: relative;
  background-image: radial-gradient(circle at center, rgba(80, 121, 172, .075) 0 1px, transparent 1.2px);
  background-size: 28px 28px;
  background-position: 3px 4px;
}

/* 顶栏 logo 默认仅在手机显示，控制中心在桌面和手机均显示。 */
.topbar-logo { display: none; width: 28px; height: 28px; object-fit: contain; }

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
  .page-heading > span { display: none; }
  .topbar h1 { margin-top: 0; font-size: 16px; }
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

.profile-loading-screen {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: grid;
  place-items: center;
  overflow: hidden;
  background:
    radial-gradient(circle at 50% 43%, rgba(35, 124, 255, .11), transparent 25%),
    radial-gradient(circle at 68% 68%, rgba(18, 184, 166, .06), transparent 30%),
    #080c14;
}
.profile-loading-screen::before {
  content: '';
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse at 50% 72%, rgba(45, 126, 244, .055), transparent 50%);
}
.profile-loading-aura {
  position: absolute;
  width: min(52vw, 560px);
  aspect-ratio: 1;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(39, 128, 255, .14), rgba(17, 182, 165, .04) 38%, transparent 68%);
  filter: blur(18px);
  animation: profile-aura 3.2s ease-in-out infinite;
}
.profile-loading-content {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  transform: translateY(-2vh);
}
.profile-loading-mark {
  position: relative;
  width: 116px;
  height: 116px;
  display: grid;
  place-items: center;
  margin-bottom: 24px;
}
.profile-loading-ring {
  position: absolute;
  inset: 0;
  border: 1px solid rgba(80, 145, 255, .2);
  border-radius: 32px;
  transform: rotate(45deg);
  box-shadow: inset 0 0 28px rgba(37, 119, 255, .07), 0 0 36px rgba(37, 119, 255, .08);
  animation: profile-ring 2.4s ease-in-out infinite;
}
.profile-loading-ring::after {
  content: '';
  position: absolute;
  width: 7px;
  height: 7px;
  top: -4px;
  left: 50%;
  border-radius: 50%;
  background: #32b8ff;
  box-shadow: 0 0 14px #278cff;
}
.profile-loading-logo {
  position: relative;
  width: 70px;
  height: 70px;
  object-fit: contain;
  filter: drop-shadow(0 8px 20px rgba(30, 109, 255, .22));
  animation: profile-logo 2.4s ease-in-out infinite;
}
.profile-loading-title {
  display: flex;
  align-items: baseline;
  min-height: 28px;
  color: #f4f7ff;
  font-size: 19px;
  font-weight: 650;
  letter-spacing: .12em;
  text-shadow: 0 0 22px rgba(71, 145, 255, .18);
}
.profile-loading-dots {
  display: inline-flex;
  gap: 4px;
  margin-left: 7px;
}
.profile-loading-dots i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: #61a8ff;
  animation: profile-dot 1.2s ease-in-out infinite;
}
.profile-loading-dots i:nth-child(2) { animation-delay: .16s; }
.profile-loading-dots i:nth-child(3) { animation-delay: .32s; }
.profile-loading-track {
  width: 148px;
  height: 2px;
  margin-top: 18px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(84, 121, 169, .14);
}
.profile-loading-track span {
  display: block;
  width: 42%;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, transparent, #398cff 45%, #25d0bd 100%);
  box-shadow: 0 0 12px rgba(52, 144, 255, .65);
  animation: profile-track 1.25s ease-in-out infinite;
}
@keyframes profile-aura {
  0%, 100% { transform: scale(.92); opacity: .55; }
  50% { transform: scale(1.08); opacity: 1; }
}
@keyframes profile-ring {
  0%, 100% { transform: rotate(45deg) scale(.94); opacity: .58; }
  50% { transform: rotate(135deg) scale(1.04); opacity: 1; }
}
@keyframes profile-logo {
  0%, 100% { transform: translateY(1px); opacity: .92; }
  50% { transform: translateY(-3px); opacity: 1; }
}
@keyframes profile-dot {
  0%, 70%, 100% { transform: translateY(0); opacity: .3; }
  35% { transform: translateY(-4px); opacity: 1; }
}
@keyframes profile-track {
  0% { transform: translateX(-110%); }
  100% { transform: translateX(350%); }
}
@media (prefers-reduced-motion: reduce) {
  .profile-loading-aura,
  .profile-loading-ring,
  .profile-loading-logo,
  .profile-loading-dots i,
  .profile-loading-track span { animation: none; }
  .profile-loading-track span { width: 100%; opacity: .7; }
}
</style>
