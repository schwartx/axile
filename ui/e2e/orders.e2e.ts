import { expect, test } from '@playwright/test'

test.beforeEach(async ({ request }) => {
  await request.post('/api/v1/test/orders/reset')
})

test('idle account receives callbacks over one SSE without REST polling', async ({ page, request }) => {
  const streams: string[] = []
  const reads: string[] = []
  page.on('request', value => {
    if (value.url().includes('/events/stream')) streams.push(value.url())
    if (/\/account\/1\/orders(?:\?|$)/.test(value.url())) reads.push(value.url())
  })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await expect(dialog.getByRole('button', { name: '展开委托 native-1', exact: true })).toBeVisible()
  await expect(dialog.getByRole('group', { name: '委托拉取方式' })).toHaveCount(0)
  await request.post('/api/v1/test/orders/partial')
  await expect(dialog.getByText('部分成交', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.getByText('已成 1 手', { exact: true })).toBeVisible()
  expect(streams).toHaveLength(1)
  expect(reads).toHaveLength(0)
  expect((await (await request.get('/api/v1/test/orders/stats')).json()).queries).toBe(0)
})

test('execution end preserves reports and manual reconcile remains explicit', async ({ page, request }) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await page.getByRole('button', { name: '结束执行', exact: true }).click()
  await request.post('/api/v1/test/orders/partial')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  await expect(dialog.getByText('已成 1 手', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect.poll(async () => (await (await request.get('/api/v1/test/orders/stats')).json()).queries).toBe(1)
})

test('disconnect retains orders and disables cancellation until recovery', async ({ page, request }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  const cancel = dialog.getByRole('button', { name: '撤单 rb2610 native-1' })
  await expect(cancel).toBeEnabled()
  await request.post('/api/v1/test/orders/disconnect')
  await expect(cancel).toBeDisabled()
  await expect(dialog).not.toContainText('待确认')
  await expect(dialog).not.toContainText('当前显示')
  await expect(dialog).not.toContainText('同步中断')
  await page.screenshot({ path: testInfo.outputPath('orders-offline.png') })
  await expect(dialog.getByRole('button', { name: '展开委托 native-1', exact: true })).toBeVisible()
  await request.post('/api/v1/test/orders/recover')
  await expect(cancel).toBeEnabled()
  expect((await (await request.get('/api/v1/test/orders/stats')).json()).queries).toBe(0)
})

test('native terminal callback resolves cancellation in every browser', async ({ page, context, request }) => {
  const second = await context.newPage()
  for (const target of [page, second]) {
    await target.goto('/e2e/orders.html')
    await target.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  }
  await page.getByRole('button', { name: '撤单 rb2610 native-1' }).click()
  for (const target of [page, second]) {
    await expect(target.getByText('当前无在途委托', { exact: true })).toBeVisible()
    await target.getByRole('button', { name: 'rb2610 · 已撤销', exact: true }).click()
    await expect(target.getByText(/订单实际状态：已撤销/)).toBeVisible()
  }
  const stats = await (await request.get('/api/v1/test/orders/stats')).json()
  expect(stats.cancels).toBe(1)
  expect(stats.queries).toBe(0)
})

test('manual reconcile failure stays accessible beside the action', async ({ page }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await page.route('**/api/v1/account/1/orders?refresh=true', route => route.fulfill({ status: 500, json: { detail: 'independent query failure' } }))
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect(dialog.getByRole('button', { name: '核对失败', exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '核对失败', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '核对失败', exact: true })).toContainText('independent query failure')
  await page.getByRole('dialog', { name: '核对失败', exact: true }).getByRole('button', { name: '关闭详情' }).click()
  await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(dialog.getByRole('button', { name: '撤单 rb2610 native-1' })).toBeDisabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('orders-compact-feedback.png') })
})

test('incomplete compact snapshot only marks the uncertain order', async ({ page }) => {
  await page.goto('/e2e/orders.html?compact')
  const content = page.getByRole('region', { name: '品种委托' })
  await expect(page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true })).toBeVisible()
  await page.evaluate(async () => {
    const path = '/src/stores/liveOrders.ts'
    const { useLiveOrders } = await import(path)
    const snapshot = useLiveOrders.getState().frames[1]
    const order = snapshot.data[0]
    useLiveOrders.getState().applyRead(1, { ...snapshot, version: (snapshot.version ?? 0) + 1000, complete: false, count: null,
      data: [order, { ...order, order_id: 'uncertain-2', uncertain: true }] })
  })
  await expect(content).toContainText('— 笔')
  await expect(page.getByRole('button', { name: '委托与成交', exact: true })).toBeVisible()
  await expect(content.getByText('待确认', { exact: true }).filter({ visible: true })).toHaveCount(1)
  await expect(content.getByRole('button', { name: '撤单 rb2610 native-1' })).toBeDisabled()
  await expect(content.getByRole('button', { name: '撤单 rb2610 uncertain-2' })).toBeDisabled()
  await expect(content).not.toContainText('同步中断')
})

