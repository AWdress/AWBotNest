const EDITABLE_SELECTOR = 'input, textarea, select, [contenteditable="true"]'

function isEditable(element) {
  return element instanceof Element && element.matches(EDITABLE_SELECTOR)
}

function findScrollContainer(element) {
  let current = element?.parentElement
  while (current && current !== document.body && current !== document.documentElement) {
    const { overflowY } = window.getComputedStyle(current)
    if (/(auto|scroll|overlay)/.test(overflowY) && current.scrollHeight > current.clientHeight) {
      return current
    }
    current = current.parentElement
  }
  return document.scrollingElement || document.documentElement
}

function keepFocusedFieldVisible() {
  if (!window.matchMedia?.('(max-width: 768px)').matches) return
  const field = document.activeElement
  if (!isEditable(field)) return

  const viewport = window.visualViewport
  const visibleTop = viewport?.offsetTop || 0
  const visibleBottom = visibleTop + (viewport?.height || window.innerHeight)
  const topPadding = Math.max(16, Number.parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--keyboard-field-top-padding')) || 16)
  const bottomPadding = 20
  const rect = field.getBoundingClientRect()
  let delta = 0

  if (rect.bottom > visibleBottom - bottomPadding) {
    delta = rect.bottom - visibleBottom + bottomPadding
  } else if (rect.top < visibleTop + topPadding) {
    delta = rect.top - visibleTop - topPadding
  }

  if (Math.abs(delta) < 1) return
  const scroller = findScrollContainer(field)
  scroller.scrollBy({ top: delta, behavior: 'smooth' })
}

/**
 * 同步移动浏览器的可见视口。iOS 弹出键盘时只缩小 visualViewport，
 * 不应跟着压缩整个平台外壳；弹窗通过这里暴露的变量单独跟随键盘上沿。
 */
export function configureMobileViewport() {
  const root = document.documentElement
  const displayMode = window.matchMedia?.('(display-mode: standalone)')
  const iosStandalone = window.navigator.standalone === true
  const standalone = iosStandalone || displayMode?.matches === true
  const viewport = window.visualViewport
  root.classList.toggle('pwa-standalone', standalone)
  root.classList.toggle('ios-pwa', iosStandalone)

  let frame = 0
  let focusTimer = 0
  let lastEditableFocus = 0
  let baselineHeight = viewport?.height || window.innerHeight
  let pwaShellHeight = Math.max(window.innerHeight, baselineHeight)

  const screenHeight = () => {
    const portrait = window.matchMedia?.('(orientation: portrait)').matches !== false
    return portrait
      ? Math.max(window.screen.width, window.screen.height)
      : Math.min(window.screen.width, window.screen.height)
  }

  const syncViewport = () => {
    frame = 0
    const visibleHeight = viewport?.height || window.innerHeight
    const offsetTop = viewport?.offsetTop || 0
    const mobile = window.matchMedia?.('(max-width: 768px)').matches === true
    const recentlyEditing = isEditable(document.activeElement) || Date.now() - lastEditableFocus < 900
    const lostHeight = baselineHeight - visibleHeight
    const deviceGap = screenHeight() - visibleHeight
    const keyboardOpen = mobile && recentlyEditing && (
      lostHeight > 100 || deviceGap > Math.max(180, screenHeight() * 0.22)
    )

    if (!keyboardOpen && !recentlyEditing && visibleHeight > baselineHeight - 80) {
      baselineHeight = visibleHeight
      pwaShellHeight = Math.max(window.innerHeight, visibleHeight)
    }

    root.style.setProperty('--visual-viewport-height', `${Math.round(visibleHeight)}px`)
    root.style.setProperty('--visual-viewport-offset-top', `${Math.round(offsetTop)}px`)
    root.classList.toggle('keyboard-open', keyboardOpen)

    if (iosStandalone) {
      // 键盘打开时保持页面外壳高度稳定，避免底部导航和整页一起被顶上去。
      const shellHeight = keyboardOpen ? pwaShellHeight : Math.max(pwaShellHeight, window.innerHeight, screenHeight())
      if (!keyboardOpen) pwaShellHeight = shellHeight
      root.style.setProperty('--pwa-viewport-height', `${Math.round(shellHeight)}px`)
    }

    if (keyboardOpen) requestAnimationFrame(keepFocusedFieldVisible)
  }

  const scheduleSync = () => {
    if (frame) cancelAnimationFrame(frame)
    frame = requestAnimationFrame(syncViewport)
  }

  const scheduleFocusVisibility = () => {
    clearTimeout(focusTimer)
    requestAnimationFrame(keepFocusedFieldVisible)
    focusTimer = window.setTimeout(keepFocusedFieldVisible, 280)
  }

  document.addEventListener('focusin', (event) => {
    if (!isEditable(event.target)) return
    lastEditableFocus = Date.now()
    scheduleSync()
    scheduleFocusVisibility()
  })
  document.addEventListener('focusout', () => {
    window.setTimeout(scheduleSync, 950)
  })
  window.addEventListener('resize', scheduleSync, { passive: true })
  window.addEventListener('orientationchange', () => {
    baselineHeight = viewport?.height || window.innerHeight
    pwaShellHeight = Math.max(window.innerHeight, baselineHeight)
    scheduleSync()
  }, { passive: true })
  viewport?.addEventListener('resize', scheduleSync, { passive: true })
  viewport?.addEventListener('scroll', scheduleSync, { passive: true })
  syncViewport()
}
