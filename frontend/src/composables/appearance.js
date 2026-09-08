// Keep the visible image until its replacement has actually loaded.
const CACHE_KEY = 'awbotnest-bg-last-success'
let generation = 0
let cancelPending = () => {}
let selected = null
let loaded = false

function read(key) {
  try { return localStorage.getItem(key) || '' } catch { return '' }
}
function cached() {
  try { return JSON.parse(read(CACHE_KEY)) || {} } catch { return {} }
}
function show(url) {
  document.documentElement.style.setProperty('--app-bg-image', url ? `url(${JSON.stringify(url)})` : 'none')
}

export function applyAppearance(options = {}) {
  const theme = read('awbotnest-theme') || 'dark'
  document.documentElement.dataset.theme = theme
  const custom = read('awbotnest-bg-image').trim()
  const source = custom || (theme === 'transparent' ? 'default' : '')
  const rotate = options.rotate === true
  if (source === selected && loaded && !rotate) return
  const id = ++generation
  cancelPending()
  selected = source
  loaded = false
  if (!source) { show(''); return }
  const previous = cached()
  if (!document.documentElement.style.getPropertyValue('--app-bg-image') && previous.url) show(previous.url)
  const initial = custom || (!rotate && previous.source === source && previous.url)
    || `https://www.loliapi.com/acg/?t=${Date.now()}`
  let timer
  let image
  function clear() {
    clearTimeout(timer)
    if (image) { image.onload = null; image.onerror = null }
  }
  cancelPending = clear
  function attempt(url, number) {
    if (id !== generation) return
    image = new Image()
    let settled = false
    const finish = success => {
      if (settled || id !== generation) return
      settled = true
      clear()
      if (success) {
        loaded = true
        show(url)
        try { localStorage.setItem(CACHE_KEY, JSON.stringify({ source, url })) } catch { /* Storage may be disabled. */ }
      } else if (number < 2) {
        timer = setTimeout(() => attempt(custom || `https://www.loliapi.com/acg/?t=${Date.now()}&retry=${number + 1}`, number + 1), 1500 * (number + 1))
      }
      // Exhausted retries never erase an already visible image.
    }
    image.onload = () => finish(true)
    image.onerror = () => finish(false)
    timer = setTimeout(() => finish(false), 15000)
    image.src = url
  }
  attempt(initial, 0)
}

export function disposeAppearance() {
  generation++
  cancelPending()
  selected = null
  loaded = false
}