test('account activity tabs expose fills and rejected orders and remember selection after reopening', async ({ page }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(dialog).toContainText('成交时间')
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.locator('[data-swap-current]').getByText('1 手', { exact: true }).filter({ visible: true })).toBeVisible()
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await dialog.getByRole('button', { name: '撤单 / 拒绝', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('保证金不足', { exact: true })).toBeVisible()
  await expect(dialog).not.toContainText('本次连接起采集')
  await expect(dialog).not.toContainText('最多显示最近')
  await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toHaveAttribute('title', /^上次核对：/)
  await page.screenshot({ path: testInfo.outputPath('account-activity-rejections.png'), animations: 'disabled' })
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await expect(dialog).not.toBeVisible()
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '撤单 / 拒绝', exact: true })).toHaveAttribute('aria-pressed', 'true')
})

test('activity symbol filter isolates records and refresh failure preserves last successful data', async ({ page }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await dialog.getByRole('button', { name: '品种', exact: true }).click()
  await page.getByRole('option', { name: 'ag2612', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true })).toHaveCount(0)
  await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ status: 502, json: { detail: 'query failed' } }))
  await dialog.getByRole('button', { name: '核对记录', exact: true }).click()
  await expect(dialog).toContainText('以下为上次读取结果')
  await expect(dialog.locator('[data-swap-current]').getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('account-activity-mobile.png'), animations: 'disabled' })
})

test('background and foreground preserve activity tab, filter, rows and scroll during stream recovery', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 450 })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await dialog.getByRole('button', { name: '品种', exact: true }).click()
  await page.getByRole('option', { name: 'ag2612', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toBeEnabled()
  const scrollBefore = await dialog.evaluate(element => {
    const body = element.querySelector<HTMLElement>('.overflow-y-auto')!
    body.scrollTop = 60
    return body.scrollTop
  })
  expect(scrollBefore).toBeGreaterThan(0)

  const visibility = async (hidden: boolean) => page.evaluate(value => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => value })
    document.dispatchEvent(new Event('visibilitychange'))
  }, hidden)
  const rows = dialog.locator('[data-swap-current]').getByText('ag2612', { exact: true }).filter({ visible: true }).last()
  await visibility(true)
  await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toBeDisabled()
  await expect(rows).toBeVisible()
  await expect(dialog).not.toContainText('记录暂不可查询')

  let resume!: () => void
  const gate = new Promise<void>(resolve => { resume = resolve })
  await page.route('**/api/v1/events/stream', async route => { await gate; await route.continue() })
  await visibility(false)
  try {
    await expect(rows).toBeVisible()
    await expect(dialog.getByRole('button', { name: '最近委托', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(dialog.getByRole('button', { name: '品种', exact: true })).toContainText('ag2612')
    expect(await dialog.evaluate(element => element.querySelector<HTMLElement>('.overflow-y-auto')!.scrollTop)).toBe(scrollBefore)
  } finally {
    resume()
  }
  await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toBeEnabled()
  await expect(rows).toBeVisible()
  expect(await dialog.evaluate(element => element.querySelector<HTMLElement>('.overflow-y-auto')!.scrollTop)).toBe(scrollBefore)
})


test('activity picker supports search, immediate selection and manual symbols', async ({ page }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  const picker = dialog.getByRole('button', { name: '品种', exact: true })
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await picker.click()
  await page.getByRole('textbox', { name: '搜索选项' }).fill('AG')
  await expect(page.getByRole('option', { name: 'rb2610', exact: true })).toHaveCount(0)
  await page.getByRole('option', { name: 'ag2612', exact: true }).click()
  await expect(picker).toContainText('ag2612')
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true })).toHaveCount(0)
  await picker.click()
  await page.getByRole('option', { name: '输入其他品种…', exact: true }).click()
  await dialog.getByRole('textbox', { name: '其他品种代码' }).fill('unseen123')
  await dialog.getByRole('button', { name: '筛选', exact: true }).click()
  await expect(picker).toContainText('unseen123')
  await expect(dialog).toContainText('暂无委托记录')
  await picker.click()
  await page.getByRole('option', { name: '全部品种', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await picker.click()
  await page.screenshot({ path: testInfo.outputPath('account-activity-symbol-picker.png'), animations: 'disabled' })
})

