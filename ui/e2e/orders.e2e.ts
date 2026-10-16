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
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
  await expect(dialog.getByRole('button', { name: '展开委托 native-1', exact: true })).toBeVisible()
  await expect(dialog.getByRole('group', { name: '委托拉取方式' })).toHaveCount(0)
  await request.post('/api/v1/test/orders/partial')
  await expect(dialog.getByText('部分成交', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.getByText('3 / 1 手', { exact: true }).filter({ visible: true })).toBeVisible()
  expect(streams).toHaveLength(1)
  expect(reads).toHaveLength(0)
  expect((await (await request.get('/api/v1/test/orders/stats')).json()).queries).toBe(0)
})

test('execution end preserves reports and manual reconcile remains explicit', async ({ page, request }) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await page.getByRole('button', { name: '结束执行', exact: true }).click()
  await request.post('/api/v1/test/orders/partial')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  await expect(dialog.getByText('3 / 1 手', { exact: true }).filter({ visible: true })).toBeVisible()
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect.poll(async () => (await (await request.get('/api/v1/test/orders/stats')).json()).queries).toBe(1)
})

test('disconnect retains orders and disables cancellation until recovery', async ({ page, request }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
    await target.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
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
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
  await expect(page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true })).toBeVisible()
  await page.evaluate(async () => {
    const path = '/src/stores/liveOrders.ts'
    const { useLiveOrders } = await import(path)
    const snapshot = useLiveOrders.getState().frames[1]
    const order = snapshot.data[0]
    useLiveOrders.getState().applyRead(1, { ...snapshot, version: (snapshot.version ?? 0) + 1000, complete: false, count: null,
      data: [order, { ...order, order_id: 'uncertain-2', uncertain: true }] })
  })
  await expect(content).toContainText('— 笔')
  await expect(page.getByRole('button', { name: '当日委托与成交', exact: true })).toBeVisible()
  await expect(content.getByText('待确认', { exact: true }).filter({ visible: true })).toHaveCount(1)
  await expect(content.getByRole('button', { name: '撤单 rb2610 native-1' })).toBeDisabled()
  await expect(content.getByRole('button', { name: '撤单 rb2610 uncertain-2' })).toBeDisabled()
  await expect(content).not.toContainText('同步中断')
})

test('account activity tabs expose fills and rejected orders and remember selection after reopening', async ({ page }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(dialog).toContainText('成交时间')
  await dialog.getByRole('button', { name: '记录范围说明', exact: true }).hover()
  await expect(page.getByRole('tooltip')).toHaveText('柜台当前交易日记录 + 本次连接观测')
  await page.mouse.move(0, 0)
  await expect(dialog.locator('[data-swap-current]').getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.locator('[data-swap-current]').getByText('1 手', { exact: true }).filter({ visible: true })).toBeVisible()
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await dialog.getByRole('button', { name: '撤单 / 拒绝', exact: true }).click()
  await expect(dialog.locator('[data-swap-current]').getByText('保证金不足', { exact: true })).toBeVisible()
  await expect(dialog).not.toContainText('本次连接起采集')
  await expect(dialog).not.toContainText('最多显示最近')
  await dialog.getByRole('button', { name: '核对记录', exact: true }).hover()
  await expect(page.getByRole('tooltip')).toContainText('上次核对：')
  await page.screenshot({ path: testInfo.outputPath('account-activity-rejections.png'), animations: 'disabled' })
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await expect(dialog).not.toBeVisible()
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '撤单 / 拒绝', exact: true })).toHaveAttribute('aria-pressed', 'true')
})

test('activity symbol filter isolates records and refresh failure preserves last successful data', async ({ page }, testInfo) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
  await expect(row.getByText('已成交', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(row.locator(':scope > *')).toHaveCount(10)
  await expect(current).not.toContainText('开平未知')
  await expect(current.getByText('买卖 / 开平', { exact: true })).toHaveCount(0)
  await expect(row.locator('time')).toHaveText('2026-10-16 10:26:17')
  await page.screenshot({ path: testInfo.outputPath('stock-orders.png'), animations: 'disabled' })
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('stock-orders-mobile.png'), animations: 'disabled' })
})


