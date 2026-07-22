// AWBotNest PWA Service Worker
// 极简策略：导航请求网络优先（保证控制台数据最新），静态资源缓存兜底。
// 不缓存 /api/，避免登录态/数据陈旧。
const CACHE = 'awbotnest-v2'
const ASSETS = ['/', '/index.html', '/favicon.ico', '/pwa-192.png', '/pwa-512.png']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => {}))
  self.skipWaiting()
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  )
  self.clients.claim()
})

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url)
  // API 与 WebSocket 一律走网络，不缓存
  if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') return
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
      .catch(() => caches.match(e.request).then((r) => r || caches.match('/index.html')))
  )
})
