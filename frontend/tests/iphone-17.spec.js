import { expect, test } from '@playwright/test'
import { join } from 'node:path'

// Assertion-only red/green runs do not consume visual inspection rounds.
test.use({ screenshot: process.env.AWBOTNEST_ASSERTIONS_ONLY ? 'off' : 'only-on-failure' })

const status = {
  version: '2.0.0.6', telegram_configured: false, clients: [], accounts: [],
  user_count: 0, bot_connected: false, uptime_seconds: 120,
  resources: { cpu_percent: 8, memory_percent: 21, memory_used_mb: 128 },
  scheduler_jobs: [], plugin_names: {},
  plugins: { total: 0, enabled: 0 },
  activity: { buckets: [], totals: {}, success_totals: {} },
  activity_7d: { buckets: [], totals: {}, success_totals: {} },
}

const settings = {
  API_ID: '', API_HASH: '', BOT_TOKEN: '', BOT_NAME: '主要通知渠道',
  DEFAULT_BOT_ID: 'default', DEFAULT_BOT_CHAT_ID: '', BOTS: [], ACCOUNTS: [],
  WEB_UI_PORT: 18001, WEB_UI_URL: '0.0.0.0', NOTIFICATION_CHANNELS: [],
  proxy_set: { proxy_enable: false, PROXY_URL: '', proxy: {} },
  PIP_INDEX_URL: '', GITHUB_TOKEN: '', DB_INFO: {}, LOG_CLEANER: {
    enabled: true, keep_lines: 1000, hour: 3, minute: 0,
  },
  BROWSER_ENGINE: 'chromium', CLOAKBROWSER_USE_FREE_KEY: false, CLOAKBROWSER_LICENSE_KEY: '',
  WEBHOOK_SECRET: '', API_KEY: '', PLUGIN_REPOS: ['Example/AWBotNest-Plugins'],
}

const aiSettings = {
  providers: [{
    id: 'primary', name: '主 AI 服务', enabled: true,
    base_url: 'https://api.example.test/v1', api_key: '********', api_format: 'auto',
  }],
  models: [{
    id: 'text', alias: 'fast', name: '快速模型', enabled: true,
    provider_id: 'primary', model: 'gpt-test', capabilities: ['text'],
  }],
  capabilities: { text: { default_model: 'text', fallback_model: '' }, vision: {}, image: {} },
  plugin_permissions: {}, timeout_seconds: 60, image_timeout_seconds: 300, max_concurrency: 3,
}

const configurablePlugin = {
  id: 'mobile_config_test', name: '手机配置测试', version: '1.0.0', enabled: true,
  description: '用于验证手机端插件配置弹窗', scope: 'standalone', render_mode: 'schema',
  author: 'AWBotNest', tags: ['测试'], error: '',
}

function json(route, body) {
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => console.error('Browser page error:', error.message))
  await page.addInitScript(() => {
    localStorage.setItem('awbotnest_token', 'mobile-test-token')
    localStorage.setItem('awbotnest-theme', 'dark')
  })
  await page.route('https://api.github.com/**', (route) => json(route, []))
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/auth/status') return json(route, { needs_setup: false, must_change_password: false })
    if (path === '/api/auth/resource_token') return json(route, { ok: true })
    if (path === '/api/ui/profile') return json(route, { username: 'mobile-admin', avatar_url: '' })
    if (path === '/api/status') return json(route, status)
    if (path === '/api/settings') return json(route, { settings })
    if (path === '/api/browser/status') return json(route, {
      engine: 'chromium', cloakbrowser_installed: true, cloakbrowser_version: '0.5.10',
      cloakbrowser_kernels: [{ channel: 'legacy_free', version: '146.0.7680.177.5' }],
      key_configured: false, key_enabled: false, key_active: false,
      binary_mode: 'legacy_free',
      update_check: { status: 'disabled', update_available: false },
      queue_enabled: false, active: false, waiting: 0,
      maintenance: false, cooldown_seconds: 0,
    })
    if (path === '/api/ai/settings') return json(route, {
      settings: aiSettings,
      status: { configured: true, detected_protocols: { primary: 'responses' }, usage: {
        total: 8, succeeded: 7, failed: 1, active: 0,
        input_tokens: 1200, output_tokens: 340, total_tokens: 1540,
      } },
    })
    if (path === '/api/ai/plugins') return json(route, { plugins: [] })
    if (path === '/api/ai/usage/overview') return json(route, {
      items: [{
        timestamp: '2026-09-10T21:52:32+08:00', source: '平台', plugin_id: '',
        capability: 'text', provider: '主 AI 服务', model: 'gpt-test', protocol: 'responses',
        status: 'success', latency_ms: 1420, total_tokens: 88, error_type: '', error_message: '',
      }, {
        timestamp: '2026-09-10T21:50:02+08:00', source: '插件:多站签到', plugin_id: 'pt_multi_checkin',
        capability: 'vision', provider: '主 AI 服务', model: 'gpt-test', protocol: 'responses',
        status: 'failed', latency_ms: 2648, total_tokens: 0,
        error_type: 'invalid_response', error_message: '协议不匹配：接口返回了 HTML 页面',
      }],
      plugins: [{
        plugin_id: 'pt_multi_checkin', name: '多站签到', calls: 1, succeeded: 0,
        failed: 1, fallbacks: 0, avg_latency_ms: 2648, total_tokens: 0,
      }],
      total_items: 2,
    })
    if (path === '/api/ai/usage/recent') return json(route, { items: [{
      timestamp: '2026-09-10T21:52:32+08:00', source: '平台', plugin_id: '',
      capability: 'text', provider: '主 AI 服务', model: 'gpt-test', protocol: 'responses',
      status: 'success', latency_ms: 1420, total_tokens: 88, error_type: '', error_message: '',
    }, {
      timestamp: '2026-09-10T21:50:02+08:00', source: '插件:多站签到', plugin_id: 'pt_multi_checkin',
      capability: 'vision', provider: '主 AI 服务', model: 'gpt-test', protocol: 'responses',
      status: 'failed', latency_ms: 2648, total_tokens: 0,
      error_type: 'invalid_response', error_message: '协议不匹配：接口返回了 HTML 页面',
    }] })
    if (path === '/api/ai/usage/plugins') return json(route, { items: [{
      plugin_id: 'pt_multi_checkin', name: '多站签到', calls: 1, succeeded: 0,
      failed: 1, fallbacks: 0, avg_latency_ms: 2648, total_tokens: 0,
    }] })
    if (path === '/api/ai/status') return json(route, {
      configured: true, detected_protocols: { primary: 'responses' },
      usage: { total: 8, succeeded: 7, failed: 1, active: 0,
        input_tokens: 1200, output_tokens: 340, total_tokens: 1540 },
    })
    if (path === '/api/plugins') return json(route, { plugins: [configurablePlugin] })
    if (path === '/api/plugins/mobile_config_test/config') return json(route, {
      schema: {
        enabled: { type: 'boolean', label: '启用测试功能' },
        schedule: { type: 'string', format: 'cron', label: '执行周期' },
        access_token: { type: 'password', label: '访问令牌' },
      },
      values: { enabled: true, schedule: '6 3 * * *', access_token: '********' }, render_mode: 'schema', has_frontend: false,
    })
    if (path === '/api/plugins/mobile_config_test/config/reveal') return json(route, {
      field: 'access_token', value: 'mobile-real-secret',
    })
    if (path === '/api/bots/routing') return json(route, {
      bots: [{ id: 'default', name: '主要通知渠道', type: 'telegram', is_default: true }],
      plugins: [],
    })
    if (path === '/api/accounts') return json(route, { accounts: [] })
    if (path === '/api/logs/recent') return json(route, { logs: [] })
    return json(route, {})
  })
})

async function expectInsideViewport(page) {
  const geometry = await page.evaluate(() => ({
    viewportWidth: document.documentElement.clientWidth,
    documentWidth: document.documentElement.scrollWidth,
    overflowing: [...document.querySelectorAll('body *')].filter((element) => {
      const style = getComputedStyle(element)
      if (style.position === 'fixed' && style.display === 'none') return false
      const rect = element.getBoundingClientRect()
      return rect.left < -1 || rect.right > document.documentElement.clientWidth + 1
    }).slice(0, 5).map((element) => element.className),
  }))
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth + 1)
  expect(geometry.overflowing).toEqual([])
}

