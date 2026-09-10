import { expect, test } from '@playwright/test'

const status = {
  version: '2.0.0.1', telegram_configured: false, clients: [], accounts: [],
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
  WEBHOOK_SECRET: '', API_KEY: '', PLUGIN_REPOS: [],
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
    if (path === '/api/ai/settings') return json(route, {
      settings: aiSettings,
      status: { configured: true, detected_protocols: { primary: 'responses' }, usage: {
        total: 8, succeeded: 7, failed: 1, active: 0,
        input_tokens: 1200, output_tokens: 340, total_tokens: 1540,
      } },
    })
    if (path === '/api/ai/plugins') return json(route, { plugins: [] })
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
    if (path === '/api/plugins') return json(route, { plugins: [] })
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

test('iPhone 17 外壳保留悬浮导航且内容不被遮挡', async ({ page }) => {
  await page.goto('/#/status')
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

test('iPhone 17 完整显示 AI 服务、协议和调用明细', async ({ page }) => {
  await page.goto('/#/settings')
  await page.getByRole('button', { name: 'AI 服务', exact: true }).click()
  await expect(page.getByText('最近调用', { exact: true })).toBeVisible()
  await expect(page.getByText(/当前识别为 Responses/)).toBeVisible()
  await expect(page.getByText('多站签到', { exact: true })).toBeVisible()
  await expect(page.getByLabel('筛选调用状态')).toBeVisible()
  await expectInsideViewport(page)
})

test('iPhone 17 横屏仍可使用顶部与底部菜单', async ({ page }) => {
  await page.setViewportSize({ width: 874, height: 402 })
  await page.goto('/#/settings')
  await expect(page.locator('[data-app-topbar]')).toBeVisible()
  await expect(page.locator('[data-mobile-navigation-dock]')).toBeHidden()
  await expectInsideViewport(page)
})
