import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'

// Module stubs stay in a child process so this page test cannot replace the API
// or hooks used by the cache/chart tests in the main Bun test process.
function renderPage(options: { displayed?: boolean; marker?: number; status?: string; loadingResult?: boolean; errors?: boolean; retries?: string[]; selected?: boolean } = {}) {
  const script = `
    import { mock } from 'bun:test'
    import { createElement } from 'react'
    import { renderToStaticMarkup } from 'react-dom/server'
    const options = JSON.parse(process.env.HISTORY_SCENARIO)
    const charts = [], queries = [], notices = [], retryHandlers = new Map()
    let checks = 0, refreshes = 0
    const metadata = { schema_version: 1, status: options.status ?? 'ready', source_version: 2, snapshot_id: 's2', snapshot_version: 2, logic_version: 'logic', engine_version: 'engine', computed_at: '2026-10-20T12:00:00', data_until: null, error: options.errors ? 'calculation failed' : null, retry_at: null, read_view_schema_version: options.marker ?? 1 }
    const result = { range: '90', settings: { backtest_fee_rate: 0, backtest_weight_type: 'cs' }, backtest_included: true, engine_version: 'engine', baseline: '1970-01-01T00:00:00Z', end: '1970-01-01T00:00:01Z', record_count: 1, observation_count: 2, used_record_count: 1, invalid_asset_count: 0, skips: null, points: [{ date: '1970-01-01', observed_at: '1970-01-01T00:00:00Z', account_return: 0, portfolio_return: 0 }, { date: '1970-01-01', observed_at: '1970-01-01T00:00:01Z', account_return: 0.1, portfolio_return: 0.1 }], bindings: [], calendar: { status: 'available', calendar_id: 'test', label: 'fixture', closed_ranges: [], unavailable_ranges: [] } }
    const displayed = options.displayed === false ? null : { snapshot_id: 's1', snapshot_version: 1, range: '90', computed_at: null, data_until: null, result, daily_costs: {}, events: [], event_count: 0, execution_index: [{ record_id: 11, time_ms: 1000, has_transactions: false }] }
    mock.module('react-router', () => ({ useParams: () => ({ id: '7' }) }))
    mock.module('@/components/ui/nav', () => ({ useNavigate: () => () => {} }))
    mock.module('@/features/account/pageHead', () => ({ AccountPageTitle: () => createElement('h1', null, 'History') }))
    mock.module('@/stores/domain', () => ({ useDomainStore: selector => selector({ accounts: [] }) }))
    mock.module('@/components/ui/Segmented', () => ({ Segmented: ({ value }) => createElement('span', { 'data-segment': value }) }))
    mock.module('@/components/ui/Tooltip', () => ({ Tooltip: ({ children }) => children }))
    mock.module('@/features/history/FeeControl', () => ({ FeeControl: () => null }))
    mock.module('@/components/ui/ErrorNotice', () => ({ ErrorNotice: ({ title, error, onRetry }) => { if (!error) return null; notices.push(title); retryHandlers.set(title, onRetry); return createElement('p', { role: 'status' }, title, ': ', error.message) } }))
    mock.module('@/components/viz/PerformanceChart', () => ({ PerformanceChart: props => { charts.push({ snapshotId: props.snapshotId, executionIndex: props.executionIndex, hasFullExecutions: 'executions' in props.data, daily: props.daily, backtestMode: props.backtestMode, scaleMode: props.scaleMode, showExecutions: props.showExecutions }); return createElement('div', { 'data-chart-snapshot': props.snapshotId }, props.controls) } }))
    mock.module('@/features/history/PerformanceChartPlaceholder', () => ({ PerformanceChartPlaceholder: props => createElement('div', { 'data-placeholder-loading': String(props.loading), 'data-placeholder-failed': String(props.failed) }, props.message, props.controls) }))
    mock.module('@/lib/api/performance', () => ({
      getPerformanceCosts: async (accountId, query) => { queries.push({ accountId, ...query }); return { snapshot_id: query.snapshot_id, summary: {} } },
      selectionQuery: selection => selection?.kind === 'interval' ? { start: selection.start, end: selection.end } : {},
      savePerformanceSettings: async () => ({ backtest_fee_rate: 0, backtest_weight_type: 'cs' }),
    }))
    mock.module('@/lib/hooks/usePolling', () => ({ usePolling: (fetcher, { queryKey, enabled = true }) => {
      if (queryKey.startsWith('performance-summary:') && enabled) void fetcher(new AbortController().signal)
      return { data: queryKey.startsWith('account:') ? { name: 'fixture', backtest_fee_rate: 0, backtest_weight_type: 'cs' } : queryKey === 'performance-portfolios' ? { data: [] } : null, loading: false, error: null, refresh: async () => {} }
    } }))
    mock.module('@/features/history/usePerformanceSnapshot', () => ({ usePerformanceSnapshot: () => ({ metadata, displayed, statusError: options.errors ? new Error('status failed') : null, resultError: options.errors ? new Error('result failed') : null, refreshError: options.errors ? new Error('refresh failed') : null, checking: false, loadingResult: options.loadingResult ?? false, check: async () => { checks++ }, refresh: async () => { refreshes++; return true } }) }))
    const { performanceViews } = await import('@/features/history/viewState')
    performanceViews.set(7, { range: '90', view: 'daily', scale: 'natural', markers: false, backtestMode: 'target', selection: options.selected === false ? null : { kind: 'interval', start: 0, end: 1000 } })
    const { AccountHistoryPage } = await import('@/pages/AccountHistoryPage')
    const html = renderToStaticMarkup(createElement(AccountHistoryPage))
    for (const title of options.retries ?? []) await retryHandlers.get(title)?.()
    console.log(JSON.stringify({ html, charts, queries, notices, checks, refreshes }))
  `
  const child = spawnSync(process.execPath, ['-e', script], {
    cwd: `${import.meta.dir}/../..`,
    env: { ...process.env, HISTORY_SCENARIO: JSON.stringify(options) },
    encoding: 'utf8', timeout: 20_000,
  })
  if (child.status !== 0) throw new Error(child.error?.message ?? child.stderr ?? `Child exited ${child.status}`)
  return JSON.parse(child.stdout) as { html: string; charts: Array<{ snapshotId: string; executionIndex: Array<{ record_id: number; time_ms: number; has_transactions: boolean }>; hasFullExecutions: boolean; daily: boolean; backtestMode: string; scaleMode: string; showExecutions: boolean }>; queries: Array<{ snapshot_id: string; range: string; accountId: number; dimension: string; start: number; end: number }>; notices: string[]; checks: number; refreshes: number }
}

