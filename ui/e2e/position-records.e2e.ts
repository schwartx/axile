import { expect, test } from '@playwright/test'

test.beforeEach(async ({ request }) => { await request.post('/api/v1/test/orders/reset') })

const snapshot = (channel: string) => ({ id: 1, account_id: 1, source: 'manual', execution_id: null,
  created_at: '2026-10-19T10:30:00+08:00', assets: { source: 'real', currency: 'CNY',
    positions: [{ symbol: 'rb2610', direction: '多头', volume: 3, available_volume: 2, market_value: 105000, avg_price: 99999,
      extra: { long_yd: 2, quote_asset: 'CNY', position_attributes: {
        volume: 3, available_now: 0, market_value: 105000, today_volume: 1, yesterday_volume: 2,
        ...(channel === 'ctp' ? { position_profit: -12, margin: 1000, long_frozen: 0 }
          : { [channel === 'gm' ? 'position_price' : 'open_price']: 3500, float_profit: -12, margin: 1000,
              ...(channel === 'tq' ? { position_price: 2183, position_profit: 0, open_cost: 1585210.85, position_cost: 1571760, frozen: 0 }
                : { position_cost: 1000, amount: 10500, last_price: 3501, today_volume: 1, frozen: 0 }), }),
      } } }],
  } })

for (const channel of ['ctp', 'tq', 'gm']) test(`position table uses native ${channel} fields and keeps missing prices unknown`, async ({ page }) => {
  const data = snapshot(channel)
  let reconciles = 0
  await page.route('**/api/v1/account/1/asset_snapshots*', route => route.fulfill({ json: { data: [data], count: 1 } }))
  await page.route('**/api/v1/account/1/assets/refresh', route => { reconciles++; return route.fulfill({ json: data }) })
  await page.goto(`/e2e/orders.html?channel=${channel}`)
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  const table = dialog.locator('[data-swap-current] [data-position-table]')
  await expect(table.locator('[data-activity-row]')).toHaveCount(1)
  await expect(table).not.toContainText('99,999')
  const headers = await table.locator('th').allTextContents()
  expect(headers.slice(0, 5)).toEqual(['品种', '方向', '持仓', '可用', channel === 'gm' ? '市值' : '货值'])
  const cells = table.locator('[data-activity-row] td')
  const columnValue = (label: string) => cells.nth(headers.indexOf(label)).locator('.overflow-text-static')
  if (channel === 'tq') {
    await expect(columnValue('持仓均价')).toHaveText('2,183.00')
    await expect(columnValue('持仓盈亏')).toHaveText('0.00')
    await expect(columnValue('开仓成本')).toHaveText('1,585,210.85')
    await expect(columnValue('持仓成本')).toHaveText('1,571,760.00')
    await expect(columnValue('冻结数量')).toHaveText('0')
  }
  if (channel === 'ctp') await expect(columnValue('冻结数量')).toHaveText('0')
  if (channel === 'gm') {
    await expect(columnValue('当前价格')).toHaveText('3,501.00')
    await expect(columnValue('持仓成本')).toHaveText('1,000.00')
    await expect(columnValue('持仓额')).toHaveText('10,500.00')
  }
  if (channel === 'ctp') await expect(cells.nth(6).locator('.overflow-text-static')).toHaveText('—')
  if (channel === 'gm') await expect(cells.nth(3).locator('.overflow-text-static')).toHaveText('0 手')
  await expect(cells.nth(4).locator('.overflow-text-static')).toHaveText('105,000.00 元')
  expect(reconciles).toBe(0)
  await table.getByRole('button', { name: /^展开持仓/ }).click()
  await expect(table.getByRole('columnheader', { name: channel === 'gm' ? '持仓成本' : '占用保证金', exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '核对持仓', exact: true }).click()
  await expect.poll(() => reconciles).toBe(1)
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('position tab remembers its own filter and scroll and preserves data after failed reconcile', async ({ page }) => {
  const data = snapshot('tq')
  data.assets.positions = Array.from({ length: 35 }, (_, index) => ({ ...data.assets.positions[0], symbol: index < 30 ? 'rb2610' : 'ag2612', direction: index % 2 ? '空头' : '多头' }))
  await page.route('**/api/v1/account/1/asset_snapshots*', route => route.fulfill({ json: { data: [data], count: 1 } }))
  await page.goto('/e2e/orders.html?memory')
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-activity-row]')).toHaveCount(35)
  await dialog.getByRole('button', { name: '品种', exact: true }).click()
  await page.getByRole('option', { name: 'rb2610', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-activity-row]')).toHaveCount(30)
  const scroll = dialog.locator('[data-activity-scroll]')
  await scroll.evaluate(node => { node.scrollTop = 500; node.dispatchEvent(new Event('scroll')) })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '品种', exact: true })).toContainText('rb2610')
  // 状态提示改变行高时，浏览器滚动锚定可能产生 1px 舍入差。
  await expect.poll(() => scroll.evaluate(node => Math.abs(node.scrollTop - 500))).toBeLessThanOrEqual(1)
  await page.route('**/api/v1/account/1/assets/refresh', route => route.fulfill({ status: 502, json: { detail: 'snapshot failed' } }))
  await dialog.getByRole('button', { name: '核对持仓', exact: true }).click()
  const notice = dialog.getByRole('img', { name: '本次核对未完成', exact: true })
  await expect(notice).toBeVisible()
  await expect(dialog).not.toContainText('持仓读取失败')
  // 后台成功读取旧快照不能冒充渠道核对成功。
  await page.evaluate(async () => { const { notifyReadRecovery } = await import('/src/lib/readRecovery.ts'); await notifyReadRecovery() })
  await expect(notice).toBeVisible()
  await page.route('**/api/v1/account/1/assets/refresh', route => route.fulfill({ json: data }))
  await dialog.getByRole('button', { name: '核对持仓', exact: true }).click()
  await expect(notice).toHaveCount(0)
  await expect(dialog.locator('[data-swap-current] [data-activity-row]')).toHaveCount(30)
  await expect.poll(() => scroll.evaluate(node => Math.abs(node.scrollTop - 500))).toBeLessThanOrEqual(1)
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await page.getByRole('button', { name: '重新挂载弹窗', exact: true }).click()
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '持仓', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect.poll(() => scroll.evaluate(node => Math.abs(node.scrollTop - 500))).toBeLessThanOrEqual(1)
})

