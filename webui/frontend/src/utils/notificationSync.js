const EVENT_NAME = 'awbotnest:notification-sync'
const STORAGE_KEY = 'awbotnest_notification_sync'
const channel = typeof BroadcastChannel !== 'undefined'
  ? new BroadcastChannel(EVENT_NAME)
  : null

function message(detail) {
  return {
    id: `${Date.now()}_${Math.random().toString(36).slice(2)}`,
    at: Date.now(),
    ...detail,
  }
}

export function publishNotificationSync(detail = {}) {
  const payload = message(detail)
  window.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: payload }))
  if (channel) {
    channel.postMessage(payload)
  } else {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  }
}

export function subscribeNotificationSync(handler) {
  const run = (payload) => {
    try {
      const pending = handler(payload)
      if (pending?.catch) pending.catch(() => {})
    } catch {}
  }
  const onWindow = (event) => run(event.detail || {})
  const onChannel = (event) => run(event.data || {})
  const onStorage = (event) => {
    if (event.key !== STORAGE_KEY || !event.newValue) return
    try { run(JSON.parse(event.newValue)) } catch {}
  }

  window.addEventListener(EVENT_NAME, onWindow)
  if (channel) channel.addEventListener('message', onChannel)
  else window.addEventListener('storage', onStorage)

  return () => {
    window.removeEventListener(EVENT_NAME, onWindow)
    if (channel) channel.removeEventListener('message', onChannel)
    else window.removeEventListener('storage', onStorage)
  }
}
