import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  page.on('pageerror', error => { throw error })
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.emulateMedia({ colorScheme: 'dark' })
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/replay/raw/')) {
      const response = await route.fetch({ url: `http://127.0.0.1:15481${url.pathname}${url.search}` })
      return route.fulfill({ response })
    }
    const json = url.pathname.endsWith('/init/status') ? { configured: true, values: {} }
      : url.pathname.endsWith('/capabilities/channels') ? []
      : url.pathname.endsWith('/account/dashboard') ? { data: [{ account_id: 7, name: '回放验收', trade_channel: 'paper', market: 'demo', is_started: false }] }
      : { data: [], count: 0 }
    return route.fulfill({ json })
  })
})

test('the original complete workbench reads V2 recordings through WASM and restores chart, ladder, transport, fullscreen and details', async ({ page }) => {
  const legacyRequests: string[] = []
  page.on('request', request => {
    if (/^\/api\/v1\/account\/executions\/[^/]+\/replay(?:\/window)?$/.test(new URL(request.url()).pathname) && request.method() === 'GET') legacyRequests.push(request.url())
  })
  await page.goto('/accounts/7/executions/E/replay')
  await page.getByRole('button', { name: '下载并进入回放' }).click()
  await expect(page.locator('.replay-workbench')).toBeVisible()
  await expect(page.locator('.replay-chart')).toBeVisible()
  await expect(page.getByRole('button', { name: '跳到首个盘口' })).toBeEnabled({ timeout: 15000 })
  await page.getByRole('button', { name: '跳到首个盘口' }).click()
  const ladder = page.getByRole('list', { name: '当前已录价位' })
  await expect(ladder).toContainText('买1')
  await expect(ladder).toContainText('卖1')
  await expect(ladder).toContainText('0.003876')
  await expect(page.getByRole('button', { name: '1×', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '委托', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '成交', exact: true })).toBeVisible()
  await expect(page.locator('.replay-heat-scale')).toContainText('挂量峰值')
  await page.getByRole('button', { name: '后一观察' }).click()
  await expect(ladder.locator('.replay-ladder-order-tag')).toHaveText('买 250')
  await page.getByRole('button', { name: '播放回放' }).click()
  await expect(page.getByRole('button', { name: '暂停回放' })).toBeVisible()
  await page.getByRole('button', { name: '暂停回放' }).click()
  await page.getByRole('button', { name: '全屏', exact: true }).click()
  await expect(page.locator('.replay-workbench-fullscreen')).toBeVisible()
  await page.getByRole('button', { name: '退出全屏', exact: true }).click()
  await expect(page.locator('.replay-workbench-fullscreen')).toHaveCount(0)
  const timeline = page.getByRole('slider', { name: '录制定位' })
  await timeline.focus()
  await page.keyboard.press('End')
  await page.getByRole('button', { name: '成交', exact: true }).click()
  await expect(page.getByRole('region', { name: '已录自有成交，未来记录标记未到达' }).getByRole('button', { name: /O1 · 买 · 本次执行 25 @ 0.003876/ })).toBeVisible()
  await page.getByRole('button', { name: '委托', exact: true }).click()
  await expect(page.getByRole('button', { name: '展开区间统计' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: '回放区间统计' })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/replay-original-restored.png', fullPage: true })
  await page.reload()
  await expect(page.locator('.replay-chart')).toBeVisible()
  await expect(page.getByRole('button', { name: '下载并进入回放' })).toHaveCount(0)
  expect(legacyRequests).toEqual([])
})

test('many small blocks load in batches and skip other symbols while preserving the original workbench', async ({ page }) => {
  const batches: number[][] = []
  page.on('request', request => {
    if (new URL(request.url()).pathname.endsWith('/raw/blocks')) batches.push(request.postDataJSON().blocks)
  })
  await page.goto('/accounts/7/executions/E-bulk/replay')
  await page.getByRole('button', { name: '下载并进入回放' }).click()
  await expect(page.locator('.replay-chart')).toBeVisible({ timeout: 15000 })
  await expect(page.getByRole('button', { name: '跳到首个盘口' })).toBeEnabled()
  expect(batches.map(batch => batch.length)).toEqual([70])
  expect(batches.flat()).toHaveLength(70)
  await page.getByRole('button', { name: '选择回放品种' }).click()
  await page.getByRole('option', { name: 'ZZZ', exact: true }).click()
  await expect(page.locator('.replay-chart')).toBeVisible()
  await expect.poll(() => batches.flat().length).toBe(140)
})