for (const channel of ['ctp', 'tq', 'external'] as const) {
  test(`${channel} fills expose channel columns in the wider dialog`, async ({ page }, testInfo) => {
    const extras = {
      ctp: { offset: 'OPEN', trading_day: '20261019', hedge_flag: '3' },
      tq: { offset: 'CLOSETODAY', exchange_trade_id: 'exchange-fill-12345' },
      external: { commission: 0, commission_asset: 'CNY', realized_pnl: -2.5, is_maker: false, position_side: 'SHORT' },
    }[channel]
    const labels = { ctp: ['开平', '交易日', '投机 / 套保'], tq: ['开平'], external: ['手续费', '手续费币种', '已实现盈亏', 'Maker/Taker', '持仓方向'] }[channel]
    const values = { ctp: ['开仓', '2026-10-19', '套保'], tq: ['平今'], external: ['0', 'CNY', '-2.5', 'Taker', '空头'] }[channel]
    const fill = { trade_id: 'channel-fill', order_id: 'channel-order', symbol: channel === 'external' ? 'rb2610' : 'rb2610', direction: 'BUY', offset: null, time: '2026-10-16T21:01:02+08:00', price: 100, volume: 1, value: 100 }
    await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ json: {
      channel, orders: [], ended_orders: [], trades: [{ ...fill, ...extras }, { ...fill, trade_id: 'missing-fields' }], failures: [], scope: '', started_at: fill.time, sampled_at: fill.time, queried_at: fill.time, errors: [], symbols: [fill.symbol], limit: 100, truncated: false,
    } }))
    await page.setViewportSize({ width: 1600, height: 900 })
    await page.goto('/e2e/orders.html')
    await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
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
    if (channel === 'external') await expect(rows.first().locator('.text-down')).toContainText('-2.5')
    await page.screenshot({ path: testInfo.outputPath(`${channel}-trade-columns.png`), animations: 'disabled' })
    await page.setViewportSize({ width: 375, height: 812 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}

for (const scenario of [
  { channel: 'ctp', attributes: { trading_day: '20261019', hedge_flag: '3', time_condition: '3', min_volume: 1 }, headers: ['交易日', '投机 / 套保'], cells: ['2026-10-19', '套保'], detail: '最小成交量' },
  { channel: 'tq', attributes: { time_condition: 'IOC', volume_condition: 'ALL', exchange_order_id: 'exchange-demo', is_online: false }, headers: ['执行条件'], cells: ['立即成交剩余撤销 / 全部数量'], detail: '尚未确认' },
  { channel: 'gm', attributes: { filled_amount: 312, order_business: '200', order_duration: '3' }, headers: ['已成金额', '业务类型'], cells: ['312.00', '融资买入'], detail: '委托时间属性' },
  { channel: 'external', attributes: { native_order_type: 'LIMIT', position_side: 'SHORT', reduce_only: false, time_in_force: 'GTX' }, headers: ['订单类型', '持仓方向', '只减仓'], cells: ['空头', '否'], detail: '只做挂单' },
]) {
  test(`${scenario.channel} orders show verified average and keep secondary fields in details`, async ({ page }) => {
    const time = '2026-10-16T10:00:00+08:00'
    await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ json: {
      channel: scenario.channel, orders: [{ order_id: 'demo-order', symbol: 'demo-symbol', direction: 'BUY', offset: 'OPEN',
        order_type: 'LIMIT', price: 120, volume: 3, filled_volume: 3, remaining_volume: 0, status: '已成交',
        create_time: time, execution_id: null, native_identity: {}, uncertain: false, avg_price: 104,
        avg_price_source: 'trades', attributes: scenario.attributes }], trades: [], ended_orders: [], failures: [],
      scope: '合成委托', started_at: time, sampled_at: time, queried_at: time, errors: [], symbols: ['demo-symbol'], limit: 100, truncated: false,
    } }))
    await page.goto('/e2e/orders.html')
    await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
    await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
    const table = dialog.locator('[data-swap-current] [data-order-table]')
    for (const header of ['成交均价', ...scenario.headers]) await expect(table.getByRole('columnheader', { name: header, exact: true })).toBeVisible()
    const row = table.locator('[data-activity-row]')
    for (const value of ['104', ...scenario.cells]) await expect(row.getByText(value, { exact: true }).filter({ visible: true })).toBeVisible()
    await expect(table.getByText('逐笔成交计算', { exact: true })).not.toBeVisible()
    await table.getByRole('button', { name: '展开委托 demo-order', exact: true }).click()
    await expect(table.getByText('逐笔成交计算', { exact: true })).toBeVisible()
    await expect(table.getByText(scenario.detail, { exact: true })).toBeVisible()
    await page.setViewportSize({ width: 375, height: 812 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}

for (const scenario of [
  { channel: 'ctp', attributes: { exchange_id: 'SHFE', order_sys_id: 'sys-123', order_ref: 'ref-123', event_date: '20261016', event_clock: '21:01:02' }, extras: { offset: 'OPEN', trading_day: '20261019', hedge_flag: '3' }, expected: ['SHFE', 'sys-123', 'ref-123', '2026-10-16', '21:01:02'] },
  { channel: 'tq', attributes: { exchange_id: 'SHFE', tq_symbol: 'SHFE.rb2610' }, extras: { offset: 'CLOSETODAY', exchange_trade_id: 'exchange-fill-12345' }, expected: ['SHFE', 'SHFE.rb2610', 'exchange-fill-12345'] },
  { channel: 'gm', attributes: { cl_ord_id: 'client-123', exchange_order_id: 'counter-123', gm_symbol: 'SHSE.600000' }, extras: { offset: 'OPEN', commission: 0, amount: 100, cost: 100 }, expected: ['client-123', 'counter-123', 'SHSE.600000', '开仓'] },
  { channel: 'external', attributes: { client_order_id: 'client-123', execution_type: 'TRADE', order_status: 'PARTIALLY_FILLED' }, extras: { commission: 0, commission_asset: 'USDT', realized_pnl: 0, is_maker: false, position_side: 'SHORT' }, expected: ['client-123', '成交', '部分成交'] },
]) {
  test(`${scenario.channel} fill details expand, copy both IDs and adapt to narrow screens`, async ({ page }, testInfo) => {
    const time = '2026-10-16T21:01:02+08:00'
    const orderId = 'order-12345678901234567890123456789012345678901234567890'
    const tradeId = 'fill-12345678901234567890123456789012345678901234567890'
    await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ json: {
      channel: scenario.channel, orders: [], ended_orders: [], trades: [{ trade_id: tradeId, order_id: orderId,
        symbol: 'rb2610', direction: 'BUY', offset: null, time, price: 100, volume: 1, value: 100,
        attributes: scenario.attributes, ...scenario.extras }], failures: [], scope: '合成成交记录', started_at: time,
      sampled_at: time, queried_at: time, errors: [], symbols: ['rb2610'], limit: 100, truncated: false,
    } }))
    await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async (value: string) => { Object.assign(window, { copiedTradeDetail: value }) },
    } }))
    await page.setViewportSize({ width: 1600, height: 900 })
    await page.goto('/e2e/orders.html')
    await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
    await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
    const current = dialog.locator('[data-swap-current]')
    await expect(current.getByRole('button', { name: '复制成交编号', exact: true })).not.toBeVisible()
    if (scenario.channel === 'tq') {
      await expect(current.getByRole('columnheader', { name: '交易所成交编号', exact: true })).toHaveCount(0)
      await expect(current.getByText('exchange-fill-12345', { exact: true })).not.toBeVisible()
    }
    await current.getByRole('button', { name: `展开成交 ${tradeId}`, exact: true }).click()
    for (const value of scenario.expected) await expect(current.getByText(value, { exact: true })).toBeVisible()
    await current.getByRole('button', { name: '复制订单编号', exact: true }).click()
    expect(await page.evaluate(() => Reflect.get(window, 'copiedTradeDetail'))).toBe(orderId)
    await current.getByRole('button', { name: '复制成交编号', exact: true }).click()
    expect(await page.evaluate(() => Reflect.get(window, 'copiedTradeDetail'))).toBe(tradeId)
    const details = current.locator('dl')
    const desktopColumns = await details.evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length)
    expect(desktopColumns).toBeGreaterThan(1)
    await page.screenshot({ path: testInfo.outputPath(`${scenario.channel}-trade-details.png`), animations: 'disabled' })
    await page.setViewportSize({ width: 375, height: 812 })
    await expect.poll(async () => details.evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length)).toBe(1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await current.locator('[data-trade-table]').evaluate(table => { table.parentElement!.scrollLeft = table.parentElement!.scrollWidth })
    await expect(current.getByRole('button', { name: '复制成交编号', exact: true })).toBeInViewport()
    await page.screenshot({ path: testInfo.outputPath(`${scenario.channel}-trade-details-mobile.png`), animations: 'disabled' })
    await current.getByRole('button', { name: `收起成交 ${tradeId}`, exact: true }).click()
    await expect(current.getByRole('button', { name: '复制成交编号', exact: true })).not.toBeVisible()
  })
}

