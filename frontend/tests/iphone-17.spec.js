import { expect, test } from '@playwright/test'

const status = {
  version: '2.0.0.2', telegram_configured: false, clients: [], accounts: [],
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
      key_configured: false, key_enabled: false, key_active: false,
      binary_mode: 'legacy_free',
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
        access_token: { type: 'password', label: '访问令牌' },
      },
      values: { enabled: true, access_token: '********' }, render_mode: 'schema', has_frontend: false,
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
  await expect(page.getByText('CloakBrowser 0.5.10')).toBeVisible()
  await expect(page.getByText(/尚未填写 Key，将使用旧版免费内核/)).toBeVisible()
  await expectInsideViewport(page)
  const overflow = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    page: document.documentElement.scrollWidth,
  }))
  expect(overflow.page).toBeLessThanOrEqual(overflow.viewport)
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
