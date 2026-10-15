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
  await page.getByRole('button', { name: '在途委托 · 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
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
  await page.getByRole('button', { name: '在途委托 · 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await page.getByRole('button', { name: '结束执行', exact: true }).click()
  await request.post('/api/v1/test/orders/partial')
  await page.getByRole('button', { name: '在途委托 · 1', exact: true }).click()
  await expect(dialog.getByText('已成 1 手', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '核对在途委托' }).click()
  await expect.poll(async () => (await (await request.get('/api/v1/test/orders/stats')).json()).queries).toBe(1)
})

test('disconnect retains orders and disables cancellation until recovery', async ({ page, request }) => {
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '在途委托 · 1', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  const cancel = dialog.getByRole('button', { name: '撤单 rb2610 native-1' })
  await expect(cancel).toBeEnabled()
  await request.post('/api/v1/test/orders/disconnect')
  await expect(cancel).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '展开委托 native-1', exact: true })).toBeVisible()
  await request.post('/api/v1/test/orders/recover')
  await expect(cancel).toBeEnabled()
  expect((await (await request.get('/api/v1/test/orders/stats')).json()).queries).toBe(0)
})

test('native terminal callback resolves cancellation in every browser', async ({ page, context, request }) => {
  const second = await context.newPage()
  for (const target of [page, second]) {
    await target.goto('/e2e/orders.html')
    await target.getByRole('button', { name: '在途委托 · 1', exact: true }).click()
  }
  await page.getByRole('button', { name: '撤单 rb2610 native-1' }).click()
  for (const target of [page, second]) {
    await expect(target.getByText('当前无未完成委托', { exact: true })).toBeVisible()
    await expect(target.getByText(/订单实际状态：已撤销/)).toBeVisible()
  }
  const stats = await (await request.get('/api/v1/test/orders/stats')).json()
  expect(stats.cancels).toBe(1)
  expect(stats.queries).toBe(0)
})
