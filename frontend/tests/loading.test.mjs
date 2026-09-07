import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const flush = () => new Promise(resolve => setImmediate(resolve))

function page(file, api, expose, extra = {}) {
  const source = readFileSync(new URL(`../src/views/${file}.vue`, import.meta.url), 'utf8')
    .match(/<script setup>([\s\S]*?)<\/script>/)[1]
    .replace(/^import[\s\S]*?from ['"][^'"]+['"]\s*;?\s*$/gm, '')
  const mounted = [], unmounted = [], idle = new Map()
  let nextId = 0
  const sandbox = {
    ref: value => ({ value }), computed: fn => ({ get value() { return fn() } }),
    watch() {}, onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn),
    nextTick: () => Promise.resolve(), api, logo: '', formatLogTime: () => '',
    getToken: () => 'test', toast: { success() {}, error() {} }, confirm: async () => true,
    subscribeNotificationSync: () => () => {}, publishNotificationSync() {},
    document: { addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {},
      requestIdleCallback(fn) { idle.set(++nextId, fn); return nextId },
      cancelIdleCallback(id) { idle.delete(id) }, clearTimeout() {} },
    setTimeout: () => 0, clearTimeout() {}, ...extra,
  }
  runInNewContext(`${source}\nglobalThis.result = { ${expose} }`, sandbox)
  return { ...sandbox.result, mounted, unmounted, idle }
}

test('market preloads local data at idle and reuses an empty successful result', async () => {
  const calls = []
  const p = page('Plugins', {
    listPlugins: async () => ({ plugins: [] }),
    pluginStore: async refresh => { calls.push(refresh); return { plugins: [] } },
  }, 'goStore, loadStore, tab')
  p.mounted.forEach(fn => fn())
  await flush()
  assert.equal(p.idle.size, 1)
  p.idle.values().next().value()
  await flush()
  p.goStore()
  p.tab.value = 'mine'
  p.goStore()
  await flush()
  assert.deepEqual(calls, [false])
  p.unmounted.forEach(fn => fn())
})

test('manual refresh waits for in-flight preload and still requests fresh data', async () => {
  const first = deferred(), calls = []
  const p = page('Plugins', {
    listPlugins: async () => ({ plugins: [] }),
    pluginStore: refresh => { calls.push(refresh); return refresh ? Promise.resolve({ plugins: [] }) : first.promise },
  }, 'goStore, loadStore')
  p.mounted.forEach(fn => fn())
  p.goStore()
  const refresh = p.loadStore(true)
  assert.deepEqual(calls, [false])
  first.resolve({ plugins: [] })
  await refresh
  assert.deepEqual(calls, [false, true])
  p.unmounted.forEach(fn => fn())
})

test('a slower plugin list response cannot overwrite a newer result', async () => {
  const first = deferred(), second = deferred()
  let n = 0
  const p = page('Plugins', { listPlugins: () => (++n === 1 ? first.promise : second.promise) }, 'load, plugins')
  const old = p.load(), current = p.load()
  second.resolve({ plugins: [{ id: 'new' }] })
  await current
  first.resolve({ plugins: [{ id: 'old' }] })
  await old
  assert.equal(p.plugins.value[0].id, 'new')
})

test('account page waits for configuration status and displays password rejection', async () => {
  const status = { value: null }, ready = deferred()
  const p = page('Accounts', {
    listAccounts: async () => ({ accounts: [] }),
    loginSubmitPassword: async () => ({ authorized: false, error: '两步验证密码错误' }),
  }, 'load, loading, telegramConfigured, submitPassword, form, wizardErr', {
    platformStatus: status,
    refreshPlatformStatus: () => ready.promise,
  })
  const loading = p.load()
  await flush()
  assert.equal(p.loading.value, true)
  assert.equal(p.telegramConfigured.value, true)
  status.value = { telegram_configured: true }
  ready.resolve(status.value)
  await loading
  p.form.value.password = 'wrong'
  await p.submitPassword()
  assert.equal(p.wizardErr.value, '两步验证密码错误')
})

test('plugin card allows file upload drops without treating them as sorting', () => {
  const p = page('Plugins', {}, 'allowPluginDrop, dragging, draggedPluginId')
  let prevented = false
  p.allowPluginDrop({ dataTransfer: { types: ['Files'] }, preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  assert.equal(p.dragging.value, true)
  assert.equal(p.draggedPluginId.value, '')
})

test('install batch preserves reload results and continues after a failed plugin', async () => {
  let source = readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8').replace(/\bexport /g, '')
  const calls = []
  const sandbox = {
    localStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    fetch: async (url, options) => {
      const plugin = JSON.parse(options.body).plugin
      calls.push(plugin.id)
      return plugin.id === 'bad'
        ? { ok: false, status: 409, json: async () => ({ detail: 'broken' }) }
        : { ok: true, json: async () => ({ ok: true, plugin: { loaded: true, install_count: 12 } }) }
    },
  }
  runInNewContext(`${source}\nglobalThis.client = api`, sandbox)
  const { result } = await sandbox.client.storeDownload([{ id: 'bad', name: '失败插件' }, { id: 'good' }])
  assert.deepEqual(calls, ['bad', 'good'])
  assert.equal(result.reloaded[0], 'good')
  assert.equal(result.install_counts.good, 12)
  assert.match(result.errors[0], /失败插件.*broken/)
})
