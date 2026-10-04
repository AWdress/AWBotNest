import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const flush = () => new Promise(resolve => setImmediate(resolve))

test('activity summary reads the selected timeline, including empty data', () => {
  const source = readFileSync(new URL('../src/views/Status.vue', import.meta.url), 'utf8')
  const fn = source.match(/function activityTotal\(data\) \{[\s\S]*?\n\}/)[0]
  const total = runInNewContext(`(${fn})`)
  assert.equal(total({ totals: { ai: 3, checkin: 2 } }), 5)
  assert.equal(total({ totals: { ai: 10 } }), 10)
  assert.equal(total(undefined), 0)
  assert.match(source, /activityTotal\(activityRange.value === '7d' \? next.activity_7d : next.activity\)/)
})

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

function configPage(apiOverrides = {}, extra = {}) {
  return page('Plugins', {
    getPluginConfig: async () => ({ schema: { enabled: { type: 'boolean' } }, values: { enabled: true } }),
    getPluginAccounts: async id => ({ accounts: [], selected: [id] }),
    getBotsRouting: async () => ({ bots: [], plugins: [] }),
    clearCache() {},
    ...apiOverrides,
  }, 'openConfig, closeConfig, configOpen, configTarget, configValues, configSaving, saveConfig, '
    + 'acctSelected, acctAllMode, acctSaving, saveAccounts, toggleAcct, '
    + 'configBotChoice, configBotConfirmed, configBotSaving, saveConfigBot, '
    + 'webhookPath, webhookSecret', extra)
}

for (const outcome of ['success', 'failure']) {
  test(`old account ${outcome} cannot overwrite another dialog or finish its active save`, async () => {
    const old = deferred(), current = deferred(), writes = []
    const p = configPage({ setPluginAccounts: (id, sessions) => {
      writes.push([id, [...sessions]])
      return id === 'a' ? old.promise : current.promise
    } })
    await p.openConfig({ id: 'a', scope: 'user' }); await flush()
    const savingOld = p.toggleAcct('extra-a')
    p.closeConfig()
    await p.openConfig({ id: 'b', scope: 'user' }); await flush()
    const savingCurrent = p.toggleAcct('extra-b')
    if (outcome === 'success') old.resolve({ selected: ['a', 'extra-a'] })
    else old.reject(new Error('old request failed'))
    await savingOld
    assert.deepEqual(Array.from(p.acctSelected.value), ['b', 'extra-b'])
    assert.equal(p.acctSaving.value, true)
    assert.deepEqual(writes, [['a', ['a', 'extra-a']], ['b', ['b', 'extra-b']]])
    current.resolve({ selected: ['b', 'extra-b'] }); await savingCurrent
    assert.equal(p.acctSaving.value, false)
  })

  test(`old channel ${outcome} cannot overwrite new choices or finish a new save`, async () => {
    const old = deferred(), current = deferred()
    const p = configPage({ setBotRouting: id => id === 'a' ? old.promise : current.promise })
    await p.openConfig({ id: 'a', scope: 'standalone' }); await flush()
    p.configBotChoice.value = ['channel-a']
    const savingOld = p.saveConfigBot()
    p.closeConfig()
    await p.openConfig({ id: 'b', scope: 'standalone' }); await flush()
    p.configBotChoice.value = ['channel-b']
    const savingCurrent = p.saveConfigBot()
    if (outcome === 'success') old.resolve({ bot: 'channel-a' })
    else old.reject(new Error('old request failed'))
    await savingOld
    assert.deepEqual(Array.from(p.configBotChoice.value), ['channel-b'])
    assert.equal(p.configBotSaving.value, true)
    current.resolve({ bot: 'channel-b' }); await savingCurrent
    assert.deepEqual(Array.from(p.configBotConfirmed.value), ['channel-b'])
  })

  test(`old configuration ${outcome} cannot close a new dialog or display its error`, async () => {
    const old = deferred(), current = deferred(), errors = []
    const p = configPage({ setPluginConfig: id => id === 'a' ? old.promise : current.promise }, {
      toast: { success() {}, error: message => errors.push(message) },
    })
    await p.openConfig({ id: 'a' }); await flush()
    const savingOld = p.saveConfig()
    p.closeConfig()
    await p.openConfig({ id: 'b' }); await flush()
    const savingCurrent = p.saveConfig()
    if (outcome === 'success') old.resolve({ ok: true })
    else old.reject(new Error('old request failed'))
    await savingOld
    assert.equal(p.configOpen.value, true)
    assert.equal(p.configTarget.value.id, 'b')
    assert.equal(p.configSaving.value, true)
    assert.deepEqual(errors, [])
    current.resolve({ ok: true }); await savingCurrent
    assert.equal(p.configOpen.value, false)
  })
}

