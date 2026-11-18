import { expect, test } from '@playwright/test'

const account = (account_id: number) => ({
  account_id, name: `CTP ${account_id}`, market: '期货', trade_channel: 'ctp', remark: null,
  is_started: false, is_scheduled: false, portfolio_id: null, next_run_time: null, total_asset: 100,
  currency: 'CNY', holdings_count: 0, position_weights: [], connection: { status: 'ready' },
})

const binance = { ...account(9), name: 'Binance 9', trade_channel: 'binance', market: '加密货币', currency: 'USDT' }

test('top bar tools follow the current account and channel', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' })
  const queries: string[] = []
  const fees: string[] = []
  const statementDays: (string | null)[] = []
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path.endsWith('/margin-rates')) {
      const code = url.searchParams.get('symbol') ?? ''
      queries.push(`${path}?${code}`)
      return route.fulfill({ json: {
        instrument_id: code.toLowerCase(), is_relative: code.toLowerCase() === 'rb2701',
        long_ratio: code === 'RB2701' ? .13 : .125, short_ratio: .14, long_per_lot: 0, short_per_lot: 2.5,
      } })
    }
    if (path.endsWith('/commission-rates')) {
      const code = url.searchParams.get('symbol')
      fees.push(`${path}?${code}`)
      return route.fulfill({ json: {
        instrument_id: code, open_ratio: .0001, open_per_lot: 1.5, close_ratio: 0,
        close_per_lot: 2, close_today_ratio: 0, close_today_per_lot: 0,
      } })
    }
    if (path.endsWith('/settlement-statement')) {
      statementDays.push(url.searchParams.get('trading_day'))
      return route.fulfill({ json: { documents: [{
        trading_day: '20261117', settlement_id: 1, currency: 'CNY', content: '期货结算单\n手续费  ￥20',
      }] } })
    }
    if (path.endsWith('/instrument-catalog/search')) {
      return route.fulfill({ json: { cache_ready: true, data: url.searchParams.get('prefix') === 'rb' ? [{ symbol: 'rb2701', exchange: 'SHFE' }] : [] } })
    }
    if (path.endsWith('/init/status')) return route.fulfill({ json: { configured: true, values: {} } })
    if (path.endsWith('/account/dashboard')) return route.fulfill({ json: { data: [account(7), account(8), binance] } })
    if (path.endsWith('/business/stream')) return route.fulfill({ status: 204 })
    if (path.endsWith('/capabilities/channels') || path.endsWith('/algorithms')) return route.fulfill({ json: [] })
    if (path.endsWith('/target_snapshot')) return route.fulfill({ json: { weights: {}, sizing: null } })
    if (path.endsWith('/rebalance_plan')) return route.fulfill({ json: { rows: [], off_symbol_count: 0 } })
    if (path.endsWith('/next_run_time')) return route.fulfill({ json: { next_run_time: null, next_execution_times: [] } })
    if (/\/account\/[789]$/.test(path)) {
      const id = Number(path.at(-1))
      return route.fulfill({ json: { ...(id === 9 ? binance : account(id)), id, algorithm: null, empty_positions_algorithm: null } })
    }
    return route.fulfill({ json: { data: [], count: 0 } })
  })
  await page.goto('/accounts/8')
  const opener = page.getByRole('button', { name: '工具箱' })
  await opener.click()
  const dialog = page.getByRole('dialog', { name: '工具箱' })
  const handle = dialog.getByRole('group', { name: '移动工具窗' })
  await expect(handle.getByRole('tab', { name: '保证金查询' })).toHaveAttribute('aria-selected', 'true')
  await expect(dialog.getByText('工具箱', { exact: true })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: 'CTP 账户' })).toHaveCount(0)
  expect(queries).toEqual([])

  const symbol = dialog.getByRole('combobox', { name: '合约' })
  await expect(symbol).toBeFocused()

  const layout = (await dialog.boundingBox())!
  const grip = (await handle.boundingBox())!
  await page.mouse.move(grip.x + 12, grip.y + 12)
  await page.mouse.down()
  await page.mouse.move(grip.x - 108, grip.y + 72, { steps: 5 })
  await page.mouse.up()
  await expect.poll(async () => (await dialog.boundingBox())!.x).toBeCloseTo(layout.x - 120)

  await symbol.fill('rg')
  await dialog.getByRole('button', { name: '查询' }).click()
  await expect(dialog.getByText('没有以 rg 开头的期货合约，请核对代码。')).toBeVisible()
  expect(queries).toEqual([])
  await symbol.fill('rb')
  await expect(dialog.getByRole('option', { name: /rb2701.*上期所/ })).toBeVisible()
  expect(queries).toEqual([])
  await symbol.press('ArrowDown')
  await symbol.press('Enter')
  const table = dialog.getByRole('table', { name: '保证金对照' })
  const first = table.getByRole('row', { name: /rb2701/ })
  await expect(first.getByText('相对交易所', { exact: true })).toBeVisible()
  await expect(first.getByText('12.5%')).toBeVisible()
  await expect(first.getByText('￥2.5/手', { exact: true })).toBeVisible()
  await expect(table.getByText('￥0/手', { exact: true })).toHaveCount(0)
  expect(queries).toEqual(['/api/v1/account/8/margin-rates?rb2701'])

  await symbol.fill('rb2612')
  await symbol.press('Enter')
  await expect(table.getByRole('row', { name: /rb2612/ }).getByText('12.5%')).toBeVisible()
  await expect(first).toBeVisible()

  await symbol.fill('RB2701')
  await symbol.press('Enter')
  await expect(first.getByText('13%', { exact: true })).toBeVisible()
  await expect(table.getByRole('rowheader')).toHaveText([/^rb2701/, /^rb2612/])
  await expect(dialog.getByText('查询于', { exact: false })).toHaveCount(0)
  await dialog.getByRole('button', { name: '关闭工具箱' }).click()
  await opener.click()
  await expect(symbol).toBeFocused()
  await expect(symbol).toHaveValue('RB2701')
  await expect(table.getByRole('rowheader')).toHaveCount(2)
  expect(queries).toHaveLength(3)
  await expect(dialog.getByRole('button', { name: /试算/ })).toHaveCount(0)

  await dialog.getByRole('tab', { name: '手续费查询' }).click()
  await symbol.fill('rb2701')
  await symbol.press('Enter')
  const commission = dialog.getByRole('table', { name: '手续费对照' })
  await expect(commission.getByText('0.01%', { exact: true })).toBeVisible()
  await expect(commission.getByText('￥1.5/手', { exact: true })).toBeVisible()
  await expect(commission.getByText('￥2/手', { exact: true })).toBeVisible()
  await expect(commission.getByText('￥0/手', { exact: true })).toBeVisible()
  expect(fees).toEqual(['/api/v1/account/8/commission-rates?rb2701'])
  await dialog.screenshot({ path: test.info().outputPath('commission.png'), animations: 'disabled' })
  await dialog.getByRole('tab', { name: '保证金查询' }).click()
  await expect(table.getByRole('rowheader')).toHaveCount(2)
  await dialog.screenshot({ path: test.info().outputPath('margin-tool.png'), animations: 'disabled' })
  await page.getByRole('link', { name: 'axile 首页' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(table.getByRole('rowheader')).toHaveCount(2)
  await page.getByText('CTP 7', { exact: true }).click()
  await expect(page).toHaveURL(/\/accounts\/7$/)
  await expect(table).toHaveCount(0)
  await expect(symbol).toBeEmpty()
  await symbol.fill('rb2701')
  await symbol.press('Enter')
  await expect(first.getByText('12.5%', { exact: true })).toBeVisible()
  await expect(dialog.getByText('CTP 7 · 投机', { exact: true })).toHaveCount(0)
  expect(queries.at(-1)).toBe('/api/v1/account/7/margin-rates?rb2701')

  await page.setViewportSize({ width: 390, height: 640 })
  await expect.poll(async () => {
    const box = (await dialog.boundingBox())!
    return box.x >= 8 && box.x + box.width <= 382
  }).toBe(true)
  await handle.focus()
  await handle.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(opener).toBeFocused()

  await page.setViewportSize({ width: 1280, height: 720 })
  await page.getByRole('button', { name: '结算单', exact: true }).click()
  const settlement = page.getByRole('dialog', { name: '结算单', exact: true })
  await settlement.getByLabel('交易日').fill('2026-11-17')
  await settlement.getByRole('button', { name: '查询结算单' }).click()
  await expect(settlement.getByLabel('结算单原文')).toContainText('手续费  ￥20')
  expect(statementDays).toEqual(['2026-11-17'])
  await settlement.screenshot({ path: test.info().outputPath('settlement.png'), animations: 'disabled' })
  await settlement.getByRole('button', { name: '关闭', exact: true }).click()
  await opener.click()
  await page.getByRole('link', { name: 'axile 首页' }).click()
  await page.getByText('Binance 9', { exact: true }).click()
  await expect(page).toHaveURL(/\/accounts\/9$/)
  await expect(opener).toHaveCount(0)
  await expect(page.getByRole('button', { name: '结算单', exact: true })).toHaveCount(0)
  await expect(dialog).toHaveCount(0)
  expect(queries).toHaveLength(4)
})
