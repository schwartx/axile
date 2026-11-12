import { expect, test } from '@playwright/test'

test('没有 SSE 时仍实时推进，终态补读证据并保留静止阶段线', async ({ page }, testInfo) => {
  let phase = 'planning'
  let finished = false
  let stale = false
  let finalDetails = 0
  const event = (event_type: string, extra = {}) => ({ event_type, status: 'INFO', symbol: null,
    ts_local_created: '2026-11-12T10:00:00', seq: 1, details: {}, ...extra })
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/executions/A')) return route.fulfill({ json: {
      execution_id: 'A', account_id: 7, execution_kind: 'rebalance', status: finished ? 'SUCCEEDED' : 'RUNNING',
      created_at: '2026-11-12T10:00:00', started_at: '2026-11-12T10:00:00', progress: { phase, stale },
    } })
    if (path.endsWith('/events')) return route.fulfill({ json: { count: phase === 'planning' ? 1 : finished ? 3 : 2, data: [
      event('target_computed'), ...(phase === 'planning' ? [] : [event('order_submitted', { symbol: 'TEST', order_id: 'O', details: {
        order: { direction: 'BUY', volume: 10, filled_volume: 3, status: '部分成交' },
      } })]), ...(finished ? [event('execution_completed')] : []),
    ] } })
    if (path.endsWith('/artifacts')) return route.fulfill({ json: { count: 0, data: [] } })
    if (path.endsWith('/detail')) {
      if (finished) finalDetails++
      return route.fulfill({ json: finished ? {
        success: true, status: 'SUCCEEDED',
        reconciliation: { account: { source_before: 'real', source_after: 'real' },
          symbols: [{ symbol: 'TEST', before: 0, after: 10, target: 10, reached: true }] },
        symbol_results: { TEST: { status: 'SUCCEEDED', target_volume: 10, final_volume: 10, filled: 10 } },
      } : null })
    }
    return route.fulfill({ json: { data: [], count: 0 } })
  })
  await page.clock.install()
  await page.goto('/e2e/evidence.html')
  const bar = page.getByRole('group', { name: '执行阶段' })
  await expect(bar.locator('[aria-current="step"]')).toHaveAttribute('aria-label', '算目标')
  await expect(page.getByText(/执行后账户快照为/)).toHaveCount(0)
  phase = 'executing'
  await page.clock.runFor(2500)
  await expect(bar.locator('[aria-current="step"]')).toHaveAttribute('aria-label', '下单')
  await expect(page.getByText('执行中 · 持仓待核对', { exact: true })).toBeVisible()
  await expect(page.getByText(/买 3/)).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('running.png') })
  stale = true
  await page.clock.runFor(2500)
  await expect(page.getByText('进度更新中断，显示最后确认的阶段。')).toBeVisible()
  await expect(bar.locator('[class*="animate-pulse"]')).toHaveCount(0)
  stale = false
  finished = true
  await page.clock.runFor(2500)
  await expect(page.getByText('逐只结果', { exact: true })).toBeVisible()
  await expect(page.getByText(/实际 0 → 10/)).toBeVisible()
  await expect(bar.locator('[aria-current="step"]')).toHaveAttribute('aria-label', '对账')
  await expect(bar.locator('[class*="animate-pulse"]')).toHaveCount(0)
  expect(finalDetails).toBeGreaterThan(0)
  const settledReads = finalDetails
  await page.clock.runFor(5000)
  expect(finalDetails).toBe(settledReads)
})

for (const scenario of ['正常跳过', '快照读取失败', '仍有未决委托']) {
  test(`休市执行：${scenario}`, async ({ page }) => {
    const blocked = scenario === '仍有未决委托'
    const source = scenario === '快照读取失败' ? 'error' : 'unavailable'
    const order = { order_id: 'O', symbol: 'TEST', direction: 'BUY', order_type: 'LIMIT', volume: 10, filled_volume: 0, status: '已报' }
    const result = {
      status: blocked ? 'BLOCKED' : 'NOOP', reason_code: 'COMMON.SESSION.CLOSED',
      symbol_results: blocked ? { TEST: { status: 'BLOCKED', reason_code: 'COMMON.SESSION.CLOSED', orders: [order] } } : {},
      reconciliation: { account: { source_before: 'real', source_after: source }, symbols: [] },
    }
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/executions/A')) return route.fulfill({ json: {
        execution_id: 'A', account_id: 7, status: blocked ? 'FAILED' : 'SKIPPED', output_status: result.status,
        created_at: '2026-11-12T10:00:00', started_at: '2026-11-12T10:00:00',
      } })
      if (path.endsWith('/detail')) return route.fulfill({ json: result })
      if (path.endsWith('/events')) return route.fulfill({ json: { count: 2, data: ['target_computed', 'execution_completed'].map((event_type, seq) => ({
        event_type, status: 'INFO', seq, symbol: null, details: {}, ts_local_created: '2026-11-12T10:00:00',
      })) } })
      if (path.endsWith('/artifacts')) return route.fulfill({ json: { count: 2, data: [
        { id: 'input', artifact_type: 'standard_input', content: { input: { target_mode: 'quantity', curr_target: { TEST: 1000 } } } },
        { id: 'after', artifact_type: 'account_snapshot', content: { source } },
      ] } })
      return route.fulfill({ json: { data: [], count: 0 } })
    })
    await page.goto('/e2e/evidence.html')
    if (scenario === '正常跳过') {
      await expect(page.getByText('本次因非交易时段跳过，未采集执行后账户快照。')).toBeVisible()
      await expect(page.getByText(/执行后账户快照为/)).toHaveCount(0)
      await expect(page.getByText('本次未下单，无逐品种执行结果。')).toBeVisible()
      await expect(page.getByText('结果待确认', { exact: true })).toHaveCount(0)
      await expect(page.getByText('当时未记录换算依据', { exact: true })).toHaveCount(0)
      await expect(page.getByRole('group', { name: '执行阶段' }).locator('[aria-current="step"]')).toHaveAttribute('aria-label', '算目标')
      await expect(page.getByText('读执行后账户', { exact: true })).toHaveCount(0)
    } else {
      await expect(page.getByText(/执行后账户快照为/)).toBeVisible()
      await expect(page.getByText('本次因非交易时段跳过，未采集执行后账户快照。')).toHaveCount(0)
    }
  })
}