for (const scenario of [
  { channel: 'ctp', tab: '最近成交', count: 0 },
  { channel: 'tq', tab: '最近成交', count: 1 },
  { channel: 'gm', tab: '最近委托', count: 80 },
  { channel: 'external', tab: '最近成交', count: 80 },
  { channel: 'tq', tab: '撤单 / 拒绝', count: 1 },
]) {
  test(`activity loading keeps geometry for ${scenario.channel} ${scenario.tab} ${scenario.count} records`, async ({ page }, testInfo) => {
    await page.emulateMedia({ reducedMotion: scenario.channel === 'external' ? 'no-preference' : 'reduce' })
    await page.setViewportSize({ width: 1600, height: 900 })
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    const time = '2026-10-16T10:00:00+08:00'
    const orders = Array.from({ length: scenario.count }, (_, i) => ({ order_id: `loading-order-${i}`, symbol: `DEMO${i}`, direction: 'BUY', offset: 'OPEN', order_type: 'LIMIT',
      price: 100, avg_price: 99, volume: 1, filled_volume: 1, remaining_volume: 0, status: scenario.tab === '撤单 / 拒绝' ? '已拒绝' : '已成交',
      create_time: time, execution_id: null, native_identity: {}, uncertain: false, attributes: {} }))
    const payload = { channel: scenario.channel, orders, ended_orders: scenario.tab === '撤单 / 拒绝' ? orders : [],
      trades: orders.map((order, i) => ({ trade_id: `loading-fill-${i}`, order_id: order.order_id, symbol: order.symbol, direction: 'BUY', offset: 'OPEN', time, price: 99, volume: 1, value: 99 })),
      failures: [], scope: '合成记录范围，用于验证加载占位与真实表格的列宽、表头位置和弹窗尺寸', queried_at: time, started_at: time, sampled_at: time, errors: [], symbols: [], limit: 100, truncated: false }
    await page.route('**/api/v1/account/1/orders/activity?**', async route => { await gate; await route.fulfill({ json: payload }) })
    await page.goto(`/e2e/orders.html?channel=${scenario.channel}`)
    await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
    await dialog.getByRole('button', { name: scenario.tab, exact: true }).click()
    const current = dialog.locator('[data-swap-current]')
    await expect(current.locator('[data-record-placeholder]')).toBeVisible()
    await expect(dialog.locator('[data-swap-outgoing]')).toHaveCount(0)
    const anchors = [dialog, dialog.locator('h2'), dialog.getByRole('button', { name: scenario.tab, exact: true }),
      dialog.getByRole('button', { name: '品种', exact: true }), dialog.getByRole('button', { name: '核对记录', exact: true }), dialog.getByRole('button', { name: '关闭', exact: true }).last()]
    const geometry = () => Promise.all(anchors.map(locator => locator.boundingBox()))
    const before = await geometry()
    const headersBefore = await current.locator('th').evaluateAll(headers => headers.map(header => ({ x: header.getBoundingClientRect().x, y: header.getBoundingClientRect().y, width: header.getBoundingClientRect().width })))
    const heading = scenario.tab === '撤单 / 拒绝' ? null : await current.locator('thead').elementHandle()
    await page.screenshot({ path: testInfo.outputPath('activity-loading.png'), animations: 'disabled' })
    release()
    await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toBeEnabled()
    await expect(current.locator('[data-record-placeholder]')).toHaveCount(0)
    expect(await geometry()).toEqual(before)
    expect(await current.locator('th').evaluateAll(headers => headers.map(header => ({ x: header.getBoundingClientRect().x, y: header.getBoundingClientRect().y, width: header.getBoundingClientRect().width })))).toEqual(headersBefore)
    if (heading) expect(await heading.evaluate(node => node.isConnected)).toBe(true)
    if (scenario.channel === 'external') expect(await current.evaluate(node => node.getAnimations({ subtree: true }).length)).toBe(0)
    if (scenario.count === 0) await expect(current.getByText('暂无成交记录', { exact: true })).toBeVisible()
    else await expect(current.getByText('DEMO0', { exact: true }).first()).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('activity-loaded.png'), animations: 'disabled' })
    if (scenario.count > 5) {
      await dialog.locator('[data-activity-scroll]').evaluate(node => { node.scrollTop = 500 })
      expect(await dialog.locator('[data-activity-scroll]').evaluate(node => node.scrollTop)).toBeGreaterThan(0)
      expect(await geometry()).toEqual(before)
    }
    await page.setViewportSize({ width: 375, height: 812 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await expect(dialog.getByRole('button', { name: '品种', exact: true })).toBeInViewport()
    await expect(dialog.getByRole('button', { name: '关闭', exact: true }).last()).toBeInViewport()
  })
}

