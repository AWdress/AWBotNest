// AWBotNest PWA Service Worker
// 极简策略：导航请求网络优先（保证控制台数据最新），静态资源缓存兜底。
// 不缓存 /api/，避免登录态/数据陈旧。
const CACHE = 'awbotnest-v2-2'
const ICON_CACHE = 'awbotnest-v2-icons'
const ASSETS = ['/', '/index.html', '/favicon.ico', '/pwa-192.png', '/pwa-512.png']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => {}))
  self.skipWaiting()
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith('awbotnest-') && ![CACHE, ICON_CACHE].includes(k)).map((k) => caches.delete(k)))
    )
  )
  self.clients.claim()
})

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url)
  // API 与 WebSocket 一律走网络，不缓存
  if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') return
  // 插件 Logo 通常来自 GitHub/raw 仓库；跨页面持久缓存，避免每次打开市场重新下载。
  if (e.request.destination === 'image' && ['raw.githubusercontent.com', 'avatars.githubusercontent.com'].includes(url.hostname)) {
    e.respondWith(
      caches.open(ICON_CACHE).then(async (cache) => {
        const cached = await cache.match(e.request)
        if (cached) return cached
        const response = await fetch(e.request)
        if (response.ok || response.type === 'opaque') {
          try {
            await cache.put(e.request, response.clone())
            const keys = await cache.keys()
            await Promise.all(keys.slice(0, Math.max(0, keys.length - 200)).map(key => cache.delete(key)))
          } catch {}
        }
        return response
      })
    )
    return
  }
  // Random wallpaper APIs and unrelated origins must not accumulate in the asset cache.
  if (url.origin !== self.location.origin) return
  // 带内容指纹的构建资源可长期缓存；文件变化时地址也会变化。
  if (url.pathname.startsWith('/assets/')) {
    e.respondWith(
      caches.match(e.request).then((cached) => cached || fetch(e.request).then((resp) => {
        if (resp.ok) {
          const copy = resp.clone()
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {})
        }
        return resp
      }))
    )
    return
  }
  // 页面入口网络优先，保证升级后能拿到最新资源地址。
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        if (resp.ok) {
          const copy = resp.clone()
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {})
        }
        return resp
      })
      .catch(async () => (await caches.match(e.request)) ||
        (e.request.mode === 'navigate' ? await caches.match('/index.html') : null) || Response.error())
  )
})
