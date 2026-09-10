import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './styles/tokens.css'
import { appRoutes } from './routePreload'
import { applyAppearance } from './composables/appearance'

function configurePwaViewport() {
  const root = document.documentElement
  const displayMode = window.matchMedia?.('(display-mode: standalone)')
  const iosStandalone = window.navigator.standalone === true
  const standalone = iosStandalone || displayMode?.matches === true
  root.classList.toggle('pwa-standalone', standalone)
  root.classList.toggle('ios-pwa', iosStandalone)
  if (!iosStandalone) return

  let frame = 0
  const syncHeight = () => {
    frame = 0
    const portrait = window.matchMedia?.('(orientation: portrait)').matches !== false
    const screenHeight = portrait
      ? Math.max(window.screen.width, window.screen.height)
      : Math.min(window.screen.width, window.screen.height)
    const visibleHeight = window.visualViewport?.height || window.innerHeight
    const keyboardGap = screenHeight - visibleHeight
    const keyboardOpen = keyboardGap > Math.max(180, screenHeight * 0.25)
    const shellHeight = keyboardOpen ? visibleHeight : Math.max(window.innerHeight, screenHeight)
    root.style.setProperty('--pwa-viewport-height', `${Math.round(shellHeight)}px`)
  }
  const scheduleSync = () => {
    if (frame) cancelAnimationFrame(frame)
    frame = requestAnimationFrame(syncHeight)
  }
  syncHeight()
  window.addEventListener('resize', scheduleSync, { passive: true })
  window.addEventListener('orientationchange', scheduleSync, { passive: true })
  window.visualViewport?.addEventListener('resize', scheduleSync, { passive: true })
}

configurePwaViewport()
applyAppearance()

const router = createRouter({
  // 用 hash 模式，避免后端路由配置；FastAPI 只需托管 index.html
  history: createWebHashHistory(),
  routes: appRoutes,
})

createApp(App).use(router).mount('#app')

// 注册 PWA Service Worker（支持手机"添加到主屏幕"独立运行）
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' })
      .then((registration) => registration.update())
      .catch(() => {})
  })
}
