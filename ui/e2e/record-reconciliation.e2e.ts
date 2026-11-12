import { expect, test, type Page } from '@playwright/test'

test('record reconciliation refreshes overview positions and survives a page reload', async ({ page }) => {
  let volume = 1
  let reconciles = 0
  const snapshot = () => ({ id: volume, account_id: 1, source: 'manual', created_at: '2026-11-11T10:00:00Z', assets: {
    total_asset: 1000, positions: [{ symbol: 'rb2610', direction: '多头', volume, available_volume: volume, market_value: 100 }],
  } })
  await page.route('**/api/v1/account/1/asset_snapshots*', route => route.fulfill({ json: { data: [snapshot()], count: 1 } }))
  await page.route('**/api/v1/account/1/target_snapshot', route => route.fulfill({ json: { weights: {}, calculated_at: null } }))
  await page.route('**/api/v1/account/1/orders/activity?**', route => {
    const refresh = new URL(route.request().url()).searchParams.get('refresh') === 'true'
    if (refresh) { reconciles++; volume = 3 }
    return route.fulfill({ json: { orders: [], trades: [], failures: [], errors: [], symbols: [], limit: 100, truncated: false,
      scope: '当前连接', started_at: 'now', sampled_at: 'now', queried_at: 'now',
      asset_refresh: refresh ? { status: 'updated', snapshot_id: volume } : null,
    } })
  })
  await page.goto('/e2e/holdings.html')
  const overview = page.getByRole('table', { name: '当前持仓' })
  await expect(overview).toContainText('1/1')
  await page.getByRole('button', { name: '持仓、委托与成交', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(overview).toContainText('3/3')
  expect(reconciles).toBe(1)
  await dialog.getByRole('button', { name: '最近委托', exact: true }).click()
  await expect(dialog).toContainText('暂无委托记录')
  expect(reconciles).toBe(1)
  await dialog.getByRole('button', { name: '核对记录', exact: true }).click()
  await expect.poll(() => reconciles).toBe(2)
  await page.reload()
  await expect(overview).toContainText('3/3')
})

for (const status of ['failed', 'pending']) test(`record ${status} preserves positions and displays the separate asset result`, async ({ page }) => {
  let version = 1
  await mockOverview(page, () => version)
  await page.route('**/api/v1/account/1/assets/refresh', route => {
    version = 3
    return route.fulfill({ json: { id: version } })
  })
  await page.route('**/api/v1/account/1/orders/activity?**', route => route.fulfill({ json: {
    orders: [], trades: [], failures: [], errors: [], symbols: [], limit: 100, truncated: false,
    scope: '当前连接', started_at: 'now', sampled_at: 'now', queried_at: 'now',
    asset_refresh: { status, message: status === 'failed' ? '持仓核对失败，当前显示上次资产快照' : '账户正在执行或更新，持仓核对暂缓' },
  } }))
  await page.goto('/e2e/holdings.html')
  await page.getByRole('button', { name: /持仓、委托与成交/ }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('持仓待核对')
  await expect(dialog).toContainText('暂无成交记录')
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-position-table] [data-activity-row]')).toHaveCount(1)
  await dialog.getByRole('button', { name: '核对持仓', exact: true }).click()
  await expect(dialog.getByRole('status')).toHaveCount(0)
})

const reconciliationId = 'a'.repeat(32)
const records = { orders: [], trades: [], failures: [], errors: [], symbols: [], limit: 100, scope: '当前连接', sampled_at: 'now' }
async function mockOverview(page: Page, version: () => number | null) {
  await page.route('**/api/v1/account/1/asset_snapshots*', route => {
    const id = version()
    if (id == null) return route.fulfill({ status: 502, json: { detail: 'read failed' } })
    return route.fulfill({ json: { data: [{ id, account_id: 1, source: 'manual', created_at: '2026-11-11T10:00:00Z',
      assets: { total_asset: 1000, positions: [{ symbol: 'rb2610', direction: '多头', volume: id, available_volume: id, market_value: 100 }] },
    }], count: 1 } })
  })
  await page.route('**/api/v1/account/1/target_snapshot', route => route.fulfill({ json: { weights: {}, calculated_at: null } }))
}
const readRecovery = (page: Page) => page.evaluate(async () => { const { notifyReadRecovery } = await import('/src/lib/readRecovery.ts'); await notifyReadRecovery() })

for (const succeeds of [true, false]) test(`position reconciliation settles after a session change (${succeeds})`, async ({ page }) => {
  let version = 1
  let reading = false
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  await mockOverview(page, () => version)
  await page.route('**/api/v1/account/1/assets/refresh', async route => {
    reading = true
    await gate
    if (!succeeds) return route.fulfill({ status: 502, json: { detail: 'query failed' } })
    version = 3
    return route.fulfill({ json: { id: version } })
  })
  await page.goto('/e2e/holdings.html')
  const overview = page.locator('section').first()
  await expect(overview.getByRole('table')).toContainText('1/1')
  await page.getByRole('button', { name: '持仓、委托与成交', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await dialog.getByRole('button', { name: '核对持仓', exact: true }).click()
  await expect.poll(() => reading).toBe(true)
  await expect(overview.getByRole('status')).toContainText('正在核对持仓')
  await page.evaluate(async () => {
    const { useLiveOrders } = await import('/src/stores/liveOrders.ts')
    useLiveOrders.getState().applyRead(1, { data: [], count: 0, complete: true, cancellations: [], sampled_at: 'now', instance_id: 'replacement' })
  })
  await expect(dialog.getByRole('button', { name: '核对持仓', exact: true })).toBeEnabled()
  release()
  if (succeeds) {
    await expect(overview.getByRole('table')).toContainText('3/3')
    await expect(overview.getByRole('status')).toHaveCount(0)
  } else {
    await expect(overview.getByRole('status')).toContainText('持仓待核对')
    await overview.getByRole('img', { name: '持仓待核对', exact: true }).focus()
    await expect(page.getByRole('tooltip')).toContainText('本次持仓核对未完成')
  }
})

for (const terminal of ['updated', 'failed'] as const) test(`background task ${terminal} is read by identity after dialog closes`, async ({ page }) => {
  let version = 1
  let status: 'pending' | 'updated' | 'failed' = 'pending'
  let reconciles = 0
  await mockOverview(page, () => version)
  await page.route('**/api/v1/account/1/orders?reconciliation_id=*', route => route.fulfill({ json: {
    data: [], count: 0, complete: true, cancellations: [], sampled_at: 'now',
    asset_refresh: { status, reconciliation_id: reconciliationId, snapshot_id: status === 'updated' ? 3 : null,
      message: status === 'failed' ? '本次任务核对失败' : '核对仍在运行' },
  } }))
  await page.route('**/api/v1/account/1/orders/activity?**', route => {
    if (new URL(route.request().url()).searchParams.get('refresh') === 'true') reconciles++
    return route.fulfill({ json: { ...records, asset_refresh: { status: 'pending', reconciliation_id: reconciliationId } } })
  })
  await page.goto('/e2e/holdings.html')
  const overview = page.locator('section').first()
  await expect(overview.getByRole('table')).toContainText('1/1')
  await page.getByRole('button', { name: '持仓、委托与成交', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('持仓待核对')
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  // 旧查询后来写入更大的编号，但本次任务仍未完成。
  version = 2
  await readRecovery(page)
  await expect(overview.getByRole('table')).toContainText('2/2')
  await expect(overview.getByRole('status')).toContainText('持仓待核对')
  status = terminal
  if (terminal === 'updated') version = 3
  await readRecovery(page)
  if (terminal === 'updated') {
    await expect(overview.getByRole('table')).toContainText('3/3')
    await expect(overview.getByRole('status')).toHaveCount(0)
  } else {
    await expect(overview.getByRole('status')).toContainText('持仓待核对')
    await expect(overview.getByRole('table')).toContainText('2/2')
    await overview.getByRole('img', { name: '持仓待核对', exact: true }).focus()
    await expect(page.getByRole('tooltip')).toContainText('本次任务核对失败')
  }
  expect(reconciles).toBe(1)
})

test('closing during record query still receives the task identity and its eventual result', async ({ page }) => {
  let version = 1
  let reading = false
  let complete = false
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  await mockOverview(page, () => version)
  await page.route('**/api/v1/account/1/orders?reconciliation_id=*', route => route.fulfill({ json: {
    data: [], count: 0, complete: true, cancellations: [], sampled_at: 'now',
    asset_refresh: { status: complete ? 'updated' : 'pending', reconciliation_id: reconciliationId, snapshot_id: complete ? 3 : null },
  } }))
  await page.route('**/api/v1/account/1/orders/activity?**', async route => {
    reading = true
    await gate
    await route.fulfill({ json: { ...records, asset_refresh: { status: 'pending', reconciliation_id: reconciliationId } } })
  })
  await page.goto('/e2e/holdings.html')
  const overview = page.locator('section').first()
  await expect(overview.getByRole('table')).toContainText('1/1')
  await page.getByRole('button', { name: '持仓、委托与成交', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect.poll(() => reading).toBe(true)
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  release()
  complete = true
  version = 3
  await expect(overview.getByRole('table')).toContainText('3/3')
  await expect(overview.getByRole('status')).toHaveCount(0)
})

test('updated response waits until its snapshot can actually be read', async ({ page }) => {
  let version = 1
  let failRead = false
  await page.route('**/api/v1/account/1/asset_snapshots*', route => failRead
    ? route.fulfill({ status: 502, json: { detail: 'temporary read failure' } })
    : route.fulfill({ json: { data: [{ id: version, account_id: 1, source: 'manual', created_at: '2026-11-11T10:00:00Z',
      assets: { total_asset: 1000, positions: [{ symbol: 'rb2610', direction: '多头', volume: version, available_volume: version, market_value: 100 }] },
    }], count: 1 } }))
  await page.route('**/api/v1/account/1/target_snapshot', route => route.fulfill({ json: { weights: {}, calculated_at: null } }))
  await page.route('**/api/v1/account/1/orders/activity?**', route => {
    failRead = true
    return route.fulfill({ json: { orders: [], trades: [], failures: [], errors: [], symbols: [], limit: 100,
      scope: '当前连接', sampled_at: 'now', asset_refresh: { status: 'updated', snapshot_id: 3 },
    } })
  })
  await page.goto('/e2e/holdings.html')
  const overview = page.locator('section').first()
  await expect(overview.getByRole('table')).toContainText('1/1')
  await page.getByRole('button', { name: '持仓、委托与成交', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(overview.getByRole('status')).toContainText('持仓待核对')
  await expect(overview.getByRole('table')).toContainText('1/1')
  failRead = false
  version = 3
  await page.evaluate(async () => { const { notifyReadRecovery } = await import('/src/lib/readRecovery.ts'); await notifyReadRecovery() })
  await expect(overview.getByRole('table')).toContainText('3/3')
  await expect(overview.getByRole('status')).toHaveCount(0)
})

test('dialog reading the new snapshot cannot acknowledge an overview still showing the old one', async ({ page }) => {
  let version = 1
  let failOverview = false
  let nextModalRead = false
  await mockOverview(page, () => {
    if (nextModalRead) { nextModalRead = false; return 3 }
    return failOverview ? null : version
  })
  await page.route('**/api/v1/account/1/orders/activity?**', route => {
    if (new URL(route.request().url()).searchParams.get('refresh') === 'true') failOverview = true
    return route.fulfill({ json: { ...records, asset_refresh: { status: 'updated', snapshot_id: 3 } } })
  })
  await page.goto('/e2e/holdings.html')
  const overview = page.locator('section').first()
  await expect(overview.getByRole('table')).toContainText('1/1')
  await page.getByRole('button', { name: '持仓、委托与成交', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect(overview.getByRole('status')).toContainText('持仓待核对')
  nextModalRead = true
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-activity-row] td').nth(2)).toContainText('3')
  await expect(overview.getByRole('table')).toContainText('1/1')
  await expect(overview.getByRole('status')).toContainText('持仓待核对')
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  failOverview = false
  version = 3
  await readRecovery(page)
  await expect(overview.getByRole('table')).toContainText('3/3')
  await expect(overview.getByRole('status')).toHaveCount(0)
})

test('a late result from a closed dialog cannot replace a newer reconciliation', async ({ page }) => {
  let version = 1
  let calls = 0
  let firstReturned = false
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  await mockOverview(page, () => version)
  await page.route('**/api/v1/account/1/orders/activity?**', async route => {
    const first = ++calls === 1
    if (first) await gate
    else version = 3
    await route.fulfill({ json: { ...records, asset_refresh: first
      ? { status: 'failed', message: '旧核对失败' } : { status: 'updated', snapshot_id: 3 } } })
    if (first) firstReturned = true
  })
  await page.goto('/e2e/holdings.html')
  const overview = page.locator('section').first()
  await expect(overview.getByRole('table')).toContainText('1/1')
  const open = page.getByRole('button', { name: '持仓、委托与成交', exact: true })
  const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
  await open.click()
  await dialog.getByRole('button', { name: '最近成交', exact: true }).click()
  await expect.poll(() => calls).toBe(1)
  await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
  await open.click()
  await expect(overview.getByRole('table')).toContainText('3/3')
  await expect(overview.getByRole('status')).toHaveCount(0)
  release()
  await expect.poll(() => firstReturned).toBe(true)
  await dialog.getByRole('button', { name: '持仓', exact: true }).click()
  await expect(dialog.locator('[data-swap-current] [data-position-table] [data-activity-row]')).toHaveCount(1)
  await expect(overview.getByRole('status')).toHaveCount(0)
})
