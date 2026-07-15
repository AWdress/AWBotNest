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
    const name = `plugin_${props.pluginId}`
    __federation_method_setRemote(name, {
      url: () => Promise.resolve(`/api/plugins/${props.pluginId}/fe/assets/remoteEntry.js`),
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