test('snapshot failures show a delayed quiet dot and clear after recovery', async ({ page }) => {
  let failed = false
  await page.route('**/api/v1/account/1/asset_snapshots*', route => failed
    ? route.fulfill({ status: 502, json: { detail: 'temporarily unavailable' } })
    : route.fulfill({ json: { data: [snapshot('tq')], count: 1 } }))
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  const rows = dialog.locator('[data-swap-current] [data-position-table] [data-activity-row]')
  await expect(rows).toHaveCount(1)
  await page.clock.install()
  await page.clock.pauseAt(new Date())
  const recover = () => page.evaluate(async () => { const { notifyReadRecovery } = await import('/src/lib/readRecovery.ts'); await notifyReadRecovery() })
  const notice = dialog.getByRole('img', { name: '持仓更新暂不可用', exact: true })
  failed = true
  await recover()
  await page.clock.runFor(4999)
  await expect(notice).toHaveCount(0)
  failed = false
  await recover()
  await page.clock.runFor(1)
  await expect(notice).toHaveCount(0)
  failed = true
  await recover()
  await page.clock.runFor(5000)
  await expect(notice).toBeVisible()
  await expect(rows).toHaveCount(1)
  await expect(dialog).not.toContainText('持仓读取失败')
  await notice.focus()
  await page.clock.runFor(200)
  await expect(page.getByRole('tooltip')).toContainText('持仓更新暂不可用。当前显示上次读取结果。')
  failed = false
  await recover()
  await expect(notice).toHaveCount(0)
})

test('initial snapshot failure does not claim a flat account', async ({ page }) => {
  await page.route('**/api/v1/account/1/asset_snapshots*', route => route.fulfill({ status: 502, json: { detail: 'unavailable' } }))
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog).toContainText('持仓暂不可用，请稍后重试')
  await expect(dialog).not.toContainText('当前空仓')
  await expect(dialog).not.toContainText('持仓读取失败')
})

test('unobserved or degraded positions never claim a flat account', async ({ page }) => {
  await page.route('**/api/v1/account/1/asset_snapshots*', route => route.fulfill({ json: { data: [{ ...snapshot('tq'), assets: { source: 'unavailable', positions: [] } }], count: 1 } }))
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog).toContainText('持仓尚未查询')
  await expect(dialog).not.toContainText('当前空仓')
})

test('background snapshot reads do not disable manual position reconciliation', async ({ page }) => {
  let hold = false
  let reading = false
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/account/1/asset_snapshots*', async route => {
    if (hold) { reading = true; await gate }
    await route.fulfill({ json: { data: [snapshot('tq')], count: 1 } })
  })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-position-table] [data-activity-row]')).toHaveCount(1)
  hold = true
  try {
    await expect.poll(() => reading, { timeout: 7000 }).toBe(true)
    await expect(dialog.getByRole('button', { name: '核对持仓', exact: true })).toBeEnabled()
    await expect(dialog.getByRole('button', { name: '核对持仓', exact: true })).toHaveText('核对持仓')
  } finally { release() }
})
