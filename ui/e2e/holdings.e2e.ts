import { expect, test, type Page } from '@playwright/test'

const at = '2026-10-15T14:48:00'
const evidence = { symbol: 'c2611', sizing_mode: 'weight', status: 'SIZED', reason_code: 'COMMON.SIZING.EXACT',
  strategy_weight: -0.3, account_weight: -0.6, account_multiplier: 2, weight_precision: 0.01,
  equity: 1_000_000, reference_price: 3000, unit_multiplier: 10, unit_notional: 30_000,
  target_notional: 600_000, raw_quantity: -20, target_quantity: -20, current_quantity: -18,
  quantity_step: 1, min_quantity: 1, min_notional: null }
const target = { weights: { c2611: -0.6, m2701: 0.2 }, calculated_at: at, source: 'execution', execution_id: 'exec-1', context_account_id: 1,
  strategy_weights: { c2611: -0.3, m2701: 0.1 }, sizing: { status: 'available', execution_id: 'exec-1', calculated_at: at, rows: { c2611: evidence } } }
const assets = { data: [{ id: 1, account_id: 1, created_at: at, source: 'execution', execution_id: 'exec-1', assets: {
  total_asset: 1_000_000, positions: [{ symbol: 'c2611', direction: '空头', volume: 18, available_volume: 17,
    market_value: 1_800_000, extra: { short_td: 3, short_yd: 15 } }],
} }], count: 1 }
const order = { order_id: 'c-1', symbol: 'c2611', direction: 'SELL', offset: 'OPEN', order_type: 'LIMIT', price: 3001,
  volume: 2, filled_volume: 1, remaining_volume: 1, status: '部分成交', create_time: at,
  execution_id: 'exec-1', native_identity: { order_ref: '12' }, uncertain: false }
const foreignOrder = { ...order, symbol: 'ag2612', order_id: 'other-1', native_identity: {} }
const orders = { data: [order, foreignOrder], count: 2, complete: true, sampled_at: at, cancellations: [] }

async function mock(page: Page, options: { legacy?: boolean; unsynced?: boolean; emptyOrders?: boolean } = {}) {
  await page.addInitScript(snapshot => { Object.assign(window, { holdingsOrders: snapshot }) },
    options.emptyOrders ? { ...orders, data: [], count: 0 } : orders)
  await page.route('**/api/v1/account/*/asset_snapshots*', route => route.fulfill({ json: options.unsynced
    ? { ...assets, data: [{ ...assets.data[0], created_at: '2026-10-15T14:47:00' }] } : assets }))
  await page.route('**/api/v1/account/*/target_snapshot', route => route.fulfill({ json: options.legacy
    ? { ...target, sizing: { ...target.sizing, status: 'legacy', rows: {} } } : target }))
  await page.route('**/api/v1/account/*/orders**', route => route.fulfill({ json: orders }))
}

