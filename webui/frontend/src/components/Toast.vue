<script setup>
// 全局悬浮提示组件，挂在 App 根部。右上角堆叠，自动消失。
import { toastState, dismissToast } from '../composables/toast'

const icons = {
  success: 'M12 22a10 10 0 100-20 10 10 0 000 20zM8 12l3 3 5-6',
  error: 'M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z',
  info: 'M12 22a10 10 0 100-20 10 10 0 000 20zM12 8v5M12 16h.01',
}
</script>

<template>
  <div class="toast-wrap">
    <TransitionGroup name="toast">
      <div v-for="t in toastState.items" :key="t.id" class="toast" :class="t.type" @click="dismissToast(t.id)">
        <svg class="t-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path :d="icons[t.type] || icons.info" />
        </svg>
        <span class="t-msg">{{ t.message }}</span>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-wrap {
  position: fixed; top: 20px; right: 20px; z-index: 1000;
  display: flex; flex-direction: column; gap: 10px;
  pointer-events: none;
}
.toast {
  display: flex; align-items: center; gap: 10px;
  min-width: 240px; max-width: 380px;
  padding: 13px 16px;
  background: var(--bg-card); border: 1px solid var(--border-light);
  border-radius: var(--radius-sm); box-shadow: var(--shadow);
  font-size: 13px; color: var(--text-primary);
  cursor: pointer; pointer-events: auto;
}
.toast.success { border-left: 3px solid var(--accent); }
.toast.error { border-left: 3px solid var(--danger); }
.toast.info { border-left: 3px solid var(--accent); }
.t-icon { width: 18px; height: 18px; flex-shrink: 0; }
.toast.success .t-icon { color: var(--accent); }
.toast.error .t-icon { color: var(--danger); }
.toast.info .t-icon { color: var(--accent); }
.t-msg { line-height: 1.4; }

.toast-enter-active, .toast-leave-active { transition: all 0.25s ease; }
.toast-enter-from { opacity: 0; transform: translateX(30px); }
.toast-leave-to { opacity: 0; transform: translateX(30px); }
.toast-leave-active { position: absolute; right: 0; }
</style>
