import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './styles/tokens.css'

import Plugins from './views/Plugins.vue'
import Accounts from './views/Accounts.vue'
import Logs from './views/Logs.vue'
import Status from './views/Status.vue'
import Settings from './views/Settings.vue'

const router = createRouter({
  // 用 hash 模式，避免后端路由配置；FastAPI 只需托管 index.html
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/plugins' },
    { path: '/plugins', component: Plugins, meta: { title: '插件管理' } },
    { path: '/accounts', component: Accounts, meta: { title: '账号管理' } },
    { path: '/logs', component: Logs, meta: { title: '运行日志' } },
    { path: '/status', component: Status, meta: { title: '系统状态' } },
    { path: '/settings', component: Settings, meta: { title: '系统设置' } },
  ],
})

createApp(App).use(router).mount('#app')