test('late webhook response stays with its original dialog', async () => {
  const first = deferred(), second = deferred()
  const p = configPage({ getPluginWebhook: id => id === 'a' ? first.promise : second.promise })
  await p.openConfig({ id: 'a', webhook: true })
  p.closeConfig()
  await p.openConfig({ id: 'b', webhook: true })
  second.resolve({ path: '/b', secret: 'b' }); await flush()
  first.resolve({ path: '/a', secret: 'a' }); await flush()
  assert.equal(p.webhookPath.value, '/b')
  assert.equal(p.webhookSecret.value, 'b')
})

test('closed configuration cannot reopen when its initial request completes', async () => {
  const pending = deferred()
  const p = configPage({ getPluginConfig: () => pending.promise })
  const opening = p.openConfig({ id: 'a' })
  p.closeConfig()
  pending.resolve({ schema: {}, values: {} }); await opening
  assert.equal(p.configOpen.value, false)
  assert.equal(p.configTarget.value, null)
})

test('unmount invalidates in-flight configuration changes', async () => {
  const pending = deferred()
  const p = configPage({ setPluginAccounts: () => pending.promise })
  await p.openConfig({ id: 'a', scope: 'user' }); await flush()
  const saving = p.toggleAcct('extra-a')
  p.unmounted.forEach(fn => fn())
  pending.resolve({ selected: ['late'] }); await saving
  assert.equal(p.configTarget.value, null)
  assert.equal(p.acctSaving.value, false)
  assert.notEqual(p.acctSelected.value[0], 'late')
})

function field(props, api = {}) {
  const source = readFileSync(new URL('../src/components/FieldInput.vue', import.meta.url), 'utf8')
    .match(/<script setup>([\s\S]*?)<\/script>/)[1]
    .replace(/^import[\s\S]*?from ['"][^'"]+['"]\s*;?\s*$/gm, '')
  const values = [], mounted = [], unmounted = []
  const actualProps = { name: 'choice', path: [], pluginId: 'test', ...props }
  const sandbox = {
    ref: value => ({ value }), computed: fn => ({ get value() { return fn() } }),
    defineProps: () => actualProps, defineEmits: () => (_, value) => values.push(value),
    onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn),
    api, toast: { error() {}, success() {} }, confirm: async () => true,
  }
  runInNewContext(`${source}\nglobalThis.result = { selectValue, revealSecret, addRow, delRow, secretLoading }`, sandbox)
  return { ...sandbox.result, props: actualProps, values, mounted, unmounted }
}

test('select keeps numeric and boolean option types and migrates old string values', () => {
  for (const [options, previous, selected] of [[[1, 2], '2', 2], [[true, false], 'false', false]]) {
    const p = field({ spec: { type: 'select', options }, value: previous })
    p.mounted.forEach(fn => fn())
    assert.equal(p.values.at(-1), selected)
    p.selectValue({ target: { selectedIndex: 1, value: String(selected) } })
    assert.equal(p.values.at(-1), selected)
    assert.equal(typeof p.values.at(-1), typeof selected)
    const reopened = field({ spec: { type: 'select', options }, value: selected })
    reopened.mounted.forEach(fn => fn())
    assert.equal(reopened.values.length, 0)
  }
})