test('preview shows full ETF codes without pushing numeric columns aside', async ({ page }) => {
  await mock(page, { emptyOrders: true })
  const longSymbol = 'VERY-LONG-INSTRUMENT-IDENTIFIER-THAT-CANNOT-FIT-IN-THE-PREVIEW'
  await page.route('**/api/v1/account/*/asset_snapshots*', route => route.fulfill({ json: {
    ...assets, data: [{ ...assets.data[0], assets: { ...assets.data[0].assets, positions: [] } }],
  } }))
  await page.route('**/api/v1/account/*/target_snapshot', route => route.fulfill({ json: {
    ...target, weights: { '518880.SH': 0.4, '159920.SZ': 0.4, [longSymbol]: 0.2 },
  } }))
  await page.setViewportSize({ width: 416, height: 812 })
  await page.goto('/e2e/holdings.html')
  const table = page.getByRole('table', { name: '当前持仓' })
  for (const width of [416, 520, 800, 416]) {
    await page.setViewportSize({ width, height: 812 })
    for (const symbol of ['518880.SH', '159920.SZ']) {
      const text = table.getByRole('button', { name: `查看 ${symbol} 持仓与目标` }).locator('.overflow-text')
      await expect(text).toHaveAttribute('data-overflow', 'false')
      expect(await text.locator('.overflow-text-static').evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
    }
    const bounds = await table.boundingBox()
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width)
    for (const cell of await table.locator('tbody tr').first().getByRole('cell').all()) {
      expect(await cell.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
    }
  }
  await expect(table.getByRole('button', { name: `查看 ${longSymbol} 持仓与目标` }).locator('.overflow-text'))
    .toHaveAttribute('data-overflow', 'true')
})

test('click a row; explain frozen sizing and only this symbol; Escape clears pointer focus', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const writes: string[] = []
  page.on('request', request => { if (request.method() !== 'GET') writes.push(request.url()) })
  await mock(page)
  await page.goto('/e2e/holdings.html')
  const table = page.getByRole('table', { name: '当前持仓' })
  await expect(table.getByRole('columnheader')).toHaveCount(4)
  await table.getByRole('row').filter({ hasText: 'c2611' }).getByRole('cell').last().click()
  const dialog = page.getByRole('dialog', { name: 'c2611 · 持仓与目标' })
  await expect(dialog).toBeVisible()
  await expect(dialog).not.toContainText('示例账户')
  const calculation = await dialog.getByRole('region', { name: '目标如何计算' }).boundingBox()
  const holdings = await dialog.getByRole('region', { name: '当前持仓分项' }).boundingBox()
  const modal = await dialog.boundingBox()
  expect(modal!.width).toBeGreaterThan(1100)
  expect(calculation!.x).toBeLessThan(holdings!.x)
  expect(Math.abs(calculation!.y - holdings!.y)).toBeLessThan(2)
  const bookRegion = dialog.getByRole('region', { name: '当前持仓分项' })
  await expect(bookRegion.getByRole('row')).toHaveCount(2)
  const bookTable = await bookRegion.getByRole('table').boundingBox()
  expect(Math.abs(bookTable!.width - holdings!.width)).toBeLessThan(2)
  expect(bookTable!.height).toBeLessThan(65)
  const summary = await dialog.locator('dt').filter({ hasText: /^当前持仓$/ }).locator('..').boundingBox()
  expect(summary!.height).toBeLessThan(35)
  await expect(dialog.getByText('目标持仓', { exact: true }).locator('..').locator('dd')).toContainText('空20手')
  await expect(dialog.getByText('在途全部成交后', { exact: true }).locator('..').locator('dd')).toContainText('空 19手')
  await expect(dialog.getByText('预计仍差', { exact: true }).locator('..').locator('dd')).toContainText('卖开 1手')
  await expect(dialog.getByText('策略 空30% × 账户乘数 2 = 账户 空60%')).toBeVisible()
  await expect(dialog.getByText(/权益 100\.0万 × 60%/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '展开委托 c-1' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '展开委托 other-1' })).toHaveCount(0)
  expect(writes).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('holding-detail.png') })
  const parameters = dialog.getByRole('button', { name: '查看计算参数' })
  await expect(parameters).toHaveAttribute('aria-expanded', 'false')
  const panelId = await parameters.getAttribute('aria-controls')
  const panel = dialog.locator(`[id="${panelId}"]`)
  await expect(panel).toHaveAttribute('inert', '')
  expect((await panel.boundingBox())!.height).toBe(0)
  await parameters.click()
  await expect(parameters).toHaveAttribute('aria-expanded', 'true')
  await expect(dialog.getByText('最小交易数量', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(table.getByRole('button', { name: '查看 c2611 持仓与目标' })).not.toBeFocused()
})

test('pointer dismissal leaves no persistent row highlight; keyboard dismissal restores visible focus', async ({ page }) => {
  await mock(page)
  await page.goto('/e2e/holdings.html')
  const entry = page.getByRole('button', { name: '查看 c2611 持仓与目标' })
  const row = entry.locator('xpath=ancestor::tr')
  const dialog = page.getByRole('dialog', { name: 'c2611 · 持仓与目标' })
  await entry.click()
  await expect(page.locator('main')).toHaveAttribute('data-keyboard-opened', 'false')
  await expect(dialog).toBeFocused()
  await page.keyboard.press('Escape')
  await page.mouse.move(5, 5)
  await expect(dialog).toBeHidden()
  await expect(entry).not.toBeFocused()
  await expect(entry).toHaveCSS('outline-style', 'none')
  await expect(row).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await entry.click()
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await page.mouse.move(5, 5)
  await expect(dialog).toBeHidden()
  await expect(entry).not.toBeFocused()
  await expect(row).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await entry.focus()
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(entry).toBeFocused()
  expect(await entry.evaluate(element => element.matches(':focus-visible'))).toBe(true)
  await expect(row).not.toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await entry.click()
  await page.mouse.click(5, 5)
  await expect(dialog).toBeHidden()
  await expect(entry).not.toBeFocused()
  await expect(row).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
})

