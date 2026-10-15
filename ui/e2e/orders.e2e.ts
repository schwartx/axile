import { expect, test } from '@playwright/test'

const order = { order_id: 'native-1', symbol: 'rb2610', direction: 'BUY', offset: '3', order_type: 'LIMIT',
  price: 3500, volume: 3, filled_volume: 1, remaining_volume: 2, status: '部分成交',
  create_time: '2026-10-15T10:04:00', execution_id: 'old-execution', native_identity: { order_ref: '12', session_id: '7' }, uncertain: false }
const snapshot = { data: [order], count: 1, complete: true, sampled_at: '2026-10-15T10:05:00', cancellations: [] }

test('account entry survives execution completion; order detail and keyboard dismissal', async ({ page }, testInfo) => {
  await page.route('**/api/v1/account/1/orders**', route => route.fulfill({ json: snapshot }))
  await page.goto('/e2e/orders.html')
  const entry = page.getByRole('button', { name: '查看在途委托', exact: true })
  await expect(entry).toBeVisible()
  await page.getByRole('button', { name: '结束执行' }).click()
  await expect(entry).toBeVisible()
  await entry.click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
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
  await page.getByRole('button', { name: '查看在途委托', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  const cancel = dialog.getByRole('button', { name: '撤单 rb2610 native-1' })
  await cancel.click()
  await expect(cancel).toBeDisabled()
  await expect(dialog.getByText('撤单确认中', { exact: true }).filter({ visible: true })).toBeVisible()
  expect(posts).toBe(1)
  mode = 'terminal'
  await dialog.getByRole('button', { name: '刷新在途委托' }).click()
  await expect(dialog.getByText('当前无未完成委托')).toBeVisible()
  await expect(dialog.getByText(/订单实际状态：已撤销 · 已成 1 \/ 3/)).toBeVisible()
  expect(posts).toBe(1)
})

for (const status of ['已撤销', '已成交']) {
  test(`lost duplicate cancel response converges to another client's ${status} and survives receipt eviction`, async ({ page }) => {
    let mode: 'active' | 'unknown' | 'terminal' | 'evicted' = 'active'
    let posts = 0
    let localRequestId = ''
    const actualOrder = { ...order, status, filled_volume: status === '已成交' ? 3 : 1, remaining_volume: status === '已成交' ? 0 : 2 }
    const serverReceipt = { request_id: 'other-client-request', order_id: order.order_id, symbol: order.symbol,
      state: 'terminal', message: `订单实际状态：${status}`, requested_at: '2000-10-15T10:00:00Z', order: actualOrder }
    await page.route('**/api/v1/account/1/orders**', async route => {
      if (route.request().method() === 'POST') {
        posts++
        localRequestId = route.request().postDataJSON().request_id
        mode = 'unknown'
        await route.abort('failed')
        return
      }
      const ended = mode === 'terminal' || mode === 'evicted'
      const cancellations = mode === 'terminal' ? [serverReceipt]
        : mode === 'unknown' ? [{ ...serverReceipt, state: 'submitted', message: '等待实际回报', order }] : []
      await route.fulfill({ json: { ...snapshot, data: ended ? [] : [order], count: ended ? 0 : 1, cancellations } })
    })
    await page.goto('/e2e/orders.html')
    await page.getByRole('button', { name: '查看在途委托', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
    const cancel = dialog.getByRole('button', { name: '撤单 rb2610 native-1' })
    await cancel.click()
    await expect(dialog.getByText('状态待确认', { exact: true }).filter({ visible: true })).toBeVisible()
    await expect(cancel).toBeDisabled()
    expect(localRequestId).not.toBe(serverReceipt.request_id)
    mode = 'terminal'
    await dialog.getByRole('button', { name: '刷新在途委托' }).click()
    const feedback = dialog.getByText(`rb2610 · 订单实际状态：${status} · 已成 ${actualOrder.filled_volume} / 3`, { exact: true })
    await expect(feedback).toBeVisible()
    await expect(dialog.getByText('当前无未完成委托', { exact: true })).toBeVisible()
    await expect(dialog.getByText(/撤单结果待确认/)).toHaveCount(0)
    mode = 'evicted'
    await dialog.getByRole('button', { name: '刷新在途委托' }).click()
    await expect(feedback).toBeVisible()
    await expect(dialog.getByText(/撤单结果待确认/)).toHaveCount(0)
    expect(posts).toBe(1)
  })
}

for (const lateResponse of ['lost', 'submitted']) {
  test(`terminal survives a late ${lateResponse} POST response after query history eviction`, async ({ page }) => {
    let mode: 'active' | 'terminal' | 'evicted' = 'active'
    let posts = 0
    let reads = 0
    let releasePost!: () => void
    const postResponse = new Promise<void>(resolve => { releasePost = resolve })
    const serverReceipt = { request_id: 'other-client-request', order_id: order.order_id, symbol: order.symbol,
      state: 'terminal', message: '订单实际状态：已撤销', requested_at: '2000-10-15T10:00:00Z', order: { ...order, status: '已撤销' } }
    await page.route('**/api/v1/account/1/orders**', async route => {
      if (route.request().method() === 'POST') {
        posts++
        await postResponse
        if (lateResponse === 'lost') await route.abort('failed')
        else await route.fulfill({ status: 202, json: { ...serverReceipt, state: 'submitted', message: '等待实际回报', order } })
        return
      }
      reads++
      await route.fulfill({ json: { ...snapshot, data: mode === 'active' ? [order] : [], count: mode === 'active' ? 1 : 0,
        cancellations: mode === 'terminal' ? [serverReceipt] : [] } })
    })
    await page.goto('/e2e/orders.html')
    await page.getByRole('button', { name: '查看在途委托', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
    await dialog.getByRole('button', { name: '撤单 rb2610 native-1' }).click()
    await expect.poll(() => posts).toBe(1)
    await expect(dialog.getByText('正在提交撤单', { exact: true }).filter({ visible: true })).toBeVisible()
    mode = 'terminal'
    await dialog.getByRole('button', { name: '刷新在途委托' }).click()
    const feedback = dialog.getByText('rb2610 · 订单实际状态：已撤销 · 已成 1 / 3', { exact: true })
    await expect(feedback).toBeVisible()
    mode = 'evicted'
    await dialog.getByRole('button', { name: '刷新在途委托' }).click()
    await expect(dialog.getByRole('button', { name: '刷新在途委托' })).toBeEnabled()
    const beforeResponse = reads
    releasePost()
    await expect.poll(() => reads).toBe(beforeResponse + 1)
    await expect(dialog.getByRole('button', { name: '刷新在途委托' })).toBeEnabled()
    await expect(feedback).toBeVisible()
    await expect(dialog.getByText(/撤单结果待确认/)).toHaveCount(0)
    expect(posts).toBe(1)
  })
}

test('terminal POST evidence overrides an uncertain row when the follow-up query fails', async ({ page }) => {
  let cancelled = false
  await page.route('**/api/v1/account/1/orders**', async route => {
    if (route.request().method() === 'POST') {
      cancelled = true
      const request = route.request().postDataJSON()
      await route.fulfill({ status: 202, json: { ...request, state: 'terminal', message: '订单实际状态：已成交',
        requested_at: '2000-10-15T10:00:00Z', order: { ...order, status: '已成交', filled_volume: 3, remaining_volume: 0 } } })
    } else if (cancelled) await route.fulfill({ status: 502, json: { detail: '渠道查询暂不可用' } })
    else await route.fulfill({ json: snapshot })
  })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '查看在途委托', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  await dialog.getByRole('button', { name: '撤单 rb2610 native-1' }).click()
  await expect(dialog.getByText('当前显示最后一次查询结果，请刷新核对。')).toBeVisible()
  await expect(dialog.getByText('已成交', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(dialog.getByText('已成 3 手', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '撤单 rb2610 native-1' })).toBeDisabled()
  await expect(dialog.getByText(/订单实际状态：已成交 · 已成 3 \/ 3/)).toBeVisible()
})

test('query failure retains rows and disables cancel; narrow dialog stays on screen', async ({ page }) => {
  let failed = false
  await page.route('**/api/v1/account/1/orders**', route => route.fulfill(failed ? { status: 502, json: { detail: '柜台查询不可用' } } : { json: snapshot }))
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/e2e/orders.html')
  await page.getByRole('button', { name: '查看在途委托', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  failed = true
  await dialog.getByRole('button', { name: '刷新在途委托' }).click()
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
  await page.getByRole('button', { name: '查看在途委托', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  await expect(dialog.getByText('当前无未完成委托')).toHaveCount(0)
  failed = false
  await dialog.getByRole('button', { name: '刷新在途委托' }).click()
  await expect(dialog.getByText('当前无未完成委托')).toBeVisible()
})

test('manual is the default: no background or recovery reads, one read per explicit action', async ({ page }) => {
  await page.clock.install()
  let reads = 0
  await page.route('**/api/v1/account/1/orders**', route => { reads++; return route.fulfill({ json: snapshot }) })
  await page.goto('/e2e/orders.html')
  const entry = page.getByRole('button', { name: '查看在途委托', exact: true })
  await expect(entry).toBeVisible()
  await page.clock.runFor(16000)
  await page.getByRole('button', { name: '恢复页面读取' }).click()
  expect(reads).toBe(0)
  await entry.click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  await expect(dialog.getByRole('button', { name: '手动拉取' })).toHaveAttribute('aria-pressed', 'true')
  await expect(dialog.getByText('部分成交', { exact: true }).filter({ visible: true })).toBeVisible()
  expect(reads).toBe(1)
  await page.clock.runFor(16000)
  await page.getByRole('button', { name: '恢复页面读取' }).evaluate((node: HTMLButtonElement) => node.click())
  expect(reads).toBe(1)
  await dialog.getByRole('button', { name: '刷新在途委托' }).click()
  await expect.poll(() => reads).toBe(2)
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await page.clock.runFor(16000)
  expect(reads).toBe(2)
  await entry.click()
  await expect.poll(() => reads).toBe(3)
})

test('automatic reads show the count, use the original intervals, and persist across reloads', async ({ page }) => {
  await page.clock.install()
  let reads = 0
  await page.route('**/api/v1/account/1/orders**', route => { reads++; return route.fulfill({ json: snapshot }) })
  await page.goto('/e2e/orders.html')
  const entry = page.getByRole('button', { name: /^(查看在途委托|在途委托 ·)/ })
  await entry.click()
  const dialog = page.getByRole('dialog', { name: '在途委托', exact: true })
  await expect(dialog.getByText('部分成交', { exact: true }).filter({ visible: true })).toBeVisible()
  await dialog.getByRole('button', { name: '自动拉取' }).click()
  await expect.poll(() => reads).toBe(2)
  await expect(entry).toContainText('在途委托 · 1')
  await page.clock.runFor(3100)
  await expect.poll(() => reads).toBe(3)
  await dialog.getByRole('button', { name: '手动拉取' }).click()
  await expect(entry).toContainText('查看在途委托')
  await page.clock.runFor(10000)
  expect(reads).toBe(3)
  await dialog.getByRole('button', { name: '自动拉取' }).click()
  await expect.poll(() => reads).toBe(4)
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect.poll(() => reads).toBe(5)
  await expect(entry).toContainText('在途委托 · 1')
  await page.clock.runFor(15100)
  await expect.poll(() => reads).toBe(6)
  await page.reload()
  await expect(entry).toBeVisible()
  await expect(entry).toContainText('在途委托 · 1')
  await expect.poll(() => reads).toBe(7)
  await page.clock.runFor(15100)
  await expect.poll(() => reads).toBe(8)
  await entry.click()
  await expect(dialog.getByRole('button', { name: '自动拉取' })).toHaveAttribute('aria-pressed', 'true')
  await expect.poll(() => reads).toBe(9)
  await page.clock.runFor(3100)
  await expect.poll(() => reads).toBe(10)
})