test('nested secrets use escaped JSON Pointer and whole masked lists retain array values', async () => {
  const paths = []
  const api = { revealPluginSecret: async (id, path) => { paths.push([id, path]); return { value: [{ cookie: 'real' }] } } }
  const nested = field({ spec: { type: 'password' }, name: 'cookie', path: ['accounts/a~b', 1, 'cookie'], value: '********' }, api)
  await nested.revealSecret()
  assert.deepEqual(paths[0], ['test', '/accounts~1a~0b/1/cookie'])
  const list = field({ spec: { type: 'list', secret: true }, name: 'accounts', value: '********' }, api)
  list.addRow()
  assert.equal(list.values.length, 0)
  await list.revealSecret()
  assert.equal(Array.isArray(list.values[0]), true)
})

test('deleting list rows reads secrets at old positions before moving the retained row', async () => {
  const paths = []
  const p = field({ name: 'accounts', spec: { type: 'list', fields: { cookie: { type: 'password' } } },
    value: [{ cookie: '********' }, { cookie: '********' }] }, {
    revealPluginSecret: async (id, path) => { paths.push(path); return { value: path.includes('/0/') ? 'first' : 'second' } },
  })
  await p.delRow(0)
  assert.deepEqual(paths.sort(), ['/accounts/0/cookie', '/accounts/1/cookie'])
  assert.equal(p.values[0].length, 1)
  assert.equal(p.values[0][0].cookie, 'second')
})

test('failed secret lookup preserves list rows and stale reveal cannot update another field', async () => {
  const p = field({ name: 'accounts', spec: { type: 'list', fields: { cookie: { type: 'password' } } },
    value: [{ cookie: '********' }, { cookie: '********' }] }, {
    revealPluginSecret: async () => { throw new Error('offline') },
  })
  await p.delRow(0)
  assert.equal(p.values.length, 0)
  assert.equal(p.props.value.length, 2)
  const pending = deferred()
  const secret = field({ spec: { type: 'password' }, value: '********' }, { revealPluginSecret: () => pending.promise })
  const revealing = secret.revealSecret()
  secret.props.pluginId = 'new-plugin'
  pending.resolve({ value: 'old-secret' }); await revealing
  assert.equal(secret.values.length, 0)
})

test('same-version old installations stay out of installation and update lists', async () => {
  const calls = [], prompts = []
  const p = page('Plugins', {
    storeDownload: async plugins => { calls.push(plugins); return { result: {} } },
  }, 'updateAll, store, storeAvailable, storeUpdatable', {
    confirm: async options => { prompts.push(options); return true },
  })
  const plugin = { id: 'source-test', name: '来源测试', installed: true, from_manifest: true,
    version: '1.0', local_version: '1.0', source_confirmed: false, update_available: false,
    repo: 'Example/AWBotNest-Plugins', path: 'plugins_v2/source-test/' }
  p.store.value = [plugin]
  assert.equal(p.storeAvailable.value.length, 0)
  assert.equal(p.storeUpdatable.value.length, 0)
  await p.updateAll()
  assert.equal(calls.length, 0)
  assert.equal(prompts.length, 0)
  p.store.value.push({ id: 'new-plugin', installed: false })
  assert.equal(p.storeAvailable.value.length, 1)
  assert.equal(p.storeAvailable.value[0].id, 'new-plugin')
})

test('manual updating records the source without a separate confirmation or repeated update', async () => {
  const calls = [], prompts = []
  const p = page('Plugins', {
    listPlugins: async () => ({ plugins: [] }),
    storeDownload: async plugins => { calls.push(plugins[0].id); return { result: {} } },
  }, 'download, updateAll, store, storeUpdatable, dlBusy', {
    confirm: async options => { prompts.push(options); return false },
  })
  const plugin = { id: 'source-test', installed: true, from_manifest: true,
    version: '2.0', local_version: '1.0', source_confirmed: false,
    update_available: true, auto_update_allowed: false }
  p.store.value = [plugin]
  assert.equal(p.storeUpdatable.value.length, 1)
  await p.download(plugin)
  assert.deepEqual(calls, ['source-test'])
  assert.equal(prompts.length, 0)
  assert.equal(plugin.source_confirmed, true)
  assert.equal(plugin.auto_update_allowed, true)
  assert.equal(plugin.installed_version, '2.0')
  assert.equal(plugin.local_version, '2.0')
  assert.equal(plugin.update_available, false)
  assert.equal(p.storeUpdatable.value.length, 0)
  assert.equal(p.dlBusy.value[plugin.id], false)
  await p.updateAll()
  assert.deepEqual(calls, ['source-test'])
})