async function installVisualViewportFixture(page) {
  await page.addInitScript(({ width, height }) => {
    Object.defineProperty(window.navigator, 'standalone', { configurable: true, value: true })
    const viewport = new EventTarget()
    // Init scripts run before the viewport meta is parsed (innerWidth may still
    // be the browser's 980px default). Seed from the actual test device instead.
    Object.assign(viewport, { width, height, offsetTop: 0, offsetLeft: 0,
      pageTop: 0, pageLeft: 0, scale: 1 })
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport })
    window.__setMobileTestViewport = values => {
      Object.assign(viewport, values)
      viewport.pageTop = viewport.offsetTop
      viewport.dispatchEvent(new Event('resize'))
      viewport.dispatchEvent(new Event('scroll'))
    }
  }, page.viewportSize())
}

async function setVisualViewport(page, values) {
  await page.evaluate(values => window.__setMobileTestViewport(values), values)
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}

async function visibleInputFonts(page) {
  return page.evaluate(() => {
    const excluded = new Set(['checkbox', 'radio', 'hidden', 'range', 'color', 'file', 'button', 'submit', 'reset', 'image'])
    const fields = [...document.querySelectorAll('input, select, textarea, [contenteditable="true"]')]
      .filter(field => !excluded.has(field.type) && field.getClientRects().length
        && getComputedStyle(field).visibility !== 'hidden')
    return {
      count: fields.length,
      offenders: fields.map(field => ({
        field: field.getAttribute('aria-label') || field.getAttribute('placeholder') || field.className,
        fontSize: Number.parseFloat(getComputedStyle(field).fontSize),
      })).filter(field => field.fontSize < 16),
    }
  })
}

async function assertVisibleInputFonts(page, surface) {
  const fonts = await visibleInputFonts(page)
  expect(fonts.count, `${surface} 应实际展示输入控件`).toBeGreaterThan(0)
  expect.soft(fonts.offenders, `${surface} 的移动输入控件不得低于 16px`).toEqual([])
}

async function searchGeometry(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('.search-modal')
    const rect = selector => {
      const box = dialog.querySelector(selector).getBoundingClientRect()
      return { top: box.top, bottom: box.bottom, width: box.width, height: box.height }
    }
    const box = dialog.getBoundingClientRect()
    return { top: box.top, bottom: box.bottom, height: box.height,
      title: rect('.search-title'), close: rect('.search-close'), input: rect('input[type="search"]'),
      footer: rect('.search-foot'), list: rect('.search-list'),
      shellHeight: document.querySelector('#app > .layout').getBoundingClientRect().height,
    }
  })
}

async function waitForSearchLayout(page) {
  const dialog = page.getByRole('dialog', { name: '搜索插件', exact: true })
  await expect(dialog).toBeVisible()
  await expect.poll(() => dialog.evaluate(element =>
    element.getAnimations().some(animation => animation.playState === 'running'))).toBe(false)
}

async function assertKeyboardDialog(page, field, selectors) {
  await field.focus()
  await setVisualViewport(page, { width: 402, height: 470, offsetTop: 84, scale: 1 })
  await expect(page.locator('html')).toHaveClass(/keyboard-open/)
  await expect.poll(() => field.evaluate((field, selectors) => {
    const dialog = document.querySelector(selectors.dialog)
    const box = dialog.getBoundingClientRect()
    const title = dialog.querySelector(selectors.title).getBoundingClientRect()
    const close = dialog.querySelector(selectors.close).getBoundingClientRect()
    const input = field.getBoundingClientRect()
    return { dialogTopSafe: box.top >= 84 + 59 - 1, dialogBottomVisible: box.bottom <= 554 + 1,
      titleSafe: title.top >= 84 + 59 - 1 && title.bottom <= 554,
      closeSafe: close.top >= 84 + 59 - 1 && close.bottom <= 554,
      inputVisible: input.top >= 84 && input.bottom <= 554 - 16 }
  }, selectors)).toEqual({ dialogTopSafe: true, dialogBottomVisible: true,
    titleSafe: true, closeSafe: true, inputVisible: true })
  await assertVisibleInputFonts(page, selectors.dialog)
}

async function injectSafeAreas(page) {
  await page.evaluate(() => {
    document.documentElement.style.setProperty('--safe-area-top', '59px')
    document.documentElement.style.setProperty('--safe-area-bottom', '34px')
  })
}

