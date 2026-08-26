import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './styles/tokens.css'
import { appRoutes } from './routePreload'

const router = createRouter({
  // 用 hash 模式，避免后端路由配置；FastAPI 只需托管 index.html
  history: createWebHashHistory(),
  routes: appRoutes,
})

createApp(App).use(router).mount('#app')

// 注册 PWA Service Worker（支持手机"添加到主屏幕"独立运行）
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