test('activity transitions only records, retains controls, and respects reduced motion', async ({ page }) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  const slot = dialog.locator('[data-content-swap]')
  await expect(dialog.locator('[data-swap-current]').getByText('成交时间', { exact: true })).toBeVisible()
  await expect.poll(() => slot.evaluate(element => element.getAnimations({ subtree: true }).length)).toBe(0)
  const picker = await dialog.getByRole('button', { name: '品种', exact: true }).elementHandle()
  const before = await picker!.boundingBox()
  await page.addStyleTag({ content: '.content-swap { --motion-content-swap-ms: 2000; }' })
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(slot.locator('[data-swap-outgoing]')).toHaveAttribute('inert', '')
  const opacities = await slot.evaluate(element => {
    for (const animation of element.getAnimations({ subtree: true })) {
      animation.pause()
      animation.currentTime = 1000
    }
    return [...element.children].map(child => Number(getComputedStyle(child).opacity))
  })
  expect(opacities).toHaveLength(2)
  expect(opacities[0]).toBe(1)
  expect(opacities[1]).toBeGreaterThan(0)
  expect(opacities[1]).toBeLessThan(1)
  expect(await slot.locator('[data-swap-current]').evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe('rgba(0, 0, 0, 0)')
  expect(await picker!.evaluate(element => element === document.querySelector('button[aria-label="品种"]'))).toBe(true)
  expect(await picker!.boundingBox()).toEqual(before)
  await slot.evaluate(element => element.getAnimations({ subtree: true }).forEach(animation => animation.finish()))
  await expect(slot.locator('[data-swap-outgoing]')).toHaveCount(0)
  await expect(slot.getByText('委托时间', { exact: true })).toBeVisible()

  // 连续点选取消上一段交接，不留下旧内容或播放队列。
  await dialog.getByRole('button', { name: '撤单 / 拒绝', exact: true }).click()
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await slot.evaluate(element => element.getAnimations({ subtree: true }).forEach(animation => animation.finish()))
  await expect(slot.locator('[data-swap-outgoing]')).toHaveCount(0)
  await expect(slot.getByText('委托时间', { exact: true })).toBeVisible()

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await dialog.getByRole('button', { name: '撤单 / 拒绝', exact: true }).click()
  await expect(slot.getByText('保证金不足', { exact: true })).toBeVisible()
  await expect(slot.locator('[data-swap-outgoing]')).toHaveCount(0)
  expect(await slot.evaluate(element => element.getAnimations({ subtree: true }).length)).toBe(0)
})

test('stock activity omits trade status, preserves order status and keeps IDs in copyable details', async ({ page }, testInfo) => {
  const orderId = 'f7d3e42a-c908-11f1-8f84-02410e5860af'
  const submitted = '2026-10-16T10:26:17+08:00'
  const filled = '2026-10-16T13:59:46+08:00'
  const stockOrders = [
    { order_id: orderId, symbol: '159920.SZ', volume: 5500, price: 1.425 },
    { order_id: 'f7a68a77-c908-11f1-8f84-02410e5860af', symbol: '518880.SH', volume: 200, price: 8.517 },
  ].map(order => ({ ...order, direction: 'BUY', offset: 'OPEN', order_type: 'LIMIT', filled_volume: order.volume,
    remaining_volume: 0, status: '已成交', create_time: submitted, update_time: filled,
    execution_id: null, native_identity: {}, uncertain: false }))
  await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ json: {
    channel: 'gm', orders: stockOrders, ended_orders: [], trades: stockOrders.map((order, i) => ({
      trade_id: `stock-fill-${i}`, order_id: order.order_id, symbol: order.symbol,
      direction: 'BUY', offset: 'OPEN', time: i === 0 ? filled : '2026-10-16T10:26:21+08:00',
      price: order.price, volume: order.volume, value: order.price * order.volume,
      amount: i === 0 ? 7837.4997377 : 1703.4000396, commission: i === 0 ? 2.5 : 0, cost: i === 0 ? 7840 : 1703.4,
    })), failures: [], scope: 'GM 当前可查询记录', started_at: submitted, sampled_at: filled,
    queried_at: filled, errors: [], symbols: stockOrders.map(order => order.symbol), limit: 100, truncated: false,
  } }))
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: async (value: string) => { Object.assign(window, { copiedActivityId: value }) },
  } }))
  await page.goto('/e2e/orders.html?shares')
  await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  const current = dialog.locator('[data-swap-current]')
  await expect(current.getByText('159920.SZ', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.locator('[data-swap-outgoing]')).toHaveCount(0)
  await expect(current).not.toContainText('开平未知')
  await expect(current.getByText('买卖', { exact: true })).toBeVisible()
  const row = current.locator('[data-activity-row]').first()
  await expect(current.getByText('状态', { exact: true })).toHaveCount(0)
  await expect(current.getByText('已成交', { exact: true })).toHaveCount(0)
  await expect(row.locator(':scope > *')).toHaveCount(9)
  for (const label of ['成交金额', '手续费', '成本']) await expect(current.getByText(label, { exact: true })).toBeVisible()
  await expect(row.getByText('7,837.50', { exact: true }).filter({ visible: true })).toBeVisible()
  expect(await row.locator('[data-numeric]').evaluateAll(cells => cells.every(cell => cell.scrollWidth <= cell.clientWidth))).toBe(true)
  expect((await row.boundingBox())!.height).toBe(64)
  await expect(current.getByText(orderId, { exact: true })).not.toBeVisible()
  await current.getByRole('button', { name: '展开成交 stock-fill-0', exact: true }).click()
  await expect(current.getByText(orderId, { exact: true })).toBeVisible()
  await current.getByRole('button', { name: '复制订单编号', exact: true }).click()
  expect(await page.evaluate(() => Reflect.get(window, 'copiedActivityId'))).toBe(orderId)
  await current.getByRole('button', { name: '收起成交 stock-fill-0', exact: true }).click()
  await expect(current.getByText(orderId, { exact: true })).not.toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('stock-trades.png'), animations: 'disabled' })

  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(dialog.locator('[data-swap-outgoing]')).toHaveCount(0)
  expect((await row.boundingBox())!.height).toBe(64)
  await expect(current.getByText('状态', { exact: true })).toBeVisible()
  await expect(row.getByText('已成交', { exact: true })).toBeVisible()
  await expect(row.locator(':scope > *')).toHaveCount(7)
  await expect(current).not.toContainText('开平未知')
  await expect(current.getByText('买卖 / 开平', { exact: true })).toHaveCount(0)
  await expect(row.locator('time')).toHaveText('2026-10-16 10:26:17')
  await page.screenshot({ path: testInfo.outputPath('stock-orders.png'), animations: 'disabled' })
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('stock-orders-mobile.png'), animations: 'disabled' })
})