test.describe('移动输入与可见视口回归', () => {
  test('iPhone 17 搜索输入不触发自动放大并避开 PWA 顶部安全区', async ({ page }) => {
    await installVisualViewportFixture(page)
    await page.goto('/#/plugins')
    await injectSafeAreas(page)
    await page.getByRole('button', { name: '搜索插件', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '搜索插件', exact: true })
    await waitForSearchLayout(page)
    await assertVisibleInputFonts(page, '插件搜索')
    const geometry = await searchGeometry(page)
    expect(geometry.close.top).toBeGreaterThanOrEqual(59)
    expect(geometry.title.top).toBeGreaterThanOrEqual(59)
    expect(geometry.close.width).toBeGreaterThanOrEqual(44)
    expect(geometry.close.height).toBeGreaterThanOrEqual(44)
    const viewportMeta = await page.locator('meta[name="viewport"]').getAttribute('content')
    expect(viewportMeta).not.toMatch(/user-scalable\s*=\s*(?:no|0)|maximum-scale\s*=/i)
    if (process.env.AWBOTNEST_MOBILE_PROOF_DIR) {
      await page.screenshot({ path: join(process.env.AWBOTNEST_MOBILE_PROOF_DIR, 'mobile-search.png') })
    }
  })

  test('iPhone 17 搜索键盘反复弹出和偏移后标题关闭输入与底部仍可见', async ({ page }) => {
    await installVisualViewportFixture(page)
    await page.goto('/#/plugins')
    await page.getByRole('button', { name: '搜索插件', exact: true }).click()
    await waitForSearchLayout(page)
    const initial = await searchGeometry(page)
    for (const viewport of [
      { height: 500, offsetTop: 0, scale: 1 },
      { height: 470, offsetTop: 84, scale: 1 },
      { height: 510, offsetTop: 42, scale: 1 },
      { height: 470, offsetTop: 84, scale: 1 },
    ]) {
      await setVisualViewport(page, viewport)
      await expect(page.locator('html')).toHaveClass(/keyboard-open/)
      await expect.poll(async () => {
        const geometry = await searchGeometry(page)
        const bottom = viewport.offsetTop + viewport.height
        return { title: geometry.title.top >= viewport.offsetTop && geometry.title.bottom <= bottom,
          close: geometry.close.top >= viewport.offsetTop && geometry.close.bottom <= bottom,
          input: geometry.input.top >= viewport.offsetTop && geometry.input.bottom <= bottom,
          footer: geometry.footer.top >= viewport.offsetTop && geometry.footer.bottom <= bottom + 1,
          list: geometry.list.height > 0 && geometry.list.bottom <= geometry.footer.top + 1 }
      }).toEqual({ title: true, close: true, input: true, footer: true, list: true })
      const beforeRepeat = await searchGeometry(page)
      await setVisualViewport(page, viewport)
      const repeated = await searchGeometry(page)
      expect(repeated.top).toBeCloseTo(beforeRepeat.top, 1)
      expect(repeated.height).toBeCloseTo(beforeRepeat.height, 1)
      expect(repeated.shellHeight).toBeCloseTo(initial.shellHeight, 1)
    }
    await setVisualViewport(page, { height: 874, offsetTop: 0, scale: 1 })
    await expect(page.locator('html')).not.toHaveClass(/keyboard-open/)
    const restored = await searchGeometry(page)
    expect(restored.top).toBeCloseTo(initial.top, 1)
    expect(restored.height).toBeCloseTo(initial.height, 1)
    expect(restored.shellHeight).toBeCloseTo(initial.shellHeight, 1)
    await page.getByRole('dialog', { name: '搜索插件', exact: true }).getByRole('button', { name: '关闭', exact: true }).click()
    await expect(page.getByRole('dialog', { name: '搜索插件', exact: true })).toBeHidden()
  })

  test('iPhone 17 用户手动缩放不被误判为键盘且恢复后搜索窗口不漂移', async ({ page }) => {
    await installVisualViewportFixture(page)
    await page.goto('/#/plugins')
    await page.getByRole('button', { name: '搜索插件', exact: true }).click()
    await waitForSearchLayout(page)
    const initial = await searchGeometry(page)
    for (let cycle = 0; cycle < 2; cycle += 1) {
      await setVisualViewport(page, { width: 201, height: 437, offsetTop: 50, scale: 2 })
      await expect(page.locator('html')).not.toHaveClass(/keyboard-open/)
      await setVisualViewport(page, { width: 402, height: 874, offsetTop: 0, scale: 1 })
      await expect(page.locator('html')).not.toHaveClass(/keyboard-open/)
      const restored = await searchGeometry(page)
      expect(restored.top).toBeCloseTo(initial.top, 1)
      expect(restored.height).toBeCloseTo(initial.height, 1)
    }
  })

  test('iPhone 17 登录与首次设置的输入字号不低于 16px', async ({ page }) => {
    await page.route('**/api/auth/status', route => json(route, { needs_setup: true, must_change_password: false }))
    await page.goto('/#/status')
    await expect(page.locator('.lc-input')).toHaveCount(3)
    await assertVisibleInputFonts(page, '管理员登录与首次设置')
  })

  test('iPhone 17 横屏登录输入仍保持 16px 字号', async ({ page }) => {
    await page.setViewportSize({ width: 874, height: 402 })
    await page.route('**/api/auth/status', route => json(route, { needs_setup: true, must_change_password: false }))
    await page.goto('/#/status')
    await expect(page.locator('.lc-input')).toHaveCount(3)
    await assertVisibleInputFonts(page, '横屏管理员登录')
  })

  test('iPhone 17 横屏搜索和配置窗口仍跟随键盘可见区域', async ({ page }) => {
    await page.setViewportSize({ width: 874, height: 402 })
    await installVisualViewportFixture(page)
    await page.goto('/#/plugins')
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
    await page.getByRole('button', { name: '搜索插件', exact: true }).click()
    await waitForSearchLayout(page)
    const initialShell = await page.locator('#app > .layout').evaluate(element => element.getBoundingClientRect().height)
    await page.locator('.search-input-wrap input').focus()
    await setVisualViewport(page, { width: 874, height: 240, offsetTop: 24, scale: 1 })
    await expect(page.locator('html')).toHaveClass(/keyboard-open/)
    const search = await searchGeometry(page)
    const searchMask = await page.locator('.search-mask').evaluate(element => {
      const box = element.getBoundingClientRect()
      return { top: box.top, bottom: box.bottom, height: box.height }
    })
    console.info('Landscape keyboard search geometry:', JSON.stringify({ ...search, mask: searchMask }))
    expect.soft(searchMask.top).toBeCloseTo(24, 1)
    expect.soft(searchMask.height).toBeCloseTo(240, 1)
    expect.soft(search.top).toBeGreaterThanOrEqual(24)
    expect.soft(search.bottom).toBeLessThanOrEqual(264 + 1)
    for (const name of ['title', 'close', 'input', 'footer']) {
      expect.soft(search[name].top, `横屏搜索 ${name} 顶部`).toBeGreaterThanOrEqual(24)
      expect.soft(search[name].bottom, `横屏搜索 ${name} 底部`).toBeLessThanOrEqual(264 + 1)
    }
    expect.soft(search.list.height).toBeGreaterThan(0)
    expect.soft(search.shellHeight).toBeCloseTo(initialShell, 1)
    await page.getByRole('dialog', { name: '搜索插件', exact: true }).getByRole('button', { name: '关闭', exact: true }).click()
    await setVisualViewport(page, { width: 874, height: 402, offsetTop: 0, scale: 1 })
    await expect(page.locator('html')).not.toHaveClass(/keyboard-open/)

    await page.getByText('手机配置测试', { exact: true }).click()
    const modal = page.locator('.modal.modal-wide')
    const field = modal.locator('.secret-input input').first()
    await expect(field).toBeVisible()
    await field.focus()
    await setVisualViewport(page, { width: 874, height: 240, offsetTop: 24, scale: 1 })
    await expect(page.locator('html')).toHaveClass(/keyboard-open/)
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 250)))
    const config = await modal.evaluate(element => {
      const rect = node => {
        const box = node.getBoundingClientRect()
        return { top: box.top, bottom: box.bottom, height: box.height }
      }
      const input = element.querySelector('.secret-input input')
      const inputBox = input.getBoundingClientRect()
      const hit = document.elementFromPoint(inputBox.left + inputBox.width / 2,
        inputBox.top + inputBox.height / 2)
      return { modal: rect(element), mask: rect(element.closest('.config-modal-mask')),
        title: rect(element.querySelector('.modal-head h2')),
        close: rect(element.querySelector('.modal-head .close')),
        input: rect(input), footer: rect(element.querySelector('.modal-foot')),
        inputUnobscured: input === hit || input.contains(hit), hitClass: hit?.className,
        shellHeight: document.querySelector('#app > .layout').getBoundingClientRect().height }
    })
    console.info('Landscape keyboard config geometry:', JSON.stringify(config))
    expect.soft(config.mask.top).toBeCloseTo(24, 1)
    expect.soft(config.mask.height).toBeCloseTo(240, 1)
    expect.soft(config.modal.top).toBeGreaterThanOrEqual(24)
    expect.soft(config.modal.bottom).toBeLessThanOrEqual(264 + 1)
    for (const name of ['title', 'close', 'input']) {
      expect.soft(config[name].top, `横屏配置 ${name} 顶部`).toBeGreaterThanOrEqual(24)
      expect.soft(config[name].bottom, `横屏配置 ${name} 底部`).toBeLessThanOrEqual(264 + 1)
    }
    expect.soft(config.inputUnobscured, '横屏配置输入框不能被固定头部或底部挡住').toBe(true)
    expect.soft(config.shellHeight).toBeCloseTo(initialShell, 1)
    await assertVisibleInputFonts(page, '横屏插件配置')
    await setVisualViewport(page, { width: 874, height: 402, offsetTop: 0, scale: 1 })
    await expect(page.locator('html')).not.toHaveClass(/keyboard-open/)
  })

  test('iPhone 17 插件配置跟随非零键盘偏移而系统外壳高度不变', async ({ page }) => {
    await installVisualViewportFixture(page)
    await page.goto('/#/plugins')
    await injectSafeAreas(page)
    await page.getByText('手机配置测试', { exact: true }).click()
    const modal = page.locator('.modal.modal-wide')
    const field = modal.locator('.secret-input input').first()
    await expect(field).toBeVisible()
    const initialShell = await page.locator('#app > .layout').evaluate(element => element.getBoundingClientRect().height)
    await field.focus()
    for (const viewport of [{ height: 500, offsetTop: 70, scale: 1 }, { height: 470, offsetTop: 84, scale: 1 }]) {
      await setVisualViewport(page, viewport)
      await expect(page.locator('html')).toHaveClass(/keyboard-open/)
      await expect.poll(() => modal.evaluate((element, viewport) => {
        const box = element.getBoundingClientRect()
        const close = element.querySelector('.modal-head .close').getBoundingClientRect()
        const field = element.querySelector('.secret-input input').getBoundingClientRect()
        return { top: Math.round(box.top), height: Math.round(box.height),
          closeSafe: close.top >= viewport.offsetTop + 59,
          fieldVisible: field.top >= viewport.offsetTop && field.bottom <= viewport.offsetTop + viewport.height - 16,
          shellHeight: Math.round(document.querySelector('#app > .layout').getBoundingClientRect().height) }
      }, viewport)).toEqual({ top: viewport.offsetTop, height: viewport.height,
        closeSafe: true, fieldVisible: true, shellHeight: Math.round(initialShell) })
    }
    await setVisualViewport(page, { height: 874, offsetTop: 0, scale: 1 })
    await expect(page.locator('html')).not.toHaveClass(/keyboard-open/)
    await expect.poll(() => modal.evaluate(element => Math.round(element.getBoundingClientRect().top))).toBe(0)
  })

  test('iPhone 17 账号登录弹窗随键盘偏移且标题关闭和输入避开安全区', async ({ page }) => {
    await installVisualViewportFixture(page)
    await page.route('**/api/status', route => json(route, { ...status, telegram_configured: true }))
    await page.goto('/#/accounts')
    await injectSafeAreas(page)
    await page.getByRole('button', { name: '+ 登录新账号', exact: true }).click()
    await assertKeyboardDialog(page, page.getByPlaceholder('+8615012345678'), {
      dialog: '[role="dialog"][aria-label="登录账号"]', title: '.modal-head h2', close: '.modal-head .close',
    })
  })

  test('iPhone 17 通知渠道弹窗随键盘偏移且标题关闭和输入避开安全区', async ({ page }) => {
    await installVisualViewportFixture(page)
    await page.goto('/#/settings')
    await injectSafeAreas(page)
    await page.getByRole('button', { name: '通知渠道', exact: true }).click()
    await page.locator('.btn-add-mp').click()
    await page.getByRole('button', { name: 'Telegram', exact: true }).click()
    await assertKeyboardDialog(page, page.getByPlaceholder('如：通知1、订单通知'), {
      dialog: '.channel-modal', title: '.modal-header h3', close: '.modal-header .modal-close',
    })
  })

  test('iPhone 17 body 悬浮主题和日志窗口随键盘偏移且保持可见', async ({ page }) => {
    await installVisualViewportFixture(page)
    await page.goto('/#/status')
    await injectSafeAreas(page)
    await page.getByTitle('管理员菜单').click()
    await page.locator('.theme-trigger').click()
    await page.getByRole('button', { name: /定制主题/ }).click()
    await assertKeyboardDialog(page, page.getByPlaceholder('留空使用默认背景'), {
      dialog: 'body > .control-modal-mask .control-modal', title: 'header strong', close: 'header button',
    })
    await page.getByRole('dialog', { name: '定制主题', exact: true }).getByRole('button', { name: '关闭', exact: true }).click()
    await setVisualViewport(page, { height: 874, offsetTop: 0, scale: 1 })
    await page.getByTitle('快捷入口').click()
    await page.getByRole('button', { name: /运行日志/ }).click()
    await assertKeyboardDialog(page, page.getByPlaceholder('搜索日志内容'), {
      dialog: 'body > .control-modal-mask .control-modal', title: 'header strong', close: 'header button',
    })
  })

  test('iPhone 17 系统设置 AI 服务账号和日志的输入字号不低于 16px', async ({ page }) => {
    await page.route('**/api/status', route => json(route, { ...status, telegram_configured: true }))
    await page.route('**/api/bots/routing', route => json(route, { bots: [], plugins: [configurablePlugin] }))
    await page.route('**/api/accounts/login/send_code', route => json(route, { ok: true }))
    await page.route('**/api/accounts/login/submit_code', route => json(route, { need: 'password' }))
    await page.goto('/#/settings')
    await expect(page.locator('.panel')).toBeVisible()
    await assertVisibleInputFonts(page, '系统设置账号与凭据')
    await page.getByRole('button', { name: '通知渠道', exact: true }).click()
    await expect(page.getByPlaceholder('搜索插件名称 / id…')).toBeVisible()
    await assertVisibleInputFonts(page, '系统设置插件搜索')
    await page.getByRole('button', { name: 'AI 服务', exact: true }).click()
    await expect(page.getByLabel('搜索模型库', { exact: true })).toBeVisible()
    await assertVisibleInputFonts(page, 'AI 服务与模型搜索')
    await page.getByRole('button', { name: '运行环境', exact: true }).click()
    await assertVisibleInputFonts(page, '系统设置运行环境')
    await page.goto('/#/accounts')
    await page.getByRole('button', { name: '+ 登录新账号', exact: true }).click()
    await assertVisibleInputFonts(page, '账号手机号表单')
    await page.getByPlaceholder('例如 user_account').fill('test_account')
    await page.getByPlaceholder('+8615012345678').fill('+8615012345678')
    await page.getByRole('button', { name: '发送验证码', exact: true }).click()
    await expect(page.getByPlaceholder('123456')).toBeVisible()
    await assertVisibleInputFonts(page, '账号验证码表单')
    await page.getByPlaceholder('123456').fill('123456')
    await page.getByRole('button', { name: '确认', exact: true }).click()
    await expect(page.locator('.modal input[type="password"]')).toBeVisible()
    await assertVisibleInputFonts(page, '账号两步密码表单')
    await page.goto('/#/logs')
    await expect(page.getByPlaceholder('搜索插件名/内容…')).toBeVisible()
    await assertVisibleInputFonts(page, '日志搜索')
  })
})

