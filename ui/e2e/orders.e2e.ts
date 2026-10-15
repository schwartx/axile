import { expect, test } from '@playwright/test'

const order = { order_id: 'native-1', symbol: 'rb2610', direction: 'BUY', offset: '3', order_type: 'LIMIT',
  price: 3500, volume: 3, filled_volume: 1, remaining_volume: 2, status: '部分成交',
  create_time: '2026-10-15T10:04:00', execution_id: 'old-execution', native_identity: { order_ref: '12', session_id: '7' }, uncertain: false }
const snapshot = { data: [order], count: 1, complete: true, sampled_at: '2026-10-15T10:05:00', cancellations: [] }

test('account summary survives execution completion; order detail and keyboard dismissal', async ({ page }, testInfo) => {
  await page.route('**/api/v1/account/1/orders**', route => route.fulfill({ json: snapshot }))
  await page.goto('/e2e/orders.html')
  const entry = page.getByRole('button', { name: /当前委托 · 1/ })
  await expect(entry).toBeVisible()
  await page.getByRole('button', { name: '结束执行' }).click()
  await expect(entry).toBeVisible()
  await entry.click()
  const dialog = page.getByRole('dialog', { name: '当前委托' })
  await expect(dialog.getByText('部分成交', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.getByText('平今', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '展开委托 native-1' }).click()
  await expect(dialog.getByText('old-execution', { exact: true })).toBeVisible()
  await expect(dialog.getByText('session_id', { exact: true })).toBeVisible()
  await expect(dialog.getByText('2 手', { exact: true })).toBeVisible()
  await dialog.locator('[id^="current-order-native-1"]').evaluate(async element => {
    await Promise.all(element.getAnimations().map(animation => animation.finished))
  })
  await page.screenshot({ path: testInfo.outputPath('current-orders-dialog.png') })
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(entry).toBeFocused()
})

test('one cancel request waits for actual terminal and preserves partial fill evidence', async ({ page }) => {
  let mode: 'active' | 'submitted' | 'terminal' = 'active'
  let posts = 0
  let requestId = ''
  const receipt = () => ({ request_id: requestId, order_id: order.order_id, symbol: order.symbol,
    state: mode === 'terminal' ? 'terminal' : 'submitted', message: mode === 'terminal' ? '订单实际状态：已撤销' : '撤单请求已提交，等待实际回报',
    requested_at: '2026-10-15T10:06:00', order: mode === 'terminal' ? { ...order, status: '已撤销' } : order })
  await page.route('**/api/v1/account/1/orders**', async route => {
    if (route.request().method() === 'POST') {
      posts++
      requestId = route.request().postDataJSON().request_id
      mode = 'submitted'
      await route.fulfill({ status: 202, json: receipt() })
    } else await route.fulfill({ json: { ...snapshot, data: mode === 'terminal' ? [] : [order],
      count: mode === 'terminal' ? 0 : 1, cancellations: mode === 'active' ? [] : [receipt()] } })
  })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: /当前委托 · 1/ }).click()
  const dialog = page.getByRole('dialog', { name: '当前委托' })
  const cancel = dialog.getByRole('button', { name: '撤单 rb2610 native-1' })
  await cancel.click()
  await expect(cancel).toBeDisabled()
  await expect(dialog.getByText('撤单确认中', { exact: true }).filter({ visible: true })).toBeVisible()
  expect(posts).toBe(1)
  mode = 'terminal'
  await dialog.getByRole('button', { name: '刷新当前委托' }).click()
  await expect(dialog.getByText('当前无未完成委托')).toBeVisible()
  await expect(dialog.getByText(/订单实际状态：已撤销 · 已成 1 \/ 3/)).toBeVisible()
  expect(posts).toBe(1)
})

test('query failure retains rows and disables cancel; narrow dialog stays on screen', async ({ page }) => {
  let failed = false
  await page.route('**/api/v1/account/1/orders**', route => route.fulfill(failed ? { status: 502, json: { detail: '柜台查询不可用' } } : { json: snapshot }))
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: /当前委托 · 1/ }).click()
  const dialog = page.getByRole('dialog', { name: '当前委托' })
  failed = true
  await dialog.getByRole('button', { name: '刷新当前委托' }).click()
  await expect(dialog.getByText('当前显示最后一次查询结果，请刷新核对。')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '撤单 rb2610 native-1' })).toBeDisabled()
  await expect(dialog.getByText('当前无未完成委托')).toHaveCount(0)
  const bounds = await dialog.boundingBox()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(375)
})

test('first query failure is unknown; recovery to empty is explicit', async ({ page }) => {
  let failed = true
  await page.route('**/api/v1/account/1/orders**', route => route.fulfill(failed ? { status: 502, json: { detail: '不可用' } } : { json: { ...snapshot, data: [], count: 0 } }))
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: /当前委托 · 待确认/ }).click()
  const dialog = page.getByRole('dialog', { name: '当前委托' })
  await expect(dialog.getByText('当前无未完成委托')).toHaveCount(0)
  failed = false
  await dialog.getByRole('button', { name: '刷新当前委托' }).click()
  await expect(dialog.getByText('当前无未完成委托')).toBeVisible()
})