test('target-only entry supports keyboard, legacy target never invents quantities; narrow modal fits', async ({ page }) => {
  await mock(page, { legacy: true })
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/e2e/holdings.html')
  const entry = page.getByRole('button', { name: '查看 m2701 持仓与目标' })
  await expect(entry).toBeVisible()
  await expect(entry.locator('xpath=ancestor::tr').getByRole('cell').nth(1)).toHaveText('空仓')
  await entry.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: 'm2701 · 持仓与目标' })
  await expect(dialog.getByText('当时未记录换算依据', { exact: false })).toBeVisible()
  await expect(dialog.getByText('目标持仓', { exact: true }).locator('..').locator('dd')).toContainText('当时未记')
  await expect(dialog.getByText('该品种当前无在途委托')).toBeVisible()
  const box = await dialog.boundingBox()
  expect(box?.x).toBeGreaterThanOrEqual(0)
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(375)
})

test('unsynchronized sampling and failed order refresh retain evidence but disable definite projection and cancel', async ({ page }) => {
  await mock(page, { unsynced: true })
  await page.goto('/e2e/holdings.html')
  await page.getByRole('button', { name: '查看 c2611 持仓与目标' }).click()
  const dialog = page.getByRole('dialog', { name: 'c2611 · 持仓与目标' })
  await expect(dialog.getByText(/持仓与委托采样时间不同/)).toHaveCount(0)
  await dialog.getByRole('button', { name: '在途全部成交后待确认原因' }).focus()
  await expect(page.getByRole('tooltip')).toContainText('持仓与委托采样时间不同')
  await expect(dialog.getByText('在途全部成交后', { exact: true }).locator('..').locator('dd')).toContainText('待确认')
  await page.route('**/api/v1/account/*/orders**', route => route.fulfill({ status: 502, json: { detail: '柜台查询失败' } }))
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect(dialog.getByRole('button', { name: '撤单 c2611 c-1' })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '核对失败' })).toBeVisible()
  await expect(dialog.getByText('目标持仓', { exact: true }).locator('..').locator('dd')).toContainText('空20手')
})

test('single cancel shares confirmed terminal receipts and preserves fills in the symbol dialog', async ({ page }) => {
  await mock(page)
  let posts = 0
  let terminal = false
  let requestId = ''
  const receipt = () => ({ request_id: requestId, order_id: order.order_id, symbol: order.symbol,
    state: terminal ? 'terminal' : 'submitted', message: terminal ? '订单实际状态：已撤销' : '撤单请求已提交，等待实际回报', requested_at: at,
    order: terminal ? { ...order, status: '已撤销' } : order })
  await page.route('**/api/v1/account/*/orders**', async route => {
    if (route.request().method() === 'POST') {
      posts++
      requestId = route.request().postDataJSON().request_id
      await route.fulfill({ status: 202, json: receipt() })
    } else await route.fulfill({ json: { ...orders, data: terminal ? [foreignOrder] : orders.data,
      cancellations: requestId ? [receipt()] : [] } })
  })
  await page.goto('/e2e/holdings.html')
  await page.getByRole('button', { name: '查看 c2611 持仓与目标' }).click()
  const dialog = page.getByRole('dialog', { name: 'c2611 · 持仓与目标' })
  const cancel = dialog.getByRole('button', { name: '撤单 c2611 c-1' })
  await cancel.click()
  await expect(cancel).toBeDisabled()
  await expect(dialog.getByText('撤单确认中', { exact: true }).filter({ visible: true })).toBeVisible()
  terminal = true
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect(dialog.getByText('该品种当前无在途委托')).toBeVisible()
  await expect(dialog.getByText(/已成 1 \/ 2/)).toBeVisible()
  expect(posts).toBe(1)
})

