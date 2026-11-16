import { expect, test, type Page } from '@playwright/test'

const execution = '/accounts/7/executions/E'
const account = { id: 7, name: '下载测试账户', market: 'demo', trade_channel: 'paper', portfolio_id: null, backtest_fee_rate: 0, backtest_weight_type: 'cs', is_started: false }
async function defaults(page: Page) {
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url()), path = url.pathname
    const range = url.searchParams.get('range') ?? '30'
    const context = { account_id: 7, execution_id: 'E', recording_id: 'R', through_seq: 1, commit_id: 'c', index_generation: 'g' }
    const json = path.endsWith('/init/status') ? { configured: true, environment: 'test', values: {} }
      : path.endsWith('/capabilities/channels') ? []
      : path.endsWith('/account/7') ? account
      : path.endsWith('/account/analysis/7/status') ? { schema_version: 1, status: 'ready', version: 's1', source_version: 1, computed_at: null }
      : path.endsWith('/account/analysis/7/inputs') ? { protocol: 2, kind: url.searchParams.get('kind'), algorithm: 'local-analysis-1', account_id: 7, version: 's1', range, initial_equity: null, records: [], next_cursor: null }
      : path.endsWith('/account/analysis/7') ? { account_id: 7, schema_version: 1, version: 's1', range, daily_costs: {}, events: [], execution_index: [], event_count: 0, contributions: [], result: { range, settings: { backtest_fee_rate: 0, backtest_weight_type: 'cs' }, points: [{ date: '2026-01-01', account_return: 0, portfolio_return: 0 }, { date: '2026-01-02', account_return: 0.1, portfolio_return: 0.1 }], skips: null, bindings: [], record_count: 0, observation_count: 2, used_record_count: 0, calendar: { status: 'available', closed_ranges: [], unavailable_ranges: [] } } }
      : path.endsWith('/session') ? { reader_epoch: 'test', session: 'session' }
      : path.endsWith('/control') ? { state: 'settled', session_bytes: 0 }
      : path.endsWith('/manifest') ? { schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [{ id: 'R', seq: 0, watermark: 1, commit_id: 'c', start_us: 0, state: 'closed', streams: [], symbols: ['S'] }] }
      : path.endsWith('/plan') ? { ...context, target_us: JSON.parse(route.request().postData()!).time_us, cutoff_seq: 1, selected: null, evidence: [], blocks: [], planned_bytes: 0 }
      : /\/(market-)?objects$/.test(path) ? { ...context, subjects: [], next: null }
      : path.endsWith('/business-blocks') ? { ...context, blocks: [], next: null }
      : path.endsWith('/range-blocks') ? { ...context, streams: [], initial: [], range_through: 1 }
      : path.endsWith('/versions') ? { ...context, versions: [], next: null }
      : { data: [], count: 0 }
    return route.fulfill({ json })
  })
}

for (const cancel of ['button', 'escape', 'backdrop']) test(`raw replay cancel returns to execution details without downloads (${cancel})`, async ({ page }) => {
  await defaults(page)
  const requests: string[] = []
  page.on('request', request => { if (/\.wasm$|\/replay\//.test(request.url())) requests.push(request.url()) })
  await page.goto(`${execution}/replay`)
  const dialog = page.getByRole('dialog').filter({ hasText: '下载回放组件' })
  await expect(dialog).toBeVisible()
  expect(requests).toEqual([])
  if (cancel === 'button') await dialog.getByRole('button', { name: '取消', exact: true }).click()
  else if (cancel === 'escape') await page.keyboard.press('Escape')
  else await page.mouse.click(5, 5)
  await expect(page).toHaveURL(execution)
  expect(requests).toEqual([])
})

test('raw replay shows progress after confirmation and reuses the cached version', async ({ page }) => {
  await defaults(page)
  const requests: string[] = []
  page.on('request', request => { if (/\.wasm$/.test(request.url())) requests.push(request.url()) })
  let release!: () => void
  const waiting = new Promise<void>(resolve => { release = resolve })
  await page.route('**/*.wasm', async route => { await waiting; await route.continue() })
  await page.goto(`${execution}/replay`)
  try {
    await page.getByRole('button', { name: '下载并进入回放' }).click()
    await expect(page.getByRole('progressbar')).toBeVisible()
    await expect(page).toHaveURL(`${execution}/replay`)
    await page.screenshot({ path: 'test-results/wasm-replay-downloading.png', fullPage: true })
  } finally { release() }
  await expect(page.locator('.replay-chart')).toBeVisible()
  await expect(page.getByRole('button', { name: '展开区间统计' })).toBeVisible()
  expect(requests.length).toBe(2)
  await page.reload()
  await expect(page.getByRole('heading', { name: /执行回放/ })).toBeVisible()
  await expect(page.getByText('下载回放组件', { exact: true })).toHaveCount(0)
  expect(requests.length).toBe(2)
})

test('fee edits wait for a click; costs download automatically and share the same runtime', async ({ page }) => {
  await defaults(page)
  const requests: string[] = []
  page.on('pageerror', error => { throw error })
  page.on('request', request => { if (/\.wasm$/.test(request.url())) requests.push(request.url()) })
  await page.goto('/accounts/7/history')
  await expect(page.getByRole('button', { name: '展开成本分析' })).toBeVisible()
  await page.getByRole('textbox', { name: '单边费率（BP）' }).fill('2')
  await expect(page.getByRole('button', { name: /启用本地试算/ })).toBeVisible()
  expect(requests).toEqual([])
  await page.getByRole('button', { name: '展开成本分析' }).click()
  await expect(page.getByText('正在加载选区并计算成本')).toHaveCount(0)
  await expect(page.getByRole('definition').filter({ hasText: '0' }).first()).toBeVisible()
  expect(requests.length).toBe(1)
  await page.reload()
  await page.getByRole('button', { name: '展开成本分析' }).click()
  await expect(page.getByText('正在加载选区并计算成本')).toHaveCount(0)
  expect(requests.length).toBe(1)
})