test('batch updating old installations does not ask for source confirmation', async () => {
  const prompts = [], calls = []
  const p = page('Plugins', {
    listPlugins: async () => ({ plugins: [] }),
    pluginStore: async () => ({ plugins: [] }),
    storeDownload: async plugins => { calls.push(plugins.map(plugin => plugin.id)); return { result: {} } },
  }, 'updateAll, store, updateAllBusy', {
    confirm: async options => { prompts.push(options); return false },
  })
  p.store.value = ['first', 'second'].map(id => ({ id, installed: true, from_manifest: true,
    version: '2', local_version: '1', source_confirmed: false, update_available: true,
    repo: `Example-${id}/AWBotNest-Plugins`, path: `plugins_v2/${id}/` }))
  await p.updateAll()
  assert.equal(prompts.length, 0)
  assert.deepEqual(calls.map(chunk => [...chunk]), [['first', 'second']])
  assert.equal(p.updateAllBusy.value, false)
})

test('store follows the server version comparison and does not offer older versions as updates', () => {
  const p = page('Plugins', {}, 'hasUpdate')
  assert.equal(p.hasUpdate({ installed: true, from_manifest: true, local_version: '2.0', version: '1.0', update_available: false }), false)
  assert.equal(p.hasUpdate({ installed: true, from_manifest: true, local_version: '1.0', version: '2.0', update_available: true }), true)
  assert.equal(p.hasUpdate({ installed: true, from_manifest: true, local_version: '2.0', version: '1.0' }), false)
  assert.equal(p.hasUpdate({ installed: true, from_manifest: true, local_version: '1.9', version: '1.10' }), true)
  assert.equal(p.hasUpdate({ installed: true, from_manifest: true, local_version: '1.0', version: '1.0.0' }), false)
})

function settingsPage(api, expose, extra = {}) {
  return page('Settings', api, expose, {
    localStorage: { getItem: () => '' },
    uiProfile: { value: { username: 'admin' } },
    onBeforeRouteLeave() {},
    location: { origin: 'https://system.example.test' },
    ...extra,
  })
}

function channelSettingsApi(calls, plugins = []) {
  let saved = []
  return {
    getBotsRouting: async () => ({ bots: [], plugins }),
    saveNotificationChannels: async channels => {
      saved = JSON.parse(JSON.stringify(channels))
      calls.push(saved)
      return {}
    },
    getSettings: async () => ({ settings: { NOTIFICATION_CHANNELS: saved } }),
    setBotRouting: async () => ({}),
  }
}

test('WeCom callback defaults off for new and old channels and encodes its URL', async () => {
  const copied = []
  const p = settingsPage({ getBotsRouting: async () => ({ bots: [], plugins: [] }) },
    's, selectChannelType, openEditChannel, channelForm, wecomCallbackUrl, copyWecomCallbackUrl', {
      navigator: { clipboard: { writeText: async text => { copied.push(text) } } },
      window: { isSecureContext: true },
    })
  p.s.value = { NOTIFICATION_CHANNELS: [{ id: 'wecom/业务?#', type: 'wechat', config: { corpid: 'corp' } }] }
  await p.selectChannelType('wechat')
  assert.equal(p.channelForm.value.config.callback_enabled, false)
  assert.equal(p.channelForm.value.config.callback_token, '')
  assert.equal(p.channelForm.value.config.callback_aes_key, '')
  assert.equal(p.channelForm.value.config.callback_users, '')
  assert.equal(p.channelForm.value.plugins.length, 0)
  await p.openEditChannel(0)
  assert.equal(p.channelForm.value.config.callback_enabled, false)
  assert.equal(p.channelForm.value.config.corpid, 'corp')
  const url = `https://system.example.test/api/wecom/callback/${encodeURIComponent('wecom/业务?#')}`
  assert.equal(p.wecomCallbackUrl.value, url)
  await p.copyWecomCallbackUrl()
  assert.deepEqual(copied, [url])
})