test.describe('桌面输入样式回归', () => {
  test.use({ viewport: { width: 1280, height: 900 }, isMobile: false, hasTouch: false,
    deviceScaleFactor: 1 })
  test('桌面插件搜索保留原字号与居中窗口并允许浏览器缩放', async ({ page }) => {
    await page.goto('/#/plugins')
    await page.getByRole('button', { name: '搜索插件', exact: true }).click()
    await waitForSearchLayout(page)
    const input = page.locator('.search-input-wrap input')
    await expect(input).toBeVisible()
    expect(await input.evaluate(field => Number.parseFloat(getComputedStyle(field).fontSize))).toBe(14)
    const geometry = await searchGeometry(page)
    expect(geometry.top).toBeGreaterThan(0)
    expect(geometry.height).toBeLessThan(900)
    expect(geometry.close.width).toBe(32)
    await expectInsideViewport(page)
    if (process.env.AWBOTNEST_MOBILE_PROOF_DIR) {
      await page.screenshot({ path: join(process.env.AWBOTNEST_MOBILE_PROOF_DIR, 'desktop-search.png') })
    }
  })
})

test('iPhone 17 外壳保留悬浮导航且内容不被遮挡', async ({ page }) => {
  await page.goto('/#/status')
  await expect(page.locator('meta[name="mobile-web-app-capable"]')).toHaveAttribute('content', 'yes')
  const dock = page.locator('[data-mobile-navigation-dock]')
  await expect(dock).toBeVisible()
  await expect(page.locator('[data-app-topbar]')).toBeVisible()
  const spacing = await page.evaluate(() => {
    const dock = document.querySelector('[data-mobile-navigation-dock]')
    const content = document.querySelector('[data-app-content]')
    const dockBox = dock.getBoundingClientRect()
    return {
      dockHeight: dockBox.height,
      dockBottomGap: innerHeight - dockBox.bottom,
      contentBottomPadding: parseFloat(getComputedStyle(content).paddingBottom),
      backdrop: getComputedStyle(dock).backdropFilter,
    }
  })
  expect(spacing.dockBottomGap).toBeGreaterThanOrEqual(11)
  expect(spacing.contentBottomPadding).toBeGreaterThan(spacing.dockHeight)
  expect(spacing.backdrop).toContain('blur')
  await expectInsideViewport(page)
})

test('无图标插件的 Logo 占位底色与边界可分辨', async ({ page }) => {
  await page.goto('/#/plugins')
  const icon = page.locator('.plugin-card .store-icon-fallback').first()
  await expect(icon).toBeVisible()
  const style = await icon.evaluate((element) => {
    const computed = getComputedStyle(element)
    return {
      borderStyle: computed.borderStyle,
      borderColor: computed.borderColor,
      backgroundColor: computed.backgroundColor,
      padding: computed.padding,
    }
  })
  expect(style.borderStyle).toBe('solid')
  expect(style.borderColor).not.toBe('rgba(0, 0, 0, 0)')
  expect(style.backgroundColor).not.toBe('rgba(0, 0, 0, 0)')
  expect(style.padding).toBe('2px')
  await expectInsideViewport(page)
})

test('iPhone 17 根画布与页面背景连续铺满', async ({ page }) => {
  await page.goto('/#/status')
  await expect(page.locator('.layout')).toBeVisible()
  const coverage = await page.evaluate(() => {
    document.documentElement.dataset.theme = 'transparent'
    document.documentElement.style.setProperty(
      '--app-bg-image',
      'linear-gradient(rgb(17, 34, 51), rgb(17, 34, 51))',
    )
    const rootStyle = getComputedStyle(document.documentElement)
    const bodyStyle = getComputedStyle(document.body)
    const layoutBox = document.querySelector('.layout').getBoundingClientRect()
    return {
      rootBackground: rootStyle.backgroundImage,
      bodyBackground: bodyStyle.backgroundImage,
      layoutBottom: layoutBox.bottom,
      viewportBottom: window.visualViewport?.height || window.innerHeight,
    }
  })
  expect(coverage.rootBackground).toBe(coverage.bodyBackground)
  expect(coverage.rootBackground).toContain('rgb(17, 34, 51)')
  expect(coverage.layoutBottom).toBeGreaterThanOrEqual(coverage.viewportBottom - 1)
})

