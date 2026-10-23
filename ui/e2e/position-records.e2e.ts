import { expect, test } from '@playwright/test'

test.beforeEach(async ({ request }) => { await request.post('/api/v1/test/orders/reset') })

const snapshot = (channel: string) => ({ id: 1, account_id: 1, source: 'manual', execution_id: null,
  created_at: '2026-10-19T10:30:00+08:00', assets: { source: 'real', currency: channel === 'external' ? 'USD' : 'CNY',
    positions: [{ symbol: channel === 'external' ? 'CME.ES2612' : 'rb2610', direction: '多头', volume: 3, available_volume: 2, market_value: 105000, avg_price: 99999,
      extra: { long_yd: 2, quote_asset: 'USD', position_attributes: {
        volume: 3, market_value: 105000, today_volume: 1, yesterday_volume: 2,
        ...(channel === 'ctp' ? { position_profit: -12, margin: 1000, long_frozen: 0 } : channel === 'external' ? {
          open_price: null, unrealized_pnl: 0, liquidation_price: null, mark_price: null, break_even_price: null,
          native_notional: 105000, initial_margin: 50, maintenance_margin: null, isolated_margin: null, isolated_wallet: null, margin_asset: 'USD', adl: null,
        }
          : { [channel === 'gm' ? 'position_price' : 'open_price']: 3500, float_profit: -12, margin: 1000,
              ...(channel === 'tq' ? { position_price: 2183, position_profit: 0, open_cost: 1585210.85, position_cost: 1571760, frozen: 0 }
                : { available_now: 0, position_cost: 1000, amount: 10500, last_price: 3501, today_volume: 1, frozen: 0 }), }),
      } } }],
  } })

for (const channel of ['ctp', 'tq', 'gm', 'external']) test(`position table uses native ${channel} fields and keeps missing prices unknown`, async ({ page }) => {
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
  expect(headers.slice(0, 5)).toEqual(['品种', '方向', '持仓', '可用', channel === 'external' ? '名义价值' : channel === 'gm' ? '市值' : '货值'])
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
  if (channel === 'external') {
    await expect(cells.nth(3).locator('.overflow-text-static')).toHaveText('—')
    await expect(cells.nth(5).locator('.overflow-text-static')).toHaveText('—')
    await expect(cells.nth(6).locator('.overflow-text-static')).toHaveText('0.00 美元')
    await expect(cells.nth(7).locator('.overflow-text-static')).toHaveText('—')
  }
  if (channel === 'gm') await expect(cells.nth(3).locator('.overflow-text-static')).toHaveText('0 手')
  await expect(cells.nth(4).locator('.overflow-text-static')).toHaveText(channel === 'external' ? '105,000.00 美元' : '105,000.00 元')
  expect(reconciles).toBe(0)
  await table.getByRole('button', { name: /^展开持仓/ }).click()
  await expect(table.getByRole('columnheader', { name: channel === 'external' ? '初始保证金' : channel === 'gm' ? '持仓成本' : '占用保证金', exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '核对持仓', exact: true }).click()
  await expect.poll(() => reconciles).toBe(1)
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('External native risk fields display directly in the wide main table', async ({ page }) => {
  const data = snapshot('external')
  Object.assign(data.assets.positions[0].extra.position_attributes, {
    open_price: 64000.123, mark_price: 65000.456, break_even_price: 64001.789, liquidation_price: 0,
    maintenance_margin: 12.345, isolated_margin: 100.567, isolated_wallet: 90.123, adl: 3,
  })
  await page.route('**/api/v1/account/1/asset_snapshots*', route => route.fulfill({ json: { data: [data], count: 1 } }))
  await page.goto('/e2e/orders.html?channel=external')
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  const table = dialog.locator('[data-swap-current] [data-position-table]')
  await expect(table.locator('[data-activity-row]')).toHaveCount(1)
  const headers = await table.locator('th').allTextContents()
  for (const [label, value] of Object.entries({
    开仓均价: '64,000.12', 标记价: '65,000.46', 保本价: '64,001.79', 强平价: '0.00',
    初始保证金: '50.00 美元', 维持保证金: '12.35 美元', 逐仓保证金: '100.57 美元', 逐仓钱包余额: '90.12 美元', 自动减仓排名: '3',
  })) await expect(table.locator('[data-activity-row] td').nth(headers.indexOf(label)).locator('.overflow-text-static')).toHaveText(value)
  await page.setViewportSize({ width: 375, height: 812 })
  await table.locator('..').evaluate(node => { node.scrollLeft = node.scrollWidth })
  await expect(table.getByRole('button', { name: /^展开持仓/ })).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('sparse external native fields share columns while unknown currency and converted values stay explicit', async ({ page }) => {
  const base = snapshot('external')
  const data = { ...base, assets: { ...base.assets, positions: [
    { ...base.assets.positions[0], market_value: 0, extra: { quote_asset: 'USDQ', position_attributes: { open_price: null, native_notional: 0 } } },
    { ...base.assets.positions[0], symbol: 'CME.NQ2612', market_value: 125, extra: { quote_asset: 'USDQ', position_attributes: { unrealized_pnl: 0, native_notional: -100 } } },
  ] } }
  await page.route('**/api/v1/account/1/asset_snapshots*', route => route.fulfill({ json: { data: [data], count: 1 } }))
  await page.goto('/e2e/orders.html?channel=external')
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  const table = dialog.locator('[data-swap-current] [data-position-table]')
  expect(await table.locator('th').allTextContents()).toEqual(['品种', '方向', '持仓', '可用', '名义价值', '开仓均价', '未实现盈亏', '详情'])
  const rows = table.locator('[data-activity-row]')
  await expect(rows).toHaveCount(2)
  for (const row of [rows.nth(0), rows.nth(1)]) {
    await expect(row.locator('td')).toHaveCount(8)
    await expect(row.locator('td').nth(3).locator('.overflow-text-static')).toHaveText('—')
    await expect(row.locator('td').nth(5).locator('.overflow-text-static')).toHaveText('—')
  }
  await expect(rows.nth(0).locator('td').nth(4).locator('.overflow-text-static')).toHaveText('0.00 美元')
  await expect(rows.nth(0).locator('td').nth(6).locator('.overflow-text-static')).toHaveText('—')
  await expect(rows.nth(1).locator('td').nth(4).locator('.overflow-text-static')).toHaveText('125.00 美元')
  await expect(rows.nth(1).locator('td').nth(6).locator('.overflow-text-static')).toHaveText('0.00 USDQ')
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
  const retainedScroll = await scroll.evaluate(node => {
    // 使用有效的中间位置，避免字体及视口差异把测试偏移夹到滚动末端。
    node.scrollTop = Math.min(500, Math.max(0, node.scrollHeight - node.clientHeight - 100))
    node.dispatchEvent(new Event('scroll'))
    return node.scrollTop
  })
  expect(retainedScroll).toBeGreaterThan(0)
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '品种', exact: true })).toContainText('rb2610')
  await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBe(retainedScroll)
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
  await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBe(retainedScroll)
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await page.getByRole('button', { name: '重新挂载弹窗', exact: true }).click()
  await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '持仓', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBe(retainedScroll)
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
