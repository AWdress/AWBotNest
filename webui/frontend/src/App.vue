<script setup>
import { useRoute, useRouter } from 'vue-router'
import { ref, onMounted } from 'vue'
import { api, getToken, setToken, setUnauthorizedHandler } from './api'
import Login from './views/Login.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import logoWhite from './assets/logo-white.png'

const route = useRoute()
const router = useRouter()
const online = ref(false)
const version = ref('')

// 鉴权门：未登录显示 Login，登录后显示主界面
const authed = ref(false)
function onAuthed() { authed.value = true; ping() }
function logout() { setToken(''); authed.value = false }
setUnauthorizedHandler(() => { authed.value = false })

// 刷新后恢复登录态：localStorage 有令牌就乐观进主界面，
// 令牌失效时 ping() 的受保护接口会返回 401，由上面的 unauthorized 回调踢回登录页。
if (getToken()) { authed.value = true }

async function ping() {
  try {
    const s = await api.status()
    online.value = true
    version.value = s.version || ''
  } catch { online.value = false }
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
  if (authed.value) ping()   // 恢复登录态后立即校验令牌（失效则 401 踢回登录页）
  setInterval(() => { if (authed.value) ping() }, 10000)
})
</script>

<template>
  <Login v-if="!authed" @authed="onAuthed" />
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
          <div class="footer-status">
            <div class="status-dot" :class="{ online }"></div>
            <span class="muted">{{ online ? '平台在线' : '连接中…' }}</span>
          </div>
          <span class="ver" v-if="version">v{{ version }}</span>
        </div>
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
        <h1>{{ route.meta.title || '控制台' }}</h1>
      </header>
      <div class="content">
        <RouterView />
      </div>
    </main>
  </div>

  <!-- 全局确认弹窗 -->
  <ConfirmDialog />
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
.ver { color: var(--text-muted); font-size: 11px; font-family: monospace; }
.logout-btn {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  width: 100%; padding: 9px; cursor: pointer;
  background: transparent; border: 1px solid var(--border-light); border-radius: var(--radius-sm);
  color: var(--text-secondary); font-size: 13px; transition: all 0.15s ease;
}
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
</style>