test('WeCom one-way notifications need no callback credentials and disabled fields survive save', async () => {
  const calls = []
  const p = settingsPage(channelSettingsApi(calls), 's, selectChannelType, openEditChannel, channelForm, saveChannel')
  p.s.value = { NOTIFICATION_CHANNELS: [] }
  await p.selectChannelType('wechat')
  p.channelForm.value.name = '企业通知'
  await p.saveChannel()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0].config.callback_enabled, false)
  assert.equal(calls[0][0].config.callback_token, '')
  await p.openEditChannel(0)
  p.channelForm.value.config.callback_token = 'keep-token'
  p.channelForm.value.config.callback_aes_key = 'keep-key'
  p.channelForm.value.config.callback_users = 'operator'
  await p.saveChannel()
  assert.equal(calls[1][0].config.callback_enabled, false)
  assert.equal(calls[1][0].config.callback_token, 'keep-token')
  assert.equal(calls[1][0].config.callback_aes_key, 'keep-key')
  assert.equal(calls[1][0].config.callback_users, 'operator')
})

test('WeCom enabled callbacks require real new secrets and named members before saving', async () => {
  const calls = [], errors = []
  const p = settingsPage(channelSettingsApi(calls), 's, selectChannelType, channelForm, saveChannel', {
    toast: { success() {}, error: message => errors.push(message) },
  })
  p.s.value = { NOTIFICATION_CHANNELS: [] }
  await p.selectChannelType('wechat')
  p.channelForm.value.name = '企业指令'
  const config = p.channelForm.value.config
  Object.assign(config, { corpid: 'corp', agentid: '1', secret: 'app-secret' })
  config.callback_enabled = true
  await p.saveChannel()
  assert.match(errors.at(-1), /回调 Token/)
  config.callback_token = '********'
  await p.saveChannel()
  assert.match(errors.at(-1), /回调 Token/)
  config.callback_token = 'callback-token'
  await p.saveChannel()
  assert.match(errors.at(-1), /EncodingAESKey/)
  config.callback_aes_key = '********'
  await p.saveChannel()
  assert.match(errors.at(-1), /EncodingAESKey/)
  config.callback_aes_key = 'a'.repeat(43)
  config.callback_users = ' | | '
  await p.saveChannel()
  assert.match(errors.at(-1), /允许操作的成员/)
  assert.equal(calls.length, 0)
  config.callback_users = 'alice'
  config.callback_token = 'has space'
  await p.saveChannel()
  assert.match(errors.at(-1), /不能包含空白/)
  config.callback_token = 'a'.repeat(129)
  await p.saveChannel()
  assert.match(errors.at(-1), /128/)
  for (const control of ['\x00', '\x01', '\x1f', '\x7f']) {
    config.callback_token = `token${control}value`
    await p.saveChannel()
    assert.match(errors.at(-1), /控制字符/)
  }
  config.callback_token = 'callback-token'
  config.callback_aes_key = 'short-key'
  await p.saveChannel()
  assert.match(errors.at(-1), /43 位 Base64/)
  config.callback_aes_key = 'A'.repeat(43)
  config.callback_users = 'bad member'
  await p.saveChannel()
  assert.match(errors.at(-1), /每个 UserID/)
  config.callback_users = Array.from({ length: 65 }, (_, index) => `user${index}`).join('|')
  await p.saveChannel()
  assert.match(errors.at(-1), /最多 64 个/)
  config.callback_users = 'a'.repeat(65)
  await p.saveChannel()
  assert.match(errors.at(-1), /每个 UserID/)
  for (const users of ['@all', 'alice|@ALL']) {
    config.callback_users = users
    await p.saveChannel()
    assert.match(errors.at(-1), /不能使用 @all/)
  }
  config.callback_users = ' alice | bob '
  await p.saveChannel()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0].config.callback_users, 'alice|bob')
})