for (const channel of ['ctp', 'tq', 'binance'] as const) {
  test(`${channel} fills expose channel columns in the wider dialog`, async ({ page }, testInfo) => {
    const extras = {
      ctp: { offset: 'OPEN', trading_day: '20261019', hedge_flag: '3' },
      tq: { offset: 'CLOSETODAY', exchange_trade_id: 'exchange-fill-12345' },
      binance: { commission: 0, commission_asset: 'BNB', realized_pnl: -2.5, is_maker: false, position_side: 'SHORT' },
    }[channel]
    const labels = { ctp: ['开平', '交易日', '投机 / 套保'], tq: ['开平', '交易所成交编号'], binance: ['手续费', '手续费币种', '已实现盈亏', 'Maker/Taker', '持仓方向'] }[channel]
    const values = { ctp: ['开仓', '2026-10-19', '套保'], tq: ['平今', 'exchange-fill-12345'], binance: ['0', 'BNB', '-2.5', 'Taker', '空头'] }[channel]
    const fill = { trade_id: 'channel-fill', order_id: 'channel-order', symbol: channel === 'binance' ? 'BTCUSDT' : 'rb2610', direction: 'BUY', offset: null, time: '2026-10-16T21:01:02+08:00', price: 100, volume: 1, value: 100 }
    await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ json: {
      channel, orders: [], ended_orders: [], trades: [{ ...fill, ...extras }, { ...fill, trade_id: 'missing-fields' }], failures: [], scope: '', started_at: fill.time, sampled_at: fill.time, queried_at: fill.time, errors: [], symbols: [fill.symbol], limit: 100, truncated: false,
    } }))
    await page.setViewportSize({ width: 1600, height: 900 })
    await page.goto('/e2e/orders.html')
    await page.getByRole('button', { name: '委托与成交 · 在途 1', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '委托与成交', exact: true })
    await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
    const current = dialog.locator('[data-swap-current]')
    await expect(current.locator('[data-activity-row]')).toHaveCount(2)
    await expect(dialog.locator('[data-swap-outgoing]')).toHaveCount(0)
    expect((await dialog.boundingBox())!.width).toBe(1440)
    for (const label of labels) await expect(current.getByText(label, { exact: true })).toBeVisible()
    const rows = current.locator('[data-activity-row]')
    for (const value of values) await expect(rows.first().getByText(value, { exact: true }).filter({ visible: true })).toBeVisible()
    await expect(rows.first().locator(':scope > *')).toHaveCount(6 + labels.length)
    await expect(rows.last().locator(':scope > *')).toHaveCount(6 + labels.length)
    await expect(current.getByText('状态', { exact: true })).toHaveCount(0)
    if (channel === 'binance') await expect(rows.first().locator('.text-down')).toContainText('-2.5')
    await page.screenshot({ path: testInfo.outputPath(`${channel}-trade-columns.png`), animations: 'disabled' })
    await page.setViewportSize({ width: 375, height: 812 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}
