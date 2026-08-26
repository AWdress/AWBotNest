import { api } from '../api'

const avatarCache = new Map()
const AVATAR_TIMEOUT_MS = 6000

function accountKey(account) {
  const session = String(account?.session || '')
  const version = String(account?.avatar_id || '')
  return session && version ? `${session}|${version}` : ''
}

function discardOldVersions(session, keepKey) {
  const prefix = `${session}|`
  for (const [key, entry] of avatarCache) {
    if (!key.startsWith(prefix) || key === keepKey) continue
    entry.controller?.abort()
    if (entry.url) URL.revokeObjectURL(entry.url)
    avatarCache.delete(key)
  }
}

export function getAccountAvatarUrl(account) {
  return avatarCache.get(accountKey(account))?.url || ''
}

export async function loadAccountAvatar(account) {
  const key = accountKey(account)
  if (!key) return ''
  const cached = avatarCache.get(key)
  if (cached?.url || cached?.failed) return cached.url || ''
  if (cached?.promise) return cached.promise

  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), AVATAR_TIMEOUT_MS)
  const entry = { url: '', failed: false, promise: null, controller }
  entry.promise = api.accountAvatar(account.session, account.avatar_id, controller.signal)
    .then((blob) => {
      discardOldVersions(account.session, key)
      entry.url = URL.createObjectURL(blob)
      return entry.url
    })
    .catch(() => {
      entry.failed = true
      return ''
    })
    .finally(() => {
      window.clearTimeout(timeout)
      entry.promise = null
    })
  avatarCache.set(key, entry)
  return entry.promise
}

export async function preloadAccountAvatars(accounts = []) {
  const pending = accounts.filter(account => account?.avatar_id && account?.session)
  let cursor = 0
  const workers = Array.from({ length: Math.min(4, pending.length) }, async () => {
    while (cursor < pending.length) {
      const account = pending[cursor++]
      await loadAccountAvatar(account)
    }
  })
  await Promise.all(workers)
}

export function clearAccountAvatarCache() {
  for (const entry of avatarCache.values()) {
    entry.controller?.abort()
    if (entry.url) URL.revokeObjectURL(entry.url)
  }
  avatarCache.clear()
}
