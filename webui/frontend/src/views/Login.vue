<script setup>
import { ref, onMounted } from 'vue'
import { api, setToken } from '../api'
import logoWhite from '../assets/logo-white.png'

const emit = defineEmits(['authed'])

const username = ref('')
const pwd = ref('')
const err = ref('')
const busy = ref(false)
const loading = ref(true)
const version = ref('')

onMounted(async () => {
  try {
    const st = await api.authStatus()
    version.value = st.version || ''
    if (st.dev_no_auth) { emit('authed'); return }
  } catch (e) { /* 网络异常也展示登录页 */ }
  finally { loading.value = false }
})

async function submit() {
  err.value = ''
  if (!username.value) { err.value = '请输入用户名'; return }
  if (!pwd.value) { err.value = '请输入密码'; return }
  busy.value = true
  try {
    const r = await api.authLogin(username.value.trim(), pwd.value)
    setToken(r.token)
    emit('authed')
  } catch (e) { err.value = e.message }
  finally { busy.value = false }
}
</script>

<template>
  <div class="login-bg" v-if="!loading">
    <div class="login-card">
      <div class="lc-head">
        <img :src="logoWhite" class="lc-logo" alt="" />
        <div class="lc-title">AWBotNest</div>
        <div class="lc-sub">插件化机器人平台 · 控制台登录</div>
      </div>

      <div v-if="err" class="lc-alert">{{ err }}</div>

      <div class="lc-field">
        <label>用户名</label>
        <input class="lc-input" v-model="username" @keyup.enter="submit" placeholder="用户名" autofocus />
      </div>
      <div class="lc-field">
        <label>密码</label>
        <input class="lc-input" type="password" v-model="pwd" @keyup.enter="submit" placeholder="密码" />
      </div>

      <button class="lc-btn" @click="submit" :disabled="busy">
        {{ busy ? '登录中…' : '登 录' }}
      </button>

      <div class="lc-hint">© 2026 AWBotNest<span v-if="version"> · v{{ version }}</span></div>
    </div>
  </div>
</template>

<style scoped>
.login-bg {
  height: 100vh; display: flex; align-items: center; justify-content: center;
  background: radial-gradient(1200px 600px at 50% 0%, #0d1426 0%, #0a0e17 55%, #07090f 100%);
  position: relative; overflow: hidden;
}
.login-bg::before, .login-bg::after {
  content: ''; position: absolute; border-radius: 50%; filter: blur(90px); opacity: 0.35;
}
.login-bg::before { width: 460px; height: 460px; background: #3080f0; top: -140px; left: 12%; }
.login-bg::after { width: 380px; height: 380px; background: #10b080; bottom: -160px; right: 14%; opacity: 0.25; }

.login-card {
  position: relative; z-index: 1;
  width: 380px; max-width: 90vw;
  background: rgba(17, 19, 26, 0.92);
  border: 1px solid var(--border-light);
  border-radius: 18px; padding: 40px 36px 30px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.lc-head { text-align: center; margin-bottom: 28px; }
.lc-logo { width: 56px; height: 56px; object-fit: contain; margin-bottom: 14px; }
.lc-title { font-size: 24px; font-weight: 700; color: #fff; letter-spacing: 0.5px;
  text-shadow: 0 2px 12px rgba(48,128,240,0.4); }
.lc-sub { font-size: 12px; color: var(--text-muted); margin-top: 6px; }

.lc-alert { background: var(--danger-dim); color: var(--danger); padding: 9px 12px;
  border-radius: 8px; font-size: 13px; margin-bottom: 16px; text-align: center; }

.lc-field { display: flex; flex-direction: column; gap: 7px; margin-bottom: 16px; }
.lc-field label { font-size: 12px; color: var(--text-secondary); }
.lc-input {
  width: 100%; padding: 12px 14px; font-size: 14px;
  background: var(--bg-elevated); border: 1px solid var(--border-light);
  border-radius: 10px; color: var(--text-primary);
}
.lc-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }

.lc-btn {
  width: 100%; padding: 13px; margin-top: 8px; cursor: pointer;
  background: linear-gradient(135deg, #3080f0, #2566d8); color: #fff;
  border: none; border-radius: 10px; font-size: 15px; font-weight: 600; letter-spacing: 4px;
  box-shadow: 0 6px 20px rgba(48,128,240,0.35); transition: opacity 0.15s;
}
.lc-btn:hover { opacity: 0.92; }
.lc-btn:disabled { opacity: 0.6; cursor: not-allowed; }

.lc-hint { text-align: center; font-size: 11px; color: var(--text-muted); margin-top: 18px; }
</style>
