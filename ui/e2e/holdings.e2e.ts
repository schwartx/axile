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

async function mock(page: Page, options: { legacy?: boolean; unsynced?: boolean } = {}) {
  await page.route('**/api/v1/account/*/asset_snapshots*', route => route.fulfill({ json: options.unsynced
    ? { ...assets, data: [{ ...assets.data[0], created_at: '2026-10-15T14:47:00' }] } : assets }))
  await page.route('**/api/v1/account/*/target_snapshot', route => route.fulfill({ json: options.legacy
    ? { ...target, sizing: { ...target.sizing, status: 'legacy', rows: {} } } : target }))
  await page.route('**/api/v1/account/*/orders**', route => route.fulfill({ json: orders }))
}

test('click a row; explain frozen sizing and only this symbol; Escape restores focus', async ({ page }, testInfo) => {
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
  await expect(dialog.getByText('目标持仓', { exact: true }).locator('..').locator('dd')).toContainText('空20手')
  await expect(dialog.getByText('在途全部成交后', { exact: true }).locator('..').locator('dd')).toContainText('空 19手')
  await expect(dialog.getByText('预计仍差', { exact: true }).locator('..').locator('dd')).toContainText('卖开 1手')
  await expect(dialog.getByText('策略 空30% × 账户乘数 2 = 账户 空60%')).toBeVisible()
  await expect(dialog.getByText(/权益 100\.0万 × 60%/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '展开委托 c-1' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '展开委托 other-1' })).toHaveCount(0)
  expect(writes).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('holding-detail.png') })
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(table.getByRole('button', { name: '查看 c2611 持仓与目标' })).toBeFocused()
})

test('target-only entry supports keyboard, legacy target never invents quantities; narrow modal fits', async ({ page }) => {
  await mock(page, { legacy: true })
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/e2e/holdings.html')
  const entry = page.getByRole('button', { name: '查看 m2701 持仓与目标' })
  await expect(entry).toBeVisible()
  await expect(entry.locator('xpath=ancestor::tr').getByRole('cell').nth(1).locator('.overflow-text-static')).toHaveText('空仓')
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
  await expect(dialog.getByText(/持仓与委托采样时间不同/)).toBeVisible()
  await expect(dialog.getByText('在途全部成交后', { exact: true }).locator('..').locator('dd')).toContainText('待确认')
  await page.route('**/api/v1/account/*/orders**', route => route.fulfill({ status: 502, json: { detail: '柜台查询失败' } }))
  await dialog.getByRole('button', { name: '刷新当前委托' }).click()
  await expect(dialog.getByRole('button', { name: '撤单 c2611 c-1' })).toBeDisabled()
  await expect(dialog.getByText('当前显示最后一次查询结果，请刷新核对。')).toBeVisible()
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
  await dialog.getByRole('button', { name: '刷新当前委托' }).click()
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