test('iPhone 17 PWA 独立窗口使用完整屏幕高度', async ({ page, context }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, 'standalone', {
      configurable: true,
      value: true,
    })
  })
  const cdp = await context.newCDPSession(page)
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 402,
    height: 797,
    deviceScaleFactor: 3,
    mobile: true,
    screenWidth: 402,
    screenHeight: 874,
    positionX: 0,
    positionY: 0,
  })
  await page.goto('/#/status')
  await expect(page.locator('.layout')).toBeVisible()
  const coverage = await page.evaluate(() => {
    const layoutBox = document.querySelector('.layout').getBoundingClientRect()
    const dock = document.querySelector('[data-mobile-navigation-dock]')
    return {
      standalone: document.documentElement.classList.contains('ios-pwa'),
      viewportHeight: window.innerHeight,
      screenHeight: window.screen.height,
      shellHeight: layoutBox.height,
      shellBottom: layoutBox.bottom,
      dockPosition: getComputedStyle(dock).position,
      dockBottom: dock.getBoundingClientRect().bottom,
    }
  })
  expect(coverage.standalone).toBe(true)
  expect(coverage.screenHeight).toBeGreaterThan(coverage.viewportHeight)
  expect(coverage.shellHeight).toBeGreaterThanOrEqual(coverage.screenHeight - 1)
  expect(coverage.shellBottom).toBeGreaterThanOrEqual(coverage.screenHeight - 1)
  expect(coverage.dockPosition).toBe('absolute')
  expect(coverage.dockBottom).toBeGreaterThan(coverage.viewportHeight)
})

test('iPhone 17 完整显示 AI 服务、协议和调用明细', async ({ page }) => {
  await page.goto('/#/settings')
  await page.getByRole('button', { name: 'AI 服务', exact: true }).click()
  await expect(page.locator('.ai-mark svg')).toBeVisible()
  await expect(page.getByText('最近调用', { exact: true })).toBeVisible()
  await expect(page.getByText(/当前识别为 Responses/)).toBeVisible()
  await expect(page.getByText('多站签到', { exact: true })).toBeVisible()
  await expect(page.getByLabel('筛选调用状态')).toBeVisible()
  const horizontal = await page.evaluate(() => {
    const content = document.querySelector('[data-app-content]')
    const settings = document.querySelector('.settings-page')
    const overview = document.querySelector('.ai-overview')
    const contentBox = content.getBoundingClientRect()
    return {
      contentClientWidth: content.clientWidth,
      contentScrollWidth: content.scrollWidth,
      contentOverflowX: getComputedStyle(content).overflowX,
      settingsClientWidth: settings.clientWidth,
      settingsScrollWidth: settings.scrollWidth,
      outerGap: overview.getBoundingClientRect().left - contentBox.left,
    }
  })
  expect(horizontal.contentOverflowX).toBe('hidden')
  expect(horizontal.contentScrollWidth).toBeLessThanOrEqual(horizontal.contentClientWidth)
  expect(horizontal.settingsScrollWidth).toBeLessThanOrEqual(horizontal.settingsClientWidth)
  expect(horizontal.outerGap).toBeLessThanOrEqual(10)
  await expectInsideViewport(page)
})

test('iPhone 17 可选择浏览器仿真并配置 CloakBrowser Key', async ({ page }) => {
  await page.goto('/#/settings')
  await page.getByRole('button', { name: '运行环境', exact: true }).click()
  await expect(page.getByRole('radio', { name: /Chromium/ })).toBeChecked()
  await expect(page.getByText('CloakBrowser', { exact: true })).toBeVisible()
  await page.getByRole('radio', { name: /CloakBrowser/ }).check()
  await expect(page.getByText(/免费 Key 已关闭，使用旧版免费内核/)).toBeVisible()
  await page.getByRole('button', { name: '使用免费 Key 获取最新版' }).click()
  await expect(page.getByPlaceholder('cb_你的完整 Key')).toBeVisible()
  await expect(page.getByLabel('CloakBrowser 版本信息')).toContainText('组件0.5.10')
  await expect(page.getByLabel('CloakBrowser 版本信息')).toContainText('免费内核146.0.7680.177.5')
  await expect(page.getByText(/尚未填写 Key，将使用旧版免费内核/)).toBeVisible()
  await expectInsideViewport(page)
  const overflow = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    page: document.documentElement.scrollWidth,
  }))
  expect(overflow.page).toBeLessThanOrEqual(overflow.viewport)
})

test('CloakBrowser 分开显示组件和内核版本，最新时按钮明确标识', async ({ page }) => {
  await page.route('**/api/settings', (route) => json(route, { settings: {
    ...settings,
    BROWSER_ENGINE: 'cloakbrowser',
    CLOAKBROWSER_USE_FREE_KEY: true,
    CLOAKBROWSER_LICENSE_KEY: '********',
  } }))
  await page.route('**/api/browser/status', (route) => json(route, {
    engine: 'cloakbrowser', cloakbrowser_installed: true, cloakbrowser_version: '0.5.10',
    cloakbrowser_kernels: [{ channel: 'stable', version: '152.0.7977.82.1' }],
    key_configured: true, key_enabled: true, key_active: true, binary_mode: 'latest',
    update_check: {
      status: 'current', update_available: false, required_kernel_channels: ['stable'],
      kernel_channels: [{ channel: 'stable', latest_version: '152.0.7977.82.1', update_available: false }],
    },
    queue_enabled: true, active: false, waiting: 0, maintenance: false, cooldown_seconds: 0,
  }))

  await page.goto('/#/settings')
  await page.getByRole('button', { name: '运行环境', exact: true }).click()
  const versions = page.getByLabel('CloakBrowser 版本信息')
  await expect(versions).toContainText('组件0.5.10')
  await expect(versions).toContainText('Stable 内核152.0.7977.82.1')
  await expect(page.getByRole('button', { name: '已是最新', exact: true })).toBeDisabled()
  await expectInsideViewport(page)
})

test('AI 主配置不等待后台统计和插件扫描', async ({ page }) => {
  let statusRequests = 0
  let releaseBackground
  const backgroundGate = new Promise((resolve) => { releaseBackground = resolve })
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/ai/status') statusRequests += 1
  })
  await page.route('**/api/ai/plugins', async (route) => {
    await backgroundGate
    return json(route, { plugins: [] })
  })
  await page.route('**/api/ai/usage/overview*', async (route) => {
    await backgroundGate
    return json(route, { items: [], plugins: [], total_items: 0 })
  })

  await page.goto('/#/settings')
  await page.getByRole('button', { name: 'AI 服务', exact: true }).click()

  await expect(page.getByPlaceholder('例如：主 AI 服务')).toHaveValue('主 AI 服务')
  await expect(page.locator('.ai-call-skeleton')).toBeVisible()
  expect(statusRequests).toBe(0)
  releaseBackground()
  await expect(page.locator('.ai-call-skeleton')).toBeHidden({ timeout: 1500 })
})

test('iPhone 17 主题选项悬浮在主题菜单正下方', async ({ page }) => {
  await page.goto('/#/settings')
  await page.getByTitle('管理员菜单').click()
  await page.locator('.theme-trigger').click()
  const submenu = page.locator('.theme-submenu')
  await expect(submenu).toBeVisible()
  const layout = await page.evaluate(() => {
    const submenu = document.querySelector('.theme-submenu')
    const trigger = document.querySelector('.theme-trigger')
    const userMenu = document.querySelector('.user-pop')
    const submenuBox = submenu.getBoundingClientRect()
    const triggerBox = trigger.getBoundingClientRect()
    const menuBox = userMenu.getBoundingClientRect()
    const centerElement = document.elementFromPoint(
      submenuBox.left + submenuBox.width / 2,
      submenuBox.top + submenuBox.height / 2,
    )
    return {
      position: getComputedStyle(submenu).position,
      overflowY: getComputedStyle(userMenu).overflowY,
      triggerBottom: triggerBox.bottom,
      submenuTop: submenuBox.top,
      submenuLeft: submenuBox.left,
      submenuRight: submenuBox.right,
      submenuVisibleOnTop: submenu.contains(centerElement),
      menuTop: menuBox.top,
      menuBottom: menuBox.bottom,
      viewportWidth: innerWidth,
      viewportHeight: innerHeight,
    }
  })
  expect(layout.position).toBe('absolute')
  expect(layout.overflowY).toBe('auto')
  expect(layout.submenuTop).toBeGreaterThanOrEqual(layout.triggerBottom + 5)
  expect(layout.submenuTop).toBeLessThanOrEqual(layout.triggerBottom + 7)
  expect(layout.submenuLeft).toBeGreaterThanOrEqual(0)
  expect(layout.submenuRight).toBeLessThanOrEqual(layout.viewportWidth)
  expect(layout.submenuVisibleOnTop).toBe(true)
  expect(layout.menuTop).toBeGreaterThanOrEqual(0)
  expect(layout.menuBottom).toBeLessThanOrEqual(layout.viewportHeight)
})