test('production account card keeps target entries when comparison is unavailable; account change closes old detail', async ({ page }) => {
  page.on('pageerror', error => { throw error })
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    const json = /\/account\/\d+$/.test(path) ? { id: Number(path.split('/').at(-1)), name: '合成账户', market: 'demo', trade_channel: 'paper',
      is_started: false, cron_expr: '', portfolio_id: 9, algorithm: { method: 'SINGLE-MAKER', params: {} },
      account_config: {}, forbidden_symbols: [], risk_symbols: [], trade_rules: [], long_leverage: 1, short_leverage: 1, weight_precision: .001 }
      : path.endsWith('/runtime-sync') ? { status: 'synchronized', revision: 1, attempts: 1 }
        : path.endsWith('/rebalance_plan') ? { rows: [], off_symbol_count: null, observed_at: at, target_calculated_at: at }
          : path.endsWith('/next_run_time') ? { next_run_time: null, next_execution_times: [] }
            : path.endsWith('/algorithms') ? [] : { data: [], count: 0 }
    return route.fulfill({ json })
  })
  await mock(page)
  await page.goto('/e2e/errors.html?mode=account')
  const entry = page.getByRole('button', { name: '查看 m2701 持仓与目标' })
  await expect(entry).toBeVisible()
  await entry.click()
  const dialog = page.getByRole('dialog', { name: 'm2701 · 持仓与目标' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('目标持仓', { exact: true }).locator('..').locator('dd')).toContainText('无法换算')
  await page.getByRole('button', { name: '切换账户', exact: true }).evaluate((node: HTMLButtonElement) => node.click())
  await expect(dialog).toBeHidden()
})


test('landscape detail retains both nonzero directions and moves prediction out when there are no orders', async ({ page }) => {
  await mock(page, { emptyOrders: true })
  await page.route('**/api/v1/account/*/asset_snapshots*', route => route.fulfill({ json: { ...assets,
    data: [{ ...assets.data[0], assets: { ...assets.data[0].assets,
      positions: [...assets.data[0].assets.positions, { symbol: 'c2611', direction: '多头', volume: 3,
        available_volume: 2, market_value: 90_000, extra: { long_td: 1, long_yd: 2 } }],
    } }],
  } }))
  await page.route('**/api/v1/account/*/orders**', route => route.fulfill({ json: { ...orders, data: [], count: 0 } }))
  await page.goto('/e2e/holdings.html')
  await page.getByRole('button', { name: '查看 c2611 持仓与目标' }).first().click()
  const dialog = page.getByRole('dialog', { name: 'c2611 · 持仓与目标' })
  await expect(dialog.getByRole('region', { name: '当前持仓分项' }).getByRole('row')).toHaveCount(3)
  await expect(dialog.getByText('该品种当前无在途委托')).toBeVisible()
  await expect(dialog.getByText('在途全部成交后', { exact: true })).toHaveCount(0)
  await expect(dialog.getByText('待调', { exact: true }).locator('..').locator('dd')).toContainText('卖平 3手')
})

test('holding and account dialogs share the stream snapshot without polling and isolate accounts', async ({ page }) => {
  await page.clock.install()
  await mock(page)
  const reads: number[] = []
  await page.route('**/api/v1/account/*/orders**', route => {
    reads.push(Number(new URL(route.request().url()).pathname.split('/').at(-2)))
    return route.fulfill({ json: orders })
  })
  await page.goto('/e2e/holdings.html')
  await page.getByRole('button', { name: '查看 c2611 持仓与目标' }).click()
  const dialog = page.getByRole('dialog', { name: 'c2611 · 持仓与目标' })
  await expect(dialog.getByRole('button', { name: '展开委托 c-1' })).toBeVisible()
  await page.clock.runFor(16000)
  expect(reads).toEqual([])
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect.poll(() => reads.length).toBe(1)
  expect(reads).toEqual([1])
  await page.clock.runFor(10000)
  expect(reads).toEqual([1])
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  const accountEntry = page.getByRole('button', { name: /^持仓、委托与成交 · 在途 / })
  await expect(accountEntry).toContainText('持仓、委托与成交 · 在途 2')
  await page.clock.runFor(15100)
  expect(reads).toEqual([1])
  await accountEntry.click()
  const accountDialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await expect(accountDialog.getByRole('button', { name: '展开委托 c-1' })).toBeVisible()
  await expect(accountDialog.getByRole('button', { name: '自动拉取' })).toHaveCount(0)
  expect(reads).toEqual([1])
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '切换账户' }).click()
  await page.getByRole('button', { name: '查看 c2611 持仓与目标' }).click()
  await expect(dialog.getByText('该品种当前无在途委托')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '展开委托 c-1' })).toHaveCount(0)
  await page.clock.runFor(10000)
  expect(reads).toEqual([1])
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect.poll(() => reads.at(-1)).toBe(2)
})