test('WeCom editing preserves masked callback secrets and only selected plugin routing', async () => {
  const calls = [], routes = [], errors = []
  const plugins = [{ id: 'selected', scope: 'standalone', bot: 'wecom' }, { id: 'other', scope: 'standalone', bot: '' }]
  const api = channelSettingsApi(calls, plugins)
  api.setBotRouting = async (id, bot) => { routes.push({ id, bot }) }
  const p = settingsPage(api, 's, openEditChannel, channelForm, saveChannel', {
    toast: { success() {}, error: message => errors.push(message) },
  })
  p.s.value = { NOTIFICATION_CHANNELS: [{ id: 'wecom', type: 'wechat', name: '企业指令', enabled: true,
    config: { corpid: 'corp', agentid: '1', secret: '********',
      callback_enabled: true, callback_token: '********', callback_aes_key: '********', callback_users: 'alice' } }] }
  await p.openEditChannel(0)
  assert.deepEqual([...p.channelForm.value.plugins], ['selected'])
  await p.saveChannel()
  assert.equal(errors.length, 0)
  assert.equal(calls[0][0].config.callback_token, '********')
  assert.equal(calls[0][0].config.callback_aes_key, '********')
  assert.equal(calls[0][0].plugins, undefined)
  assert.deepEqual(routes, [])
  p.s.value.NOTIFICATION_CHANNELS[0].config.callback_aes_key = ''
  await p.openEditChannel(0)
  p.channelForm.value.config.callback_aes_key = '********'
  await p.saveChannel()
  assert.match(errors.at(-1), /EncodingAESKey/)
  assert.equal(calls.length, 1)
})

test('WeCom enabled callbacks require valid application credentials and reject group webhooks', async () => {
  const calls = [], errors = []
  const p = settingsPage(channelSettingsApi(calls), 's, selectChannelType, channelForm, saveChannel', {
    toast: { success() {}, error: message => errors.push(message) },
  })
  p.s.value = { NOTIFICATION_CHANNELS: [] }
  await p.selectChannelType('wechat')
  p.channelForm.value.name = '企业指令'
  const valid = { callback_enabled: true, corpid: 'corp', agentid: '1', secret: 'app-secret',
    callback_token: 'callback-token', callback_aes_key: 'A'.repeat(43), callback_users: 'alice' }
  for (const [change, error] of [
    [{ corpid: '' }, /企业 ID/],
    [{ agentid: '0' }, /正整数/],
    [{ agentid: '1.5' }, /正整数/],
    [{ agentid: '2147483648' }, /小于 2147483648/],
    [{ agentid: '999999999999999' }, /正整数/],
    [{ agentid: '00000000001' }, /正整数/],
    [{ secret: '' }, /应用 Secret/],
    [{ secret: '********' }, /应用 Secret/],
    [{ url: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=demo' }, /群机器人/],
    [{ webhook: 'demo' }, /群机器人/],
  ]) {
    p.channelForm.value.config = { ...valid, ...change }
    await p.saveChannel()
    assert.match(errors.at(-1), error)
  }
  assert.equal(calls.length, 0)
  p.channelForm.value.config = { ...valid, agentid: '2147483647' }
  await p.saveChannel()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0].config.agentid, '2147483647')
})

test('WeCom callback secrets load on demand and stale or edited responses do not change the form', async () => {
  const first = deferred(), second = deferred(), reads = []
  const p = settingsPage({
    getBotsRouting: async () => ({ bots: [], plugins: [] }),
    revealSecret: (kind, field, id) => {
      reads.push({ kind, field, id })
      return reads.length === 1 ? first.promise : second.promise
    },
  }, 's, openEditChannel, channelForm, revealChannelSecret')
  p.s.value = { NOTIFICATION_CHANNELS: ['first', 'second'].map(id => ({ id, type: 'wechat', config: {
    callback_enabled: true, callback_token: '********', callback_aes_key: '********', callback_users: 'alice',
  } })) }
  await p.openEditChannel(0)
  assert.equal(reads.length, 0)
  const old = p.revealChannelSecret('callback_token')
  await p.openEditChannel(1)
  first.resolve({ value: 'first-token' })
  await old
  assert.equal(p.channelForm.value.config.callback_token, '********')
  const edited = p.revealChannelSecret('callback_aes_key')
  p.channelForm.value.config.callback_aes_key = 'new-key'
  second.resolve({ value: 'second-key' })
  await edited
  assert.equal(p.channelForm.value.config.callback_aes_key, 'new-key')
  assert.deepEqual(reads, [
    { kind: 'channel', field: 'callback_token', id: 'first' },
    { kind: 'channel', field: 'callback_aes_key', id: 'second' },
  ])
})
