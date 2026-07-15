// 本地预览入口（npm run dev）：用一个「模拟 host」把 Config.vue 跑起来，
// 方便不启动平台也能调界面。真正运行时由平台注入真实 host（见 Config.vue 注释）。
import { createApp, h } from 'vue'
import Config from './Config.vue'

const mockHost = {
  pluginId: '_TEMPLATE_VUE',
  token: 'dev',
  async getConfig() {
    console.log('[mock] getConfig')
    return { greeting: '你好（本地预览）', enabled: true }
  },
  async saveConfig(values) {
    console.log('[mock] saveConfig', values)
  },
  async callApi(path, opts = {}) {
    console.log('[mock] callApi', path, opts)
    if (path === '/ping') return { ok: true, message: 'pong', server_time: Math.floor(Date.now() / 1000) }
    if (path === '/echo') return { ok: true, received: opts.body, echo_count: 1 }
    return { ok: true }
  },
  toast: {
    success: (m) => console.log('[toast.success]', m),
    error: (m) => console.warn('[toast.error]', m),
  },
}

createApp({
  render: () => h(Config, { pluginId: mockHost.pluginId, host: mockHost }),
}).mount('#app')