test('fast activity reads and background refresh keep the dialog frame and current rows', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
  const before = await dialog.boundingBox()
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-activity-row]')).toBeVisible()
  expect(await dialog.boundingBox()).toEqual(before)
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/account/1/orders/activity?**', async route => { await gate; await route.continue() })
  await dialog.getByRole('button', { name: '核对记录', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-activity-row]')).toBeVisible()
  await expect(dialog.locator('[data-record-placeholder]')).toHaveCount(0)
  expect(await dialog.boundingBox()).toEqual(before)
  release()
  await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toBeEnabled()
})

test('empty records stay centered in the visible area when the table scrolls', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/v1/account/1/orders/activity?**', async route => {
    const response = await route.fetch()
    await route.fulfill({ json: { ...await response.json(), trades: [] } })
  })
  await page.goto('/e2e/orders.html?channel=ctp')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
  await dialog.getByRole('button', { name: '撤单 rb2610 native-1' }).click()
  await expect(dialog.getByText('当前无在途委托', { exact: true })).toBeVisible()
  for (const tab of ['在途委托 0', '最近成交']) {
    await dialog.getByRole('button', { name: tab, exact: true }).click()
    const current = dialog.locator('[data-swap-current]')
    const empty = current.locator('[data-record-empty]')
    await expect(empty).toBeVisible()
    for (const width of [1600, 900, 375]) {
      await page.setViewportSize({ width, height: 900 })
      const scroll = current.locator('[data-record-scroll]')
      for (const end of [false, true]) {
        await scroll.evaluate((node, end) => { node.scrollLeft = end ? node.scrollWidth : 0 }, end)
        const viewport = await scroll.boundingBox()
        const message = await empty.boundingBox()
        expect(viewport).not.toBeNull()
        expect(message).not.toBeNull()
        expect(Math.abs(message!.x + message!.width / 2 - viewport!.x - viewport!.width / 2)).toBeLessThan(1)
        await expect(empty).toBeInViewport()
      }
    }
  }
})