test('新版结果未齐时图表、成本查询和浏览偏好仍绑定完整旧版', () => {
  const page = renderPage({ loadingResult: true })
  expect(page.charts).toEqual([{ snapshotId: 's1', executionIndex: [{ record_id: 11, time_ms: 1000, has_transactions: false }], hasFullExecutions: false, daily: true, backtestMode: 'target', scaleMode: 'natural', showExecutions: false }])
  expect(page.queries).toEqual([{ accountId: 7, snapshot_id: 's1', range: '90', dimension: 'summary', start: 0, end: 1000 }])
  expect(page.html).toContain('正在读取新版绩效，当前保留上次结果')
  expect(page.html).toContain('data-segment="90"')
  expect(page.refreshes).toBe(0)
})

test('历史 marker0 明确要求计算且保留可用刷新入口', () => {
  const page = renderPage({ displayed: false, marker: 0 })
  expect(page.html).toContain('当前快照需要重新计算，请刷新绩效与成本')
  expect(page.html).toContain('data-placeholder-loading="false"')
  expect(page.html).toMatch(/aria-label="刷新绩效与成本"[^>]*>/)
  expect(page.html).not.toMatch(/aria-label="刷新绩效与成本"[^>]* disabled=/)
  expect(page.queries).toEqual([])
  expect(page.refreshes).toBe(0)
})

test('marker0 已在计算时展示计算状态，避免要求点击禁用的刷新入口', () => {
  const page = renderPage({ displayed: false, marker: 0, status: 'pending' })
  expect(page.html).toContain('正在重新计算，完成后显示绩效结果')
  expect(page.html).not.toContain('请刷新绩效与成本')
  expect(page.html).toMatch(/aria-label="刷新绩效与成本"[^>]* disabled=/)
})

test('状态、结果、计算和刷新错误分别保留，读重试不会提交新计算', () => {
  const page = renderPage({ errors: true, retries: ['绩效状态读取失败', '新版绩效读取失败，保留上次结果'] })
  expect(page.notices).toEqual(['绩效状态读取失败', '新版绩效读取失败，保留上次结果', '绩效计算失败，保留上次结果', '刷新请求失败'])
  expect(page.charts[0].snapshotId).toBe('s1')
  expect(page.checks).toBe(2)
  expect(page.refreshes).toBe(0)
})

test('刷新请求错误的重试只发起一次计算请求', () => {
  const page = renderPage({ errors: true, retries: ['刷新请求失败'] })
  expect(page.checks).toBe(0)
  expect(page.refreshes).toBe(1)
})

test('共用筛选集中在吸顶栏，空选区也可从起止观测建立区间', () => {
  const page = renderPage({ selected: false })
  const toolbar = page.html.slice(page.html.indexOf('aria-label="绩效与品种贡献共用筛选"'), page.html.indexOf('data-chart-snapshot'))
  expect(page.html).toMatch(/data-testid="performance-sticky-header" class="sticky top-0/)
  expect(toolbar).toContain('data-segment="90"')
  expect(toolbar).toContain('data-segment="natural"')
  for (const label of ['比较起点', '比较终点']) {
    const button = toolbar.match(new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`))?.[0]
    expect(button).toBeDefined()
    expect(button).not.toContain('disabled=""')
  }
  expect(toolbar).toMatch(/aria-label="取消选择" disabled=""/)
  expect((page.html.match(/data-segment="natural"/g) ?? []).length).toBe(1)
})

test('未加载观测时保留公共时间范围，禁用区间选择', () => {
  const page = renderPage({ displayed: false, selected: false })
  expect(page.html).toContain('data-testid="performance-shared-controls"')
  for (const label of ['比较起点', '比较终点']) {
    const button = page.html.match(new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`))?.[0]
    expect(button).toContain('disabled=""')
  }
})
