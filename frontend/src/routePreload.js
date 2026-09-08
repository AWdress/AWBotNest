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
  { path: '/status', component: Status, meta: { title: '运行概览' } },
  { path: '/settings', component: asyncViews['/settings'], meta: { title: '系统设置' } },
]

export function preloadRoute(path) {
  const loader = asyncViews[path]
  return loader ? loader().catch(() => null) : Promise.resolve(null)
}

export function preloadAllRoutes() {
  return Promise.all(Object.values(asyncViews).map(loader => loader().catch(() => null)))
}