test('activity scope and reconciliation time live in toolbar tooltips', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/v1/account/1/orders/activity?**', async route => {
    const response = await route.fetch()
    await route.fulfill({ json: { ...await response.json(), scope: '渠道实际记录范围，每类最多1000笔', queried_at: '2026-10-16T20:38:38+08:00' } })
  })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  const refresh = dialog.getByRole('button', { name: '核对记录', exact: true })
  await expect(refresh).toBeEnabled()
  await expect(dialog.locator('[data-activity-scope]')).toHaveCount(0)
  const help = dialog.getByRole('button', { name: '记录范围说明', exact: true })
  await help.hover()
  await expect(page.getByRole('tooltip')).toHaveText('渠道实际记录范围，每类最多1000笔')
  await page.mouse.move(0, 0)
  await refresh.focus()
  await expect(page.getByRole('tooltip')).toHaveText('上次核对：2026-10-16 20:38:38')
  await help.click()
  await expect(page.getByRole('tooltip')).toHaveText('渠道实际记录范围，每类最多1000笔')
  await page.setViewportSize({ width: 375, height: 812 })
  await expect(help).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

for (const tab of ['最近成交', '最近委托']) {
  test(`${tab} keeps compact column boundaries through long-value refresh and details`, async ({ page }, testInfo) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.setViewportSize({ width: 1600, height: 900 })
    const time = '2026-10-16T10:00:00+08:00'
    let long = false
    await page.route('**/api/v1/account/1/orders/activity?**', route => {
      const symbol = long ? 'VERY-LONG-SYMBOL-123456789012345678901234567890' : 'XRPUSDT'
      return route.fulfill({ json: { channel: 'external', scope: '合成记录', started_at: time, sampled_at: time, queried_at: time, errors: [], symbols: [symbol], limit: 100, truncated: false,
        trades: [{ trade_id: 'sizing-trade', order_id: 'sizing-order', symbol, direction: 'SELL', time,
          price: long ? 123456789123456 : 1.3501, volume: long ? 123456789012345 : 14.8,
          commission: long ? 12345678912345 : 0.00799259, commission_asset: long ? 'LONG-COMMISSION-ASSET' : 'USDT',
          realized_pnl: long ? -123456789012345 : -0.00148, is_maker: false, position_side: 'BOTH' }],
        orders: [{ order_id: 'sizing-order', symbol, direction: 'SELL', offset: 'OPEN', order_type: 'LIMIT',
          price: long ? 123456789123456 : 1.3501, avg_price: 1.3501, volume: long ? 123456789012345 : 14.8,
          filled_volume: 14.8, remaining_volume: 0, status: '已成交', create_time: time, native_identity: {}, uncertain: false,
          attributes: { native_order_type: long ? 'VERY-LONG-ORDER-TYPE' : 'LIMIT', position_side: 'BOTH', reduce_only: false } }],
        ended_orders: [], failures: [] } })
    })
    await page.goto('/e2e/orders.html?channel=external')
    await page.getByRole('button', { name: '当日委托与成交 · 在途 1', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '当日委托与成交', exact: true })
    await dialog.getByRole('button', { name: tab, exact: true }).click()
    const refresh = dialog.getByRole('button', { name: '核对记录', exact: true })
    await expect(refresh).toBeEnabled()
    const table = dialog.locator('[data-swap-current] table')
    const boundaries = () => table.locator('th').evaluateAll(cells => cells.map(cell => ({ x: cell.getBoundingClientRect().x, width: cell.getBoundingClientRect().width })))
    const before = await boundaries()
    const viewport = await table.locator('..').boundingBox()
    const tableBox = await table.boundingBox()
    expect(tableBox!.width).toBeLessThan(viewport!.width)
    const headers = await table.locator('th').allTextContents()
    expect(before[headers.indexOf('品种')].width).toBeLessThan(120)
    if (tab === '最近成交') {
      expect(before[headers.indexOf('手续费币种')].width).toBeLessThan(110)
      expect(before[headers.indexOf('持仓方向')].width).toBeLessThan(95)
    }
    await page.screenshot({ path: testInfo.outputPath('compact-columns.png'), animations: 'disabled' })
    long = true
    await refresh.click()
    await expect(refresh).toBeEnabled()
    await expect(table.locator('[data-activity-row] .overflow-text-static').filter({ hasText: 'VERY-LONG-SYMBOL' })).toBeVisible()
    expect(await boundaries()).toEqual(before)
    expect(await table.locator('[data-activity-row] td').evaluateAll(cells => cells.every(cell => cell.scrollWidth <= cell.clientWidth))).toBe(true)
    await expect(table.locator('[data-activity-row] [data-overflow="true"]').first()).toBeVisible()
    await table.getByRole('button', { name: tab === '最近成交' ? '展开成交 sizing-trade' : '展开委托 sizing-order', exact: true }).click()
    expect(await boundaries()).toEqual(before)
    await page.setViewportSize({ width: 375, height: 812 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect((await boundaries()).map(cell => cell.width)).toEqual(before.map(cell => cell.width))
  })
}
