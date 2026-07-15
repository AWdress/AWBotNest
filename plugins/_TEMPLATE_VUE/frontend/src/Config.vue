<script setup>
// 插件自带的配置界面，通过模块联邦暴露给平台（见 vite.config 的 exposes './Config'）。
// 平台运行时加载本组件并注入两个 prop：
//   pluginId: 本插件 id
//   host: 平台能力对象
//     host.getConfig()            读取本插件已保存配置（Promise<对象>）
//     host.saveConfig(values)     保存配置（Promise）——存平台统一存储，插件里 ctx.config 可读到
//     host.callApi(path, {method, body})  调用插件 ctx.on_api 注册的后端接口（Promise<JSON>）
//     host.toast.success/error(msg)       弹平台提示
//     host.token                  管理员令牌（一般用不到，host.callApi 已带）
// 组件用的是平台那一份 Vue（模块联邦 shared），无需自带。
import { ref, onMounted } from 'vue'

const props = defineProps({
  pluginId: { type: String, required: true },
  host: { type: Object, required: true },
})

const cfg = ref({ greeting: '你好', enabled: true })
const loading = ref(true)
const saving = ref(false)
const pingResult = ref('')
const echoText = ref('')

onMounted(async () => {
  try {
    const saved = await props.host.getConfig()
    cfg.value = { greeting: '你好', enabled: true, ...(saved || {}) }
  } catch (e) {
    props.host.toast.error('读取配置失败：' + (e.message || e))
  } finally {
    loading.value = false
  }
})

async function save() {
  saving.value = true
  try {
    await props.host.saveConfig(cfg.value)
    props.host.toast.success('配置已保存')
  } catch (e) {
    props.host.toast.error('保存失败：' + (e.message || e))
  } finally {
    saving.value = false
  }
}

async function doPing() {
  try {
    const r = await props.host.callApi('/ping')
    pingResult.value = `pong · 服务器时间 ${r.server_time}`
  } catch (e) {
    props.host.toast.error('ping 失败：' + (e.message || e))
  }
}

async function doEcho() {
  try {
    const r = await props.host.callApi('/echo', { method: 'POST', body: { text: echoText.value } })
    props.host.toast.success(`已回显，累计 ${r.echo_count} 次`)
  } catch (e) {
    props.host.toast.error('echo 失败：' + (e.message || e))
  }
}
</script>

<template>
  <div class="vcfg">
    <div v-if="loading" class="muted">加载配置…</div>
    <template v-else>
      <section class="card">
        <h3>基础配置</h3>
        <label class="row">
          <span>问候语</span>
          <input v-model="cfg.greeting" class="inp" type="text" />
        </label>
        <label class="row switch">
          <span>启用功能</span>
          <input v-model="cfg.enabled" type="checkbox" />
        </label>
        <button class="btn primary" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存配置' }}
        </button>
      </section>

      <section class="card">
        <h3>调用插件后端接口（ctx.on_api）</h3>
        <div class="row">
          <button class="btn" @click="doPing">GET /ping</button>
          <span class="muted">{{ pingResult }}</span>
        </div>
        <div class="row">
          <input v-model="echoText" class="inp" type="text" placeholder="发点文字给 /echo" />
          <button class="btn" @click="doEcho">POST /echo</button>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
/* 用平台主题变量（有则跟随暗色主题），带回退值以便本地 npm run dev 预览 */
.vcfg { display: flex; flex-direction: column; gap: 16px; }
.card {
  display: flex; flex-direction: column; gap: 12px;
  padding: 16px; border-radius: 10px;
  background: var(--bg-elevated, #1a1d27); border: 1px solid var(--border-light, #2a2e3a);
}
h3 { font-size: 13px; font-weight: 600; color: var(--accent, #6ea8fe); margin: 0; }
.row { display: flex; align-items: center; gap: 10px; }
.row > span:first-child { min-width: 72px; font-size: 13px; color: var(--text-secondary, #b9c0cc); }
.switch { justify-content: flex-start; }
.inp {
  flex: 1; padding: 8px 10px; border-radius: 6px; font-size: 13px;
  background: var(--bg-card, #12141c); color: var(--text-primary, #e8ebf0);
  border: 1px solid var(--border-light, #2a2e3a);
}
.btn {
  padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
  background: var(--bg-card, #12141c); color: var(--text-secondary, #b9c0cc);
  border: 1px solid var(--border-light, #2a2e3a);
}
.btn:hover { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
.btn.primary {
  align-self: flex-start;
  background: var(--accent-dim, #1e3a5f); border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe);
}
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.muted { font-size: 12px; color: var(--text-muted, #7a8291); }
</style>