test('iPhone 17 插件配置关闭按钮避开顶部安全区且易于点击', async ({ page }) => {
  await page.goto('/#/plugins')
  await page.getByText('手机配置测试', { exact: true }).click()
  const modal = page.locator('.modal.modal-wide')
  const close = modal.getByRole('button', { name: '关闭' })
  await expect(modal).toBeVisible()
  await expect(close).toBeVisible()
  const geometry = await page.evaluate(() => {
    const modal = document.querySelector('.modal.modal-wide')
    const close = modal.querySelector('.modal-head .close')
    const modalBox = modal.getBoundingClientRect()
    const closeBox = close.getBoundingClientRect()
    return {
      width: closeBox.width,
      height: closeBox.height,
      topGap: closeBox.top - modalBox.top,
      closeTop: closeBox.top,
      closeRight: closeBox.right,
      viewportWidth: innerWidth,
    }
  })
  expect(geometry.width).toBeGreaterThanOrEqual(44)
  expect(geometry.height).toBeGreaterThanOrEqual(44)
  expect(geometry.topGap).toBeGreaterThanOrEqual(8)
  expect(geometry.closeTop).toBeGreaterThanOrEqual(0)
  expect(geometry.closeRight).toBeLessThanOrEqual(geometry.viewportWidth)
  await close.click()
  await expect(modal).toBeHidden()
})

test('插件操作失败使用系统内置提示弹窗', async ({ page }) => {
  await page.route('**/api/plugins/mobile_config_test/disable', (route) => route.fulfill({
    status: 500,
    contentType: 'application/json',
    body: JSON.stringify({ detail: "ImportError: cannot import name 'auto_avatar'" }),
  }))
  await page.goto('/#/plugins')

  await page.locator('.plugin-card .toggle').first().click()
  const dialog = page.getByRole('alertdialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('插件操作失败', { exact: true })).toBeVisible()
  await expect(dialog.getByText(/ImportError: cannot import name 'auto_avatar'/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '取消' })).toHaveCount(0)
  await expect(page.locator('.error-dialog')).toHaveCount(0)

  await dialog.getByRole('button', { name: '知道了' }).click()
  await expect(dialog).toBeHidden()
})

test('Schema Cron 接口显示参考图式规则编辑器', async ({ page }) => {
  await page.goto('/#/plugins')
  await page.getByText('手机配置测试', { exact: true }).click()

  const modal = page.locator('.modal.modal-wide')
  const expression = modal.getByRole('textbox', { name: 'Cron 表达式', exact: true })
  await expect(expression).toHaveValue('6 3 * * *')
  await expression.click()

  const sentence = modal.locator('.cron-sentence')
  await expect(sentence).toBeVisible()
  await expect(modal.getByRole('combobox', { name: '月份', exact: true })).toHaveValue('*')
  await expect(modal.getByRole('combobox', { name: '日期', exact: true })).toHaveValue('*')
  await expect(modal.getByRole('combobox', { name: '星期', exact: true })).toHaveValue('*')
  await expect(modal.getByRole('combobox', { name: '小时', exact: true })).toHaveValue('3')
  await expect(modal.getByRole('combobox', { name: '分钟', exact: true })).toHaveValue('6')

  await modal.getByRole('combobox', { name: '小时', exact: true }).selectOption('8')
  await modal.getByRole('combobox', { name: '分钟', exact: true }).selectOption('15')
  await expect(expression).toHaveValue('15 8 * * *')
})

test('iPhone 17 输入法弹出后配置输入框仍位于可见区域', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, 'standalone', { configurable: true, value: true })
  })
  await page.goto('/#/plugins')
  await page.getByText('手机配置测试', { exact: true }).click()

  const modal = page.locator('.modal.modal-wide')
  const secret = modal.locator('.secret-input input').first()
  await expect(secret).toBeVisible()
  await secret.evaluate((input) => {
    input.closest('.field').style.marginTop = '620px'
    input.focus({ preventScroll: true })
  })
  await page.setViewportSize({ width: 402, height: 500 })
  await expect(page.locator('html')).toHaveClass(/keyboard-open/)
  await page.waitForTimeout(350)

  const geometry = await page.evaluate(() => {
    const field = document.activeElement
    const modal = document.querySelector('.modal.modal-wide')
    const appLayout = document.querySelector('#app > .layout')
    const fieldBox = field.getBoundingClientRect()
    const modalBox = modal.getBoundingClientRect()
    return {
      fieldBottom: fieldBox.bottom,
      modalTop: modalBox.top,
      modalHeight: modalBox.height,
      layoutHeight: appLayout?.getBoundingClientRect().height || 0,
      visibleHeight: window.visualViewport?.height || window.innerHeight,
    }
  })
  expect(geometry.fieldBottom).toBeLessThanOrEqual(geometry.visibleHeight - 16)
  expect(geometry.modalTop).toBeGreaterThanOrEqual(0)
  expect(geometry.modalHeight).toBeLessThanOrEqual(geometry.visibleHeight + 1)
  expect(geometry.layoutHeight).toBeGreaterThan(geometry.visibleHeight)
})

test('插件敏感配置只在点击显示后读取真实值', async ({ page }) => {
  await page.goto('/#/plugins')
  await page.locator('.plugin-card').first().click()
  const secret = page.locator('.modal.modal-wide .secret-input input').first()
  await expect(secret).toBeVisible()
  await expect(secret).toHaveAttribute('type', 'password')
  await expect(secret).toHaveValue('********')
  await page.getByRole('button', { name: '显示内容' }).click()
  await expect(secret).toHaveAttribute('type', 'text')
  await expect(secret).toHaveValue('mobile-real-secret')
})

test('Schema 下拉选项保存并重新打开后保留数字和布尔类型', async ({ page }) => {
  let values = { count: 1, mode: true }
  await page.route('**/api/plugins/mobile_config_test/config', (route) => {
    if (route.request().method() === 'PUT') {
      values = route.request().postDataJSON().values
      return json(route, { ok: true, values })
    }
    return json(route, {
      schema: {
        count: { type: 'select', label: '数字选项', options: [1, 2] },
        mode: { type: 'select', label: '布尔选项', options: [{ value: true, label: '开启' }, { value: false, label: '关闭' }] },
      }, values, render_mode: 'schema', has_frontend: false,
    })
  })
  await page.goto('/#/plugins')
  await page.getByText('手机配置测试', { exact: true }).click()
  const modal = page.locator('.modal.modal-wide')
  await modal.locator('select').nth(0).selectOption('2')
  await modal.locator('select').nth(1).selectOption('false')
  await modal.getByRole('button', { name: '保存并应用' }).click()
  await expect(modal).toBeHidden()
  expect(values).toEqual({ count: 2, mode: false })
  await page.getByText('手机配置测试', { exact: true }).click()
  await expect(modal.locator('select').nth(0)).toHaveValue('2')
  await expect(modal.locator('select').nth(1)).toHaveValue('false')
})

test('嵌套敏感值按需读取且删除行后不会串用其他账号的值', async ({ page }) => {
  const reads = []
  let saved = null
  await page.route('**/api/plugins/mobile_config_test/config', (route) => {
    if (route.request().method() === 'PUT') {
      saved = route.request().postDataJSON().values
      return json(route, { ok: true })
    }
    return json(route, {
      schema: {
        accounts: { type: 'list', label: '签到账号', fields: {
          name: { type: 'string', label: '名称' }, cookie: { type: 'password', label: 'Cookie' },
        } },
        private_accounts: { type: 'list', secret: true, label: '其他账号', fields: {
          cookie: { type: 'password', label: 'Cookie' },
        } },
      },
      values: { accounts: [{ name: 'one', cookie: '********' }, { name: 'two', cookie: '********' }], private_accounts: '********' },
      render_mode: 'schema', has_frontend: false,
    })
  })
  await page.route('**/api/plugins/mobile_config_test/config/reveal', (route) => {
    const field = route.request().postDataJSON().field
    reads.push(field)
    const value = field === '/private_accounts' ? [{ cookie: 'private-cookie' }]
      : field === '/accounts/0/cookie' ? 'first-cookie' : 'second-cookie'
    return json(route, { field, value })
  })
  await page.goto('/#/plugins')
  await page.getByText('手机配置测试', { exact: true }).click()
  const modal = page.locator('.modal.modal-wide')
  await expect(modal.locator('.secret-input input').first()).toHaveValue('********')
  expect(reads).toEqual([])
  await modal.getByRole('button', { name: '显示内容' }).first().click()
  await expect(modal.locator('.secret-input input').first()).toHaveValue('first-cookie')
  await modal.locator('.row-del').first().click()
  await expect(modal.locator('.row-card')).toHaveCount(1)
  await expect(modal.locator('.secret-input input').first()).toHaveValue('second-cookie')
  await modal.getByRole('button', { name: '读取已保存内容' }).click()
  await expect(modal.locator('.row-card')).toHaveCount(2)
  await modal.getByRole('button', { name: '保存并应用' }).click()
  await expect(modal).toBeHidden()
  expect(reads).toEqual(['/accounts/0/cookie', '/accounts/1/cookie', '/private_accounts'])
  expect(saved.accounts).toEqual([{ name: 'two', cookie: 'second-cookie' }])
  expect(saved.private_accounts).toEqual([{ cookie: 'private-cookie' }])
})

