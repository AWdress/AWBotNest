<script setup>
// 全局确认弹窗组件，挂在 App 根部。样式与平台深色风格一致。
import { confirmState, _resolveConfirm } from '../composables/confirm'

function ok() { _resolveConfirm(true) }
function cancel() { _resolveConfirm(false) }
</script>

<template>
  <Transition name="fade">
    <div v-if="confirmState.open" class="mask" @click.self="cancel">
      <div class="dialog">
        <div class="d-icon" :class="{ danger: confirmState.danger }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path v-if="confirmState.danger" d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z" />
            <path v-else d="M12 22a10 10 0 100-20 10 10 0 000 20zM12 8v5M12 16h.01" />
          </svg>
        </div>
        <h3 class="d-title">{{ confirmState.title }}</h3>
        <p class="d-msg">{{ confirmState.message }}</p>
        <div class="d-actions">
          <button class="btn" @click="cancel">{{ confirmState.cancelText }}</button>
          <button class="btn" :class="confirmState.danger ? 'btn-danger-solid' : 'btn-primary'" @click="ok">
            {{ confirmState.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.mask {
  position: fixed; inset: 0; z-index: 999;
  background: rgba(3, 6, 12, 0.7); backdrop-filter: blur(5px);
  display: flex; align-items: center; justify-content: center;
}
.dialog {
  width: 380px; max-width: 90vw;
  background: linear-gradient(155deg, #171b25, #10131b); border: 1px solid var(--border-light);
  border-radius: var(--radius-lg); padding: 28px 26px;
  text-align: center; box-shadow: var(--shadow-float);
}
.d-icon {
  width: 52px; height: 52px; border-radius: 50%; margin: 0 auto 16px;
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-dim); color: var(--accent);
}
.d-icon.danger { background: var(--danger-dim); color: var(--danger); }
.d-icon svg { width: 26px; height: 26px; }
.d-title { font-size: 17px; font-weight: 600; margin-bottom: 10px; }
.d-msg { font-size: 13px; color: var(--text-secondary); line-height: 1.6; white-space: pre-line; margin-bottom: 24px; }
.d-actions { display: flex; gap: 12px; }
.d-actions .btn { flex: 1; justify-content: center; padding: 10px; }
.btn-danger-solid { background: var(--danger); border-color: var(--danger); color: #fff; }
.btn-danger-solid:hover { opacity: 0.9; }

.fade-enter-active, .fade-leave-active { transition: opacity 0.15s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

@media (max-width: 560px) {
  .mask { align-items: flex-end; }
  .dialog { width: 100%; max-width: 100%; border-radius: 18px 18px 0 0; padding: 24px 20px calc(20px + env(safe-area-inset-bottom)); }
}
</style>
