import Status from './views/Status.vue'

const asyncViews = {
  '/plugins': () => import('./views/Plugins.vue'),
  '/accounts': () => import('./views/Accounts.vue'),
  '/logs': () => import('./views/Logs.vue'),
  '/settings': () => import('./views/Settings.vue'),
}

export const appRoutes = [
  { path: '/', redirect: '/status' },
  { path: '/plugins', component: asyncViews['/plugins'], meta: { title: '插件管理' } },
  { path: '/accounts', component: asyncViews['/accounts'], meta: { title: '账号管理' } },
  { path: '/logs', component: asyncViews['/logs'], meta: { title: '运行日志' } },
  { path: '/status', component: Status, meta: { title: '系统状态' } },
  { path: '/settings', component: asyncViews['/settings'], meta: { title: '系统设置' } },
]

export function preloadRoute(path) {
  const loader = asyncViews[path]
  return loader ? loader().catch(() => null) : Promise.resolve(null)
}

export function scheduleDeferredRoutePreload() {
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection
  if (connection?.saveData || /(^|-)2g$/.test(connection?.effectiveType || '')) return () => {}

  let cancelled = false
  const preload = async () => {
    for (const path of ['/accounts', '/plugins', '/logs', '/settings']) {
      if (cancelled) return
      await preloadRoute(path)
    }
  }

  if ('requestIdleCallback' in window) {
    const id = window.requestIdleCallback(preload, { timeout: 2500 })
    return () => { cancelled = true; window.cancelIdleCallback(id) }
  }

  const id = window.setTimeout(preload, 800)
  return () => { cancelled = true; window.clearTimeout(id) }
}