test('旧安装无需确认来源，同版本不重装，手动更新后不再显示更新入口', async ({ page }) => {
  let installations = 0
  await page.route('**/api/plugins/store/install', (route) => {
    installations += 1
    return json(route, { ok: true, plugin: { loaded: false } })
  })
  await page.route('**/api/plugins/store*', (route) => {
    return json(route, { plugins: [{ id: 'source_demo', name: '同版本旧安装测试', installed: true,
      from_manifest: true, version: '1.0.0', local_version: '1.0.0', source_confirmed: false,
      update_available: false,
      repo: 'Example/AWBotNest-Plugins', path: 'plugins_v2/source_demo/', install_count: 1,
    }, { id: 'update_demo', name: '旧安装更新测试', installed: true,
      from_manifest: true, version: '2.0.0', local_version: '1.0.0', source_confirmed: false,
      update_available: true,
      repo: 'Example/AWBotNest-Plugins', path: 'plugins_v2/update_demo/', install_count: 1,
    }] })
  })
  await page.goto('/#/plugins')
  await page.getByRole('button', { name: /插件市场/ }).click()
  await expect(page.getByText('旧安装更新测试', { exact: true })).toBeVisible()
  await expect(page.getByText('同版本旧安装测试', { exact: true })).toHaveCount(0)
  await expect(page.getByText('先确认更新来源', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认来源', exact: true })).toHaveCount(0)
  expect(installations).toBe(0)
  await page.getByRole('button', { name: '更新', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '确认更新来源' })
  await expect(dialog).toHaveCount(0)
  await expect.poll(() => installations).toBe(1)
  await expect(page.getByText('旧安装更新测试', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '更新', exact: true })).toHaveCount(0)
})

test('企业微信回调默认关闭，按需读取密钥且关闭后保留配置', async ({ page }) => {
  const channelSettings = JSON.parse(JSON.stringify(settings))
  const saves = [], reads = [], routes = []
  const routingPlugins = [
    { id: 'checkin', name: '签到插件', scope: 'standalone', bot: '' },
    { id: 'other', name: '其他插件', scope: 'standalone', bot: '' },
    { id: 'bot_only', name: '仅 Telegram 事件插件', scope: 'bot', bot: '' },
  ]
  await page.route('**/api/settings', route => {
    const masked = JSON.parse(JSON.stringify(channelSettings))
    for (const channel of masked.NOTIFICATION_CHANNELS) {
      for (const field of ['secret', 'callback_token', 'callback_aes_key']) {
        if (channel.config[field]) channel.config[field] = '********'
      }
    }
    return json(route, { settings: masked })
  })
  await page.route('**/api/settings/notification-channels', route => {
    const channels = route.request().postDataJSON().channels
    saves.push(JSON.parse(JSON.stringify(channels)))
    for (const channel of channels) {
      const previous = channelSettings.NOTIFICATION_CHANNELS.find(item => item.id === channel.id)
      for (const field of ['secret', 'callback_token', 'callback_aes_key']) {
        if (channel.config[field] === '********') channel.config[field] = previous?.config[field] || ''
      }
    }
    channelSettings.NOTIFICATION_CHANNELS = channels
    return json(route, { ok: true, restart_required: false })
  })
  await page.route('**/api/settings/reveal-secret', route => {
    const body = route.request().postDataJSON()
    reads.push(body)
    const channel = channelSettings.NOTIFICATION_CHANNELS.find(item => item.id === body.id)
    return json(route, { value: channel.config[body.field] })
  })
  await page.route('**/api/bots/routing', route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      routes.push(body)
      routingPlugins.find(plugin => plugin.id === body.plugin_id).bot = body.bot_id
      return json(route, { ok: true })
    }
    return json(route, { bots: [], plugins: routingPlugins })
  })
  await page.goto('/#/settings')
  await page.getByRole('button', { name: '通知渠道', exact: true }).click()
  await page.locator('.btn-add-mp').click()
  await page.getByRole('button', { name: '企业微信', exact: true }).click()
  const modal = page.locator('.channel-modal')
  await modal.getByPlaceholder('如：通知1、订单通知').fill('企业指令测试')
  await expect(modal.getByLabel('消息回调', { exact: true })).not.toBeChecked()
  await expect(modal.getByLabel('回调 Token', { exact: true })).toHaveCount(0)
  await modal.getByRole('button', { name: '确认', exact: true }).click()
  await expect(modal).toBeHidden()
  expect(saves).toHaveLength(1)
  expect(saves[0][0].config.callback_enabled).toBe(false)
  expect(saves[0][0].config.callback_token).toBe('')

  await page.locator('.channel-card-mp .channel-bottom-row').click()
  await modal.getByLabel('消息回调', { exact: true }).check()
  await modal.getByPlaceholder('企业微信后台企业信息中的企业ID').fill('corp-test')
  await modal.getByPlaceholder('企业微信自建应用的AgentId').fill('1')
  await modal.getByPlaceholder('企业微信自建应用的Secret').fill('app-secret')
  await modal.getByLabel('回调 Token', { exact: true }).fill('callback-token')
  await modal.getByLabel('EncodingAESKey', { exact: true }).fill('A'.repeat(43))
  await expect(modal.getByLabel('回调 URL', { exact: true })).toHaveAttribute('readonly', '')
  await expect(modal.getByLabel('回调 URL', { exact: true })).toHaveValue(
    `${new URL(page.url()).origin}/api/wecom/callback/${encodeURIComponent(channelSettings.NOTIFICATION_CHANNELS[0].id)}`,
  )
  await expect(modal.getByText(/先保存渠道，再到自建应用/)).toBeVisible()
  await expect(modal.getByText(/支持 \/帮助、\/状态、\/插件、\/运行 插件ID 动作名/)).toBeVisible()
  await expect(modal.getByLabel('签到插件', { exact: true })).not.toBeChecked()
  await expect(modal.getByLabel('其他插件', { exact: true })).not.toBeChecked()
  await expect(modal.getByLabel('仅 Telegram 事件插件', { exact: true })).not.toBeChecked()
  await modal.getByRole('button', { name: '确认', exact: true }).click()
  await expect(page.getByText('请填写允许操作的成员', { exact: true })).toBeVisible()
  expect(saves).toHaveLength(1)
  await modal.getByLabel('允许操作的成员', { exact: true }).fill('alice|bob')
  await modal.getByLabel('签到插件', { exact: true }).check()
  await modal.getByRole('button', { name: '确认', exact: true }).click()
  await expect(modal).toBeHidden()
  await expect.poll(() => routes.length).toBe(1)
  expect(routes[0]).toEqual({ plugin_id: 'checkin', bot_id: channelSettings.NOTIFICATION_CHANNELS[0].id })
  expect(saves[1][0].config.callback_enabled).toBe(true)
  expect(saves[1][0].config.callback_users).toBe('alice|bob')
  expect(reads).toEqual([])

  await page.locator('.channel-card-mp .channel-bottom-row').click()
  await expect(modal.getByLabel('回调 Token', { exact: true })).toHaveValue('********')
  await expect(modal.getByLabel('EncodingAESKey', { exact: true })).toHaveValue('********')
  expect(reads).toEqual([])
  await modal.locator('.callback-secret-label').filter({ hasText: '回调 Token' })
    .getByRole('button', { name: '显示内容', exact: true }).click()
  await expect(modal.getByLabel('回调 Token', { exact: true })).toHaveValue('callback-token')
  expect(reads).toEqual([{ kind: 'channel', field: 'callback_token', id: channelSettings.NOTIFICATION_CHANNELS[0].id }])
  await modal.getByLabel('消息回调', { exact: true }).uncheck()
  await expect(modal.getByLabel('回调 Token', { exact: true })).toHaveCount(0)
  await modal.getByRole('button', { name: '确认', exact: true }).click()
  await expect(modal).toBeHidden()
  expect(saves[2][0].config.callback_enabled).toBe(false)
  expect(saves[2][0].config.callback_token).toBe('callback-token')
  expect(saves[2][0].config.callback_aes_key).toBe('********')
  expect(saves[2][0].config.callback_users).toBe('alice|bob')
  await page.locator('.channel-card-mp .channel-bottom-row').click()
  await expect(modal.getByLabel('消息回调', { exact: true })).not.toBeChecked()
  await modal.getByLabel('消息回调', { exact: true }).check()
  await expect(modal.getByLabel('回调 Token', { exact: true })).toHaveValue('********')
  await expect(modal.getByLabel('EncodingAESKey', { exact: true })).toHaveValue('********')
  await expect(modal.getByLabel('允许操作的成员', { exact: true })).toHaveValue('alice|bob')
  await expect(page.locator('.toast-enter-active, .toast-leave-active')).toHaveCount(0)
  await expectInsideViewport(page)
})

