// 全局确认弹窗：用内置 UI 替代原生 confirm()。
// 用法：const ok = await confirm({ title, message, danger }); if (!ok) return
import { reactive } from 'vue'

export const confirmState = reactive({
  open: false,
  title: '确认操作',
  message: '',
  confirmText: '确定',
  cancelText: '取消',
  danger: false,
  _resolve: null,
})

export function confirm(opts = {}) {
  // A second prompt must settle the previous caller instead of leaving it waiting forever.
  if (confirmState._resolve) _resolveConfirm(false)
  confirmState.title = opts.title || '确认操作'
  confirmState.message = opts.message || ''
  confirmState.confirmText = opts.confirmText || '确定'
  confirmState.cancelText = opts.cancelText || '取消'
  confirmState.danger = opts.danger || false
  confirmState.open = true
  return new Promise((resolve) => { confirmState._resolve = resolve })
}

export function _resolveConfirm(val) {
  confirmState.open = false
  if (confirmState._resolve) { confirmState._resolve(val); confirmState._resolve = null }
}
