<script setup>
// vue 模式插件配置：运行时用模块联邦动态加载插件自带的 ./Config 组件并挂载。
// 插件 id 在构建期未知，故用 @originjs/vite-plugin-federation 的动态 API：
//   setRemote(注册远程) → getRemote(取暴露的模块) → unwrapDefault(取默认导出组件)
// 宿主与插件共享同一份 Vue（见 vite.config 的 shared），组件直接用宿主 Vue 渲染。
import { ref, shallowRef, markRaw, onMounted } from 'vue'
import {
  __federation_method_setRemote,
  __federation_method_getRemote,
  __federation_method_unwrapDefault,
} from '__federation__'
import { api, getToken } from '../api'
import { toast } from '../composables/toast'

const props = defineProps({
  pluginId: { type: String, required: true },
  hasFrontend: { type: Boolean, default: true },
})

const remoteComp = shallowRef(null)
const loading = ref(true)
const err = ref('')

// 传给插件 Config 组件的能力对象：读写配置 + 调用插件自有 API + 弹提示。
// 插件通过 props.host 使用，无需自己关心鉴权（api 客户端已带管理员令牌）。
const host = {
  pluginId: props.pluginId,
  get token() { return getToken() },
  // 标准配置读写（沿用平台统一存储，ctx.config 可直接读到）
  getConfig: async () => (await api.getPluginConfig(props.pluginId)).values,
  saveConfig: (values) => api.setPluginConfig(props.pluginId, values),
  // 调用插件 ctx.on_api 注册的端点：callApi('/status') / callApi('/run', {method:'POST', body})
  callApi: (path, opts = {}) =>
    api.callPluginApi(props.pluginId, path, opts.method || 'GET', opts.body),
  toast,
}

async function loadRemote() {
  loading.value = true
  err.value = ''
  try {
    if (!props.hasFrontend) {
      throw new Error('该插件未随附前端构建产物（frontend/dist）。请让作者构建后再发布。')
    }
    // 从「dist 根」URL 加载 remoteEntry（即使文件物理在 dist/assets/ 内，也用根 URL）。
    // 因为 remoteEntry 内部把 chunk 引成 ./assets/xxx，是相对「加载 URL」解析的：
    // 从 /fe/remoteEntry.js 加载 → ./assets/xxx 正确解析为 /fe/assets/xxx；
    // 若从 /fe/assets/remoteEntry.js 加载则会叠成 /fe/assets/assets/xxx（404）。
    // 后端对 /fe/remoteEntry.js 找不到时会回退到 dist/assets/remoteEntry.js，兼容两种产物布局。
    const name = `plugin_${props.pluginId}`
    // remoteEntry 是固定文件名的联邦入口，浏览器可能仍复用早先缓存的旧入口，
    // 而旧入口引用的 hash chunk 在插件更新后已被删除 → 拉取 Config chunk 报 404
    // （"Failed to fetch dynamically imported module"）。加时间戳强制每次取最新入口。
    // 入口内部把 chunk 引成 ./assets/xxx，按「加载 URL 的路径」相对解析，查询串不会带到
    // chunk 上（/fe/remoteEntry.js?t=123 → ./assets/x.js 仍解析为 /fe/assets/x.js），
    // 故带内容 hash 的 chunk 依旧走长缓存，只有入口每次刷新。
    __federation_method_setRemote(name, {
      url: () => Promise.resolve(`/api/plugins/${props.pluginId}/fe/remoteEntry.js?t=${Date.now()}`),
      format: 'esm',
      from: 'vite',
    })
    const mod = await __federation_method_getRemote(name, './Config')
    const comp = await __federation_method_unwrapDefault(mod)
    if (!comp) throw new Error('插件未暴露 ./Config 组件')
    remoteComp.value = markRaw(comp)
  } catch (e) {
    // 常见原因：资源 Cookie 缺失/过期（403）→ 提示重新登录；其余透传原始信息
    const msg = e?.message || ''
    err.value = /403|forbidden|无权/i.test(msg)
      ? '无权加载插件前端（资源凭证可能已过期），请重新登录后重试。'
      : (msg || '加载插件配置界面失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadRemote)
</script>

<template>
  <div class="remote-config">
    <div v-if="loading" class="muted center">加载插件配置界面…</div>
    <div v-else-if="err" class="remote-err">
      {{ err }}
      <button class="btn sm" @click="loadRemote">重试</button>
    </div>
    <component v-else-if="remoteComp" :is="remoteComp" :plugin-id="pluginId" :host="host" />
  </div>
</template>

<style scoped>
.remote-config { min-height: 80px; }
.center { text-align: center; padding: 32px; }
.remote-err {
  display: flex; flex-direction: column; align-items: flex-start; gap: 12px;
  padding: 16px; border-radius: var(--radius-sm);
  background: var(--danger-dim); color: var(--danger); font-size: 13px;
}
</style>
