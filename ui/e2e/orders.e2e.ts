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
  await expect(dialog.getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.getByText('1 手', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(dialog.getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await dialog.getByRole('button', { name: '撤单 / 拒绝', exact: true }).click()
  await expect(dialog.getByText('保证金不足', { exact: true })).toBeVisible()
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
  await expect(dialog.getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await dialog.getByRole('button', { name: '品种', exact: true }).click()
  await page.getByRole('option', { name: 'ag2612', exact: true }).click()
  await expect(dialog.getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await expect(dialog.getByText('rb2610', { exact: true })).toHaveCount(0)
  await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ status: 502, json: { detail: 'query failed' } }))
  await dialog.getByRole('button', { name: '核对记录', exact: true }).click()
  await expect(dialog).toContainText('以下为上次读取结果')
  await expect(dialog.getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
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
  await expect(dialog.getByText('ag2612', { exact: true }).filter({ visible: true }).last()).toBeVisible()
  await dialog.getByRole('button', { name: '品种', exact: true }).click()
  await page.getByRole('option', { name: 'ag2612', exact: true }).click()
  await expect(dialog.getByText('rb2610', { exact: true })).toHaveCount(0)
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
  const rows = dialog.getByText('ag2612', { exact: true }).filter({ visible: true }).last()
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
  await expect(dialog.getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await picker.click()
  await page.getByRole('textbox', { name: '搜索选项' }).fill('AG')
  await expect(page.getByRole('option', { name: 'rb2610', exact: true })).toHaveCount(0)
  await page.getByRole('option', { name: 'ag2612', exact: true }).click()
  await expect(picker).toContainText('ag2612')
  await expect(dialog.getByText('rb2610', { exact: true })).toHaveCount(0)
  await picker.click()
  await page.getByRole('option', { name: '输入其他品种…', exact: true }).click()
  await dialog.getByRole('textbox', { name: '其他品种代码' }).fill('unseen123')
  await dialog.getByRole('button', { name: '筛选', exact: true }).click()
  await expect(picker).toContainText('unseen123')
  await expect(dialog).toContainText('暂无委托记录')
  await picker.click()
  await page.getByRole('option', { name: '全部品种', exact: true }).click()
  await expect(dialog.getByText('rb2610', { exact: true }).filter({ visible: true })).toBeVisible()
  await picker.click()
  await page.screenshot({ path: testInfo.outputPath('account-activity-symbol-picker.png'), animations: 'disabled' })
})
