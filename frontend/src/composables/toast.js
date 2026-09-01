// 全局悬浮提示（toast）：右上角短暂浮现，自动消失。
// 用法：toast.success('安装完成') / toast.error('失败') / toast.info('...')
import { reactive } from 'vue'

export const toastState = reactive({
  items: [],   // { id, type: 'success'|'error'|'info', message }
})

let _id = 0

export function showToast(message, type = 'info', duration = 3000) {
  const id = ++_id
  toastState.items.push({ id, type, message })
  if (duration > 0) {
    setTimeout(() => dismissToast(id), duration)
  }
  return id
}

export function dismissToast(id) {
  const i = toastState.items.findIndex((t) => t.id === id)
  if (i !== -1) toastState.items.splice(i, 1)
}

export const toast = {
  success: (msg, d) => showToast(msg, 'success', d),
  error: (msg, d) => showToast(msg, 'error', d),
  info: (msg, d) => showToast(msg, 'info', d),
}