test('Bot 与双账号插件可双向选择所有通知渠道且保存不丢关联', async ({ page }) => {
  const channels = [
    { id: 'tg', name: 'Telegram 通知', type: 'telegram', enabled: true, is_default: true, config: {} },
    { id: 'wc', name: '企业通知', type: 'wechat', enabled: true, is_default: false, config: { callback_enabled: false } },
    { id: 'bark', name: '手机推送', type: 'bark', enabled: true, is_default: false, config: {} },
  ]
  const routingPlugins = [
    { ...configurablePlugin, id: 'bot_notify', name: 'Bot 通知测试', scope: 'bot', bot: 'tg,wc' },
    { ...configurablePlugin, id: 'both_notify', name: '双账号通知测试', scope: 'both', bot: 'tg,wc' },
  ]
  const routeSaves = []
  const currentSettings = { ...settings, NOTIFICATION_CHANNELS: channels }
  await page.route('**/api/settings', route => json(route, { settings: currentSettings }))
  await page.route('**/api/settings/notification-channels', route => {
    currentSettings.NOTIFICATION_CHANNELS = route.request().postDataJSON().channels
    return json(route, { ok: true, restart_required: false })
  })
  await page.route('**/api/bots/routing', route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      routeSaves.push(body)
      routingPlugins.find(plugin => plugin.id === body.plugin_id).bot = body.bot_id
      return json(route, { ok: true, bot: body.bot_id })
    }
    return json(route, {
      bots: currentSettings.NOTIFICATION_CHANNELS.map(channel => ({ ...channel, online: true })),
      plugins: routingPlugins,
    })
  })
  await page.route('**/api/plugins', route => json(route, { plugins: routingPlugins }))
  await page.route('**/api/plugins/*/config', route => json(route, {
    schema: { enabled: { type: 'boolean', label: '启用测试功能' } },
    values: { enabled: true }, render_mode: 'schema', has_frontend: false,
  }))

  await page.goto('/#/settings')
  await page.getByRole('button', { name: '通知渠道', exact: true }).click()
  for (const plugin of routingPlugins) {
    const row = page.locator('.route-row-multi').filter({ hasText: plugin.name })
    await expect(row.getByLabel(/Telegram 通知/)).toBeChecked()
    await expect(row.getByLabel(/企业通知/)).toBeChecked()
    await row.getByLabel(/手机推送/).check()
    await expect.poll(() => plugin.bot).toBe('tg,wc,bark')
    await expect(row.getByLabel(/手机推送/)).toBeEnabled()
  }

  await page.locator('.channel-card-mp').filter({ hasText: '企业通知' }).locator('.channel-bottom-row').click()
  const channelModal = page.locator('.channel-modal')
  await expect(channelModal.getByLabel('Bot 通知测试', { exact: true })).toBeChecked()
  await expect(channelModal.getByLabel('双账号通知测试', { exact: true })).toBeChecked()
  await channelModal.getByLabel('双账号通知测试', { exact: true }).uncheck()
  await channelModal.getByRole('button', { name: '确认', exact: true }).click()
  await expect(channelModal).toBeHidden()
  await expect.poll(() => routingPlugins[1].bot).toBe('tg,bark')
  expect(routingPlugins[0].bot).toBe('tg,wc,bark')
  await expect(page.locator('.route-row-multi')).toHaveCount(2)
  await expect(page.locator('.toast-enter-active, .toast-leave-active')).toHaveCount(0)
  await expectInsideViewport(page)

  await page.goto('/#/plugins')
  for (const plugin of routingPlugins) {
    await page.getByText(plugin.name, { exact: true }).click()
    const configModal = page.locator('.modal.modal-wide')
    await expect(configModal.locator('.config-scope-select').first()).toBeEnabled()
    await configModal.locator('.config-scope-select').first().click()
    const menu = page.locator('.config-scope-menu')
    await expect(menu.getByLabel(/^Telegram 通知/)).toBeChecked()
    await expect(menu.getByLabel(/手机推送/)).toBeChecked()
    if (plugin.scope === 'both') {
      await menu.getByLabel(/企业通知/).check()
      await expect.poll(() => plugin.bot).toBe('tg,bark,wc')
      await expect(menu.getByLabel(/企业通知/)).toBeEnabled()
    } else {
      await expect(menu.getByLabel(/企业通知/)).toBeChecked()
    }
    await menu.getByLabel(/手机推送/).uncheck()
    await expect.poll(() => plugin.bot.split(',').sort()).toEqual(['tg', 'wc'])
    await expect(menu.getByLabel(/^Telegram 通知/)).toBeChecked()
    await expect(menu.getByLabel(/企业通知/)).toBeChecked()
    await expect(menu.getByLabel(/手机推送/)).not.toBeChecked()
    await expect(page.locator('.toast-enter-active, .toast-leave-active')).toHaveCount(0)
    await expectInsideViewport(page)
    await configModal.getByRole('button', { name: '关闭', exact: true }).click()
  }
  expect(routeSaves).toHaveLength(6)
})

test('iPhone 17 仓库地址不会被删除按钮挤压', async ({ page }) => {
  await page.goto('/#/plugins')
  await page.getByRole('button', { name: /插件市场/ }).click()
  await page.getByRole('button', { name: '设置仓库地址' }).click()
  const input = page.getByPlaceholder('例如 AWdress/AWBotNest-Plugins')
  const remove = page.getByRole('button', { name: '删除第 1 个仓库' })
  await expect(input).toHaveValue('Example/AWBotNest-Plugins')
  const geometry = await page.evaluate(() => {
    const row = document.querySelector('.repo-row')
    const input = row.querySelector('input')
    const remove = row.querySelector('.repo-delete')
    const rowBox = row.getBoundingClientRect()
    const inputBox = input.getBoundingClientRect()
    const removeBox = remove.getBoundingClientRect()
    return {
      display: getComputedStyle(row).display,
      rowWidth: rowBox.width,
      inputWidth: inputBox.width,
      removeWidth: removeBox.width,
      removeHeight: removeBox.height,
    }
  })
  expect(geometry.display).toBe('grid')
  expect(geometry.inputWidth).toBeGreaterThan(geometry.rowWidth * 0.75)
  expect(geometry.removeWidth).toBe(44)
  expect(geometry.removeHeight).toBe(44)
  await remove.click()
  await expect(input).toBeHidden()
})

test('插件三点菜单只在底部空间不足时向上展开', async ({ page }) => {
  const lowerPlugin = {
    ...configurablePlugin,
    id: 'mobile_bottom_menu_test',
    name: '底部菜单测试',
    description: '用于验证菜单按视口空间自动翻转',
  }
  await page.route('**/api/plugins', (route) => json(route, {
    plugins: [configurablePlugin, lowerPlugin],
  }))
  await page.goto('/#/plugins')
  const cards = page.locator('.plugin-card')
  await expect(cards).toHaveCount(2)

  const upperMenu = cards.first().getByRole('button', { name: '更多' })
  await upperMenu.click()
  await expect(cards.first().locator('.dropdown')).toBeVisible()
  await expect(cards.first().locator('.dropdown')).not.toHaveClass(/open-above/)
  // 通过同一个触发按钮关闭，避免点击页面右下角的悬浮搜索按钮。
  await upperMenu.click()
  await expect(cards.first().locator('.dropdown')).toBeHidden()

  const lowerCard = cards.last()
  await lowerCard.scrollIntoViewIfNeeded()
  await lowerCard.getByRole('button', { name: '更多' }).click()
  await expect(lowerCard.locator('.dropdown')).toHaveClass(/open-above/)
})

test('iPhone 17 横屏仍可使用顶部与底部菜单', async ({ page }) => {
  await page.setViewportSize({ width: 874, height: 402 })
  await page.goto('/#/settings')
  await expect(page.locator('[data-app-topbar]')).toBeVisible()
  await expect(page.locator('[data-mobile-navigation-dock]')).toBeHidden()
  await expectInsideViewport(page)
})
