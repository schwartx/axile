import { expect, test, type Page } from '@playwright/test'

const raw = (name: string) => `${name}开始\n${'synthetic error '.repeat(100)}\nTraceback\n  script.py:123\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因-${name}`
const record = (id = 'A') => ({ id: 1, account_id: 7, execution_id: id, raw_input: {}, raw_result: { status: 'FAILED', technical_detail: raw(id), traceback: raw(id) }, is_success: 0, created_at: '2026-10-14T10:00:00' })
const sync = (status = 'synchronized', revision = 1, last_error: string | null = null) => ({ status, revision, last_error, attempts: 1, last_attempt_at: null, synchronized_at: null })
const account = { id: 7, name: '合成账户 A', market: 'demo', trade_channel: 'paper', is_started: false, cron_expr: '', portfolio_id: 9, algorithm: { method: 'SINGLE-MAKER', params: {} }, empty_positions_algorithm: null, account_config: {}, forbidden_symbols: [], risk_symbols: [], trade_rules: [], long_leverage: 1, short_leverage: 1, weight_precision: .001 }
const snapshot = { portfolio_id: 9, context_account_id: 7, weights: {}, calculated_at: '2026-10-14T10:00:00', sizing: null }
const dashboardItem = { account_id: 7, name: account.name, remark: null, market: 'demo', trade_channel: 'paper', is_started: false,
  portfolio_id: 9, is_scheduled: false, next_run_time: null, total_asset: 100, currency: 'CNY', holdings_count: 0, position_weights: [],
  last_is_success: null, last_exec_at: null, last_output_status: null, off_symbol_count: null,
  connection: { status: 'ok', observed_at: null, message: null } }

async function defaults(page: Page) {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/v1/editor/')) return route.fallback()
    const json = path.endsWith('/account/dashboard') ? { data: [dashboardItem] }
      : path.endsWith('/capabilities/channels') ? []
      : path.endsWith('/orders') ? { data: [], count: 0, complete: true, sampled_at: '2026-10-14T10:00:00', cancellations: [] }
      : path.endsWith('/runtime-sync') ? sync() : /\/account\/7$/.test(path) ? account
      : path.endsWith('/target_snapshot') ? snapshot : path.endsWith('/rebalance_plan') ? { rows: [], off_symbol_count: 0 }
      : path.endsWith('/asset_snapshots') ? { data: [{ assets: { total_asset: 100, available_cash: 100, market_value: 0, positions: [] }, created_at: '2026-10-14T10:00:00' }], count: 1 }
      : path.endsWith('/portfolio/9') ? { id: 9, name: '合成组合', market: 'demo', custom_calc_py_code: 'def calculate_portfolio(context):\n    return {}\n', description: '' }
      : path.endsWith('/algorithms') ? [] : path.endsWith('/next_run_time') ? { next_run_time: null, next_execution_times: [] }
      : { data: [], count: 0 }
    return route.fulfill({ json })
  })
}
async function clipboard(page: Page) {
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { value: { writeText: async (text: string) => { (window as unknown as { copied: string }).copied = text } } }))
}
async function copied(page: Page) { return page.evaluate(() => (window as unknown as { copied: string }).copied) }

/** 合成 LSP 只让快速修复失败，诊断仍成功返回。 */
async function quickFixLsp(page: Page, diagnostic: boolean, fail: () => boolean) {
  await page.routeWebSocket('**/api/v1/editor/lsp', socket => {
    socket.onMessage(data => {
      const request = JSON.parse(String(data))
      if (request.id == null) return
      if (request.method === 'textDocument/codeAction' && fail()) {
        socket.send(JSON.stringify({ jsonrpc: '2.0', id: request.id, error: { code: -32603, message: raw('快速修复') } }))
        return
      }
      const result = request.method === 'initialize' ? { capabilities: { textDocumentSync: 1, diagnosticProvider: { interFileDependencies: false, workspaceDiagnostics: false }, codeActionProvider: true } }
        : request.method === 'textDocument/diagnostic' ? { kind: 'full', items: diagnostic ? [{ message: '仍然有效的 LSP 诊断', severity: 2, range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } } }] : [] }
        : request.method === 'textDocument/codeAction' ? [] : null
      socket.send(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }))
    })
    socket.send(JSON.stringify({ type: 'session', rootUri: 'file:///synthetic', uri: 'file:///synthetic/main.py' }))
  })
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', error => { throw error })
  await clipboard(page)
})

test('all first-read network failures share one global notice and recover without writes', async ({ page }) => {
  await defaults(page)
  let offline = true
  const reads: Record<string, number> = {}
  let writes = 0
  await page.route('**/api/v1/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() !== 'GET') writes++
    reads[path] = (reads[path] ?? 0) + 1
    return offline ? route.abort() : route.fallback()
  })
  await page.goto('/e2e/errors.html?mode=outage')
  await expect(page.getByRole('button', { name: '查看服务连接错误详情' })).toContainText('与服务器失联')
  await expect(page.getByText('无法连接 axile 服务', { exact: false })).toHaveCount(1)
  await expect(page.getByRole('button', { name: '查看错误详情', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^重试：/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '重试同步', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '重新计算当前目标权重', exact: true })).toBeEnabled()
  await expect(page.getByText('暂不可用', { exact: true }).filter({ visible: true }).first()).toBeVisible()
  const previous = { ...reads }
  offline = false
  await page.getByRole('button', { name: '重试连接服务器' }).click()
  await expect(page.getByRole('banner')).toContainText('服务正常')
  await expect(page.getByRole('status', { name: '账户连通状态：正常' })).toBeVisible()
  for (const source of ['runtime-sync', 'asset_snapshots', 'target_snapshot', 'next_run_time', 'rebalance_plan']) {
    const path = `/api/v1/account/7/${source}`
    await expect.poll(() => reads[path]).toBe((previous[path] ?? 0) + 1)
  }
  for (const path of ['/api/v1/capabilities/channels', '/api/v1/portfolio/']) {
    await expect.poll(() => reads[path]).toBe((previous[path] ?? 0) + 1)
  }
  await expect(page.getByText('暂不可用', { exact: true })).toHaveCount(0)
  expect(writes).toBe(0)
})

test('cached values remain visible during outage; active operations retain their own failure', async ({ page }) => {
  await defaults(page)
  let offline = false, patches = 0
  await page.route('**/api/v1/**', route => {
    if (route.request().method() === 'PATCH') patches++
    return offline ? route.abort() : route.fallback()
  })
  await page.goto('/e2e/errors.html?mode=outage')
  await expect(page.getByRole('status', { name: '账户连通状态：正常' })).toBeVisible()
  await expect(page.getByRole('button', { name: '刷新账户资产', exact: true })).toBeVisible()
  offline = true
  await page.getByRole('button', { name: '刷新页面读取' }).click()
  await expect(page.getByRole('banner')).toContainText('与服务器失联')
  await expect(page.getByRole('button', { name: '账户连通状态：未知' })).toContainText('连接状态待更新')
  await expect(page.getByText(/当前显示 .*的数据/).filter({ visible: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '刷新账户资产', exact: true })).toBeVisible()
  const start = page.getByRole('button', { name: '启动自动执行', exact: true })
  await expect(start).toBeEnabled()
  await start.click()
  await expect(page.getByRole('alert').filter({ hasText: '自动执行配置保存失败' })).toContainText('无法连接 axile 服务')
  expect(patches).toBe(1)
  offline = false
  await page.getByRole('button', { name: '重试连接服务器' }).click()
  await expect(page.getByRole('banner')).toContainText('服务正常')
  await expect(start).toBeEnabled()
  expect(patches).toBe(1)
})

test('HTTP errors keep endpoint meaning and independent retry', async ({ page }) => {
  await defaults(page)
  await page.route('**/api/v1/account/dashboard', route => route.fulfill({ status: 500, json: { detail: '账户汇总失败' } }))
  await page.route('**/api/v1/account/7/target_snapshot', route => route.fulfill({ status: 422, json: { detail: '目标参数无效' } }))
  await page.goto('/e2e/errors.html?mode=outage')
  await expect(page.getByRole('banner')).toContainText('账户数据刷新失败')
  await expect(page.getByRole('banner')).not.toContainText('与服务器失联')
  const target = page.getByRole('status').filter({ hasText: '目标快照读取失败' })
  await expect(target).toContainText('目标参数无效')
  await expect(target.getByRole('button', { name: '重试：目标快照读取失败' })).toBeVisible()
})

test('reconnect resumes the accepted execution id without triggering again', async ({ page }) => {
  await defaults(page)
  let offline = false, triggers = 0, reads = 0
  await page.route('**/api/v1/**', route => offline ? route.abort() : route.fallback())
  await page.route('**/api/v1/account/execute/7', route => {
    triggers++
    return route.fulfill({ json: { execution_id: 'same-execution' } })
  })
  await page.route('**/api/v1/account/executions/same-execution', route => {
    if (offline) return route.abort()
    reads++
    if (reads === 2) { offline = true; return route.abort() }
    return route.fulfill({ json: { execution_id: 'same-execution', status: reads === 1 ? 'RUNNING' : 'SUCCEEDED', output_status: 'SUCCEEDED' } })
  })
  await page.goto('/e2e/errors.html?mode=runner&global')
  await page.getByRole('button', { name: '立即执行', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: '状态待确认' })).toBeVisible()
  await page.getByRole('button', { name: '刷新页面读取' }).click()
  await expect(page.getByRole('banner')).toContainText('与服务器失联')
  await expect(page.getByTestId('runner')).toContainText('same-execution')
  await expect(page.getByRole('button', { name: '立即执行', exact: true })).toBeDisabled()
  offline = false
  await page.getByRole('button', { name: '重试连接服务器' }).click()
  await expect(page.getByRole('button', { name: '立即执行', exact: true })).toBeEnabled()
  expect(triggers).toBe(1)
})

test('reconnect preserves an unsaved portfolio draft and never recomputes it', async ({ page }) => {
  await defaults(page)
  let offline = false, writes = 0
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/v1/editor/')) return route.fallback()
    if (route.request().method() !== 'GET') writes++
    return offline ? route.abort() : route.fallback()
  })
  await page.goto('/e2e/errors.html?mode=portfolio&global')
  const name = page.getByRole('textbox', { name: '组合名称' })
  await name.fill('仍未保存的组合草稿')
  offline = true
  await page.getByRole('button', { name: '刷新页面读取' }).click()
  await expect(page.getByRole('banner')).toContainText('与服务器失联')
  await expect(name).toHaveText('仍未保存的组合草稿')
  offline = false
  await page.getByRole('button', { name: '重试连接服务器' }).click()
  await expect(page.getByRole('banner')).toContainText('服务正常')
  await expect(name).toHaveText('仍未保存的组合草稿')
  expect(writes).toBe(0)
})

test('full error and copy freeze A while the page receives B; Escape restores focus', async ({ page }) => {
  await page.goto('/e2e/errors.html')
  const notice = page.getByRole('status').filter({ hasText: '合成操作失败' })
  const button = notice.getByRole('button', { name: '查看错误详情' })
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await button.click()
  const dialog = page.getByRole('dialog', { name: '合成操作失败' })
  await expect(dialog).toContainText('尾部根因-A')
  await page.getByRole('button', { name: '后台更新为 B' }).evaluate((node: HTMLButtonElement) => node.click())
  await expect(notice).toContainText('B开始')
  await expect(dialog).not.toContainText('尾部根因-B')
  await dialog.getByRole('button', { name: '复制错误信息' }).click()
  expect(await copied(page)).toContain(raw('A'))
  expect(await copied(page)).not.toContain(raw('B'))
  await page.keyboard.press('Escape')
  await expect(button).toBeFocused()
  await button.click()
  await expect(dialog).toContainText('尾部根因-B')
  await page.getByRole('button', { name: '切换上下文' }).evaluate((node: HTMLButtonElement) => node.click())
  await expect(dialog).toHaveCount(0)
})

test('inline details stay frozen when the enclosing modal query recovers', async ({ page }) => {
  await page.goto('/e2e/errors.html?mode=embedded')
  const dialog = page.getByRole('dialog', { name: '已有业务弹窗' })
  await dialog.getByRole('button', { name: '查看错误详情' }).click()
  await dialog.getByRole('button', { name: '后台查询恢复成功' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(1)
  await expect(dialog).toContainText('尾部根因-A')
  await dialog.getByRole('button', { name: '复制错误信息' }).click()
  expect(await copied(page)).toContain(raw('A'))
  await dialog.getByRole('button', { name: '收起详情' }).click()
  await expect(dialog.getByRole('button', { name: '复制错误信息' })).not.toBeVisible()
})

for (const old of [true, false]) test(`target errors remain readable in both layouts, old snapshot=${old}`, async ({ page }) => {
  await page.goto('/e2e/errors.html')
  if (!old) await page.getByRole('button', { name: '切换首次计算' }).click()
  for (const variant of ['full', 'compact']) {
    const section = page.getByRole('region', { name: variant })
    await expect(section).toContainText('A开始')
    await section.getByRole('button', { name: '查看错误详情' }).click()
    await expect(page.getByRole('dialog')).toContainText('尾部根因-A')
    await page.keyboard.press('Escape')
  }
})

test('narrow screen, reduced motion, keyboard trap and fixed footer keep long details usable', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 620 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/e2e/errors.html')
  await page.getByRole('status').filter({ hasText: '合成操作失败' }).getByRole('button', { name: '查看错误详情' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: '复制错误信息' })).toBeInViewport()
  await expect(dialog.getByRole('button', { name: '关闭详情' })).toBeInViewport()
  await page.keyboard.press('Shift+Tab')
  await expect(dialog.getByRole('button', { name: '关闭详情' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('evidence reads exact record and every event/artifact page before copying all', async ({ page }) => {
  const queries: string[] = []
  let release!: () => void
  const delayed = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/account/**', async route => {
    const url = new URL(route.request().url()); queries.push(url.pathname + url.search)
    if (url.pathname.includes('/execute_records/')) return route.fulfill({ json: { data: [record()], count: 1 } })
    const skip = Number(url.searchParams.get('skip') ?? 0)
    if (/\/(events|artifacts)$/.test(url.pathname)) {
      if (skip) await delayed
      const artifacts = url.pathname.endsWith('/artifacts')
      return route.fulfill({ json: { count: 501, data: Array.from({ length: skip ? 1 : 500 }, (_, i) => artifacts
        ? { id: skip + i + 1, artifact_type: 'execution_summary', content: skip ? { error: '最后一页附件根因' } : {} }
        : { id: skip + i + 1, event_type: skip ? 'instruments_failed' : 'noop', reason_code: 'CATALOG.FAIL', details: skip ? { message: raw('event'), symbols: ['rb2701'], elapsed_seconds: 28 } : {} }) } })
    }
    return route.fulfill({ json: { status: 'FAILED', execution_id: 'A', account_id: 7, error: 'script failed' } })
  })
  await page.goto('/e2e/errors.html?mode=evidence')
  await page.getByRole('button', { name: '查看错误详情' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('尾部根因-A')
  await expect(dialog.getByRole('button', { name: '复制全部', exact: true })).toBeDisabled()
  release()
  await expect(dialog.getByRole('button', { name: '复制全部', exact: true })).toBeEnabled()
  await expect(dialog).toContainText('最后一页附件根因')
  await expect(dialog).toContainText('rb2701')
  await expect(dialog).toContainText('28 秒')
  await dialog.getByRole('button', { name: '复制全部', exact: true }).click()
  expect(await copied(page)).toContain(raw('A'))
  expect(await copied(page)).toContain('最后一页附件根因')
  expect(queries.filter(url => url.includes('/execute_records/'))).toEqual(['/api/v1/account/execute_records/7?limit=1&execution_id=A'])
  expect(queries.some(url => url.includes('events?skip=500'))).toBe(true)
  expect(queries.some(url => url.includes('artifacts?skip=500'))).toBe(true)
})

test('partial evidence keeps record originals and retries only the failed source', async ({ page }) => {
  const reads: Record<string, number> = {}
  await page.route('**/api/v1/account/**', route => {
    const path = new URL(route.request().url()).pathname
    const key = path.split('/').at(-1)!
    reads[key] = (reads[key] ?? 0) + 1
    if (path.includes('/execute_records/')) return route.fulfill({ json: { data: [record()], count: 1 } })
    if (path.endsWith('/events') && reads[key] === 1) return route.fulfill({ status: 500, json: { detail: { message: raw('query'), code: 'EVENTS_FAILED', request_id: 'req-events' } } })
    if (/\/(events|artifacts)$/.test(path)) return route.fulfill({ json: { data: [], count: 0 } })
    return route.fulfill({ status: 404, json: { detail: '任务查询不存在，不能确认终态' } })
  })
  await page.goto('/e2e/errors.html?mode=evidence')
  await page.getByRole('button', { name: '查看错误详情' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: '复制已读取内容' })).toBeEnabled()
  await expect(dialog).toContainText('尾部根因-A')
  await expect(dialog.getByRole('button', { name: '复制全部', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: '复制已读取内容' }).click()
  expect(await copied(page)).toContain('EVENTS_FAILED')
  await dialog.getByRole('button', { name: '重试：执行事件读取失败' }).click()
  await expect.poll(() => reads.events).toBe(2)
  expect(reads['7']).toBe(1)
  expect(reads.artifacts).toBe(1)
})

test('legacy record without execution id reads and copies its exact record only', async ({ page }) => {
  const queries: string[] = []
  await page.route('**/api/v1/account/**', route => { queries.push(route.request().url()); return route.fulfill({ json: { data: [{ ...record(), execution_id: null }], count: 1 } }) })
  await page.goto('/e2e/errors.html?mode=evidence&legacy')
  await page.getByRole('button', { name: '查看错误详情' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('尾部根因-A')
  await dialog.getByRole('button', { name: '复制全部', exact: true }).click()
  expect(await copied(page)).toContain(raw('A'))
  expect(queries).toHaveLength(1)
  expect(queries[0]).toContain('record_id=1')
})

for (const failure of ['404', '500', 'network']) test(`execution query ${failure} retains id and last state; recovery never retriggers`, async ({ page }) => {
  let triggers = 0, reads = 0
  await page.route('**/api/v1/account/execute/7', route => { triggers++; return route.fulfill({ json: { execution_id: 'same-execution' } }) })
  await page.route('**/api/v1/account/executions/same-execution', route => {
    if (++reads === 2) return failure === 'network' ? route.abort() : route.fulfill({ status: Number(failure), json: { detail: raw('查询') } })
    return route.fulfill({ json: { execution_id: 'same-execution', status: reads === 1 ? 'RUNNING' : 'SUCCEEDED', output_status: 'SUCCEEDED' } })
  })
  await page.goto('/e2e/errors.html?mode=runner')
  await page.getByRole('button', { name: '立即执行', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: '状态待确认' })).toBeVisible()
  await expect(page.getByTestId('runner')).toContainText('"status":"RUNNING"')
  await expect(page.getByTestId('runner')).toContainText('same-execution')
  await expect(page.getByRole('button', { name: '立即执行', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '重试：状态待确认' }).click()
  await expect(page.getByRole('button', { name: '立即执行', exact: true })).toBeEnabled()
  expect(triggers).toBe(1)
  expect(reads).toBe(3)
})

test('account PATCH pending keeps saved switch, ignores older revision, and separates GET from retry', async ({ page }) => {
  await defaults(page)
  let saved = false, queries = 0, retries = 0
  await page.route('**/api/v1/account/7', route => {
    if (route.request().method() === 'PATCH') { saved = true; return route.fulfill({ status: 202, json: { ...account, is_started: true, runtime_sync: sync('pending', 2, raw('同步')) } }) }
    return route.fulfill({ json: { ...account, is_started: saved } })
  })
  await page.route('**/api/v1/account/7/runtime-sync', route => { queries++; return route.fulfill({ json: sync('synchronized', 1) }) })
  await page.route('**/api/v1/account/7/runtime-sync/retry', route => { retries++; return route.fulfill({ json: sync('synchronized', 2) }) })
  await page.goto('/e2e/errors.html?mode=account')
  await page.getByRole('button', { name: '启动自动执行', exact: true }).click()
  await expect(page.getByText('配置已保存，运行态待同步', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '暂停自动执行', exact: true })).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: '运行态同步未完成' })).toContainText('同步开始')
  const before = queries
  await page.getByRole('button', { name: '重新查询', exact: true }).click()
  await expect.poll(() => queries).toBe(before + 1)
  await expect(page.getByText('配置已保存，运行态待同步', { exact: true })).toBeVisible()
  expect(retries).toBe(0)
  await page.getByRole('button', { name: '重试同步', exact: true }).click()
  await expect.poll(() => retries).toBe(1)
  await expect(page.getByText('配置已保存，运行态待同步', { exact: true })).toHaveCount(0)
})

test('account restores runtime failure on entry and all holdings sources retain separate retries', async ({ page }) => {
  await defaults(page)
  await page.route('**/api/v1/account/7/runtime-sync', route => route.fulfill({ json: sync('failed', 3, raw('恢复')) }))
  await page.goto('/e2e/errors.html?mode=account')
  await expect(page.getByText('配置已保存，运行态同步失败', { exact: true })).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: '运行态同步未完成' })).toContainText('恢复开始')
  const reads: Record<string, number> = {}
  for (const source of ['asset_snapshots', 'target_snapshot', 'rebalance_plan']) await page.route(`**/api/v1/account/7/${source}*`, route => {
    reads[source] = (reads[source] ?? 0) + 1
    return route.fulfill({ status: 500, json: { detail: `${source}: ${raw(source)}` } })
  })
  await page.goto('/e2e/errors.html?mode=holdings')
  for (const title of ['资产快照读取失败', '目标快照读取失败', '调仓计划读取失败']) await expect(page.getByRole('status').filter({ hasText: title })).toBeVisible()
  await expect(page.getByText('尚无目标权重', { exact: false })).toHaveCount(0)
  const original = { ...reads }
  await page.getByRole('button', { name: '重试：调仓计划读取失败' }).click()
  await expect.poll(() => reads.rebalance_plan).toBe(original.rebalance_plan + 1)
  expect(reads.asset_snapshots).toBe(original.asset_snapshots)
  expect(reads.target_snapshot).toBe(original.target_snapshot)
})

for (const failed of [false, true]) test(`asset refresh settles independently of an automatic toggle, failed=${failed}`, async ({ page }) => {
  await defaults(page)
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  let refreshes = 0
  await page.route('**/api/v1/account/7/assets/refresh', async route => {
    if (++refreshes === 1) {
      await pending
      if (failed) return route.fulfill({ status: 502, json: { detail: raw('资产刷新') } })
    }
    return route.fulfill({ json: { assets: { total_asset: 100 }, created_at: '2026-10-14T10:00:00' } })
  })
  await page.route('**/api/v1/account/7', route => route.fulfill({ json: { ...account, is_started: route.request().method() === 'PATCH', runtime_sync: sync() } }))
  await page.goto('/e2e/errors.html?mode=account')
  await page.getByRole('button', { name: '刷新账户资产', exact: true }).click()
  await expect.poll(() => refreshes).toBe(1)
  await expect(page.getByRole('button', { name: '正在查询账户资产', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '启动自动执行', exact: true }).click()
  await expect(page.getByRole('button', { name: '暂停自动执行', exact: true })).toBeEnabled()
  release()
  const refresh = page.getByRole('button', { name: '刷新账户资产', exact: true })
  await expect(refresh).toBeEnabled()
  const notice = page.getByRole('alert').filter({ hasText: '账户资产刷新失败' })
  if (failed) {
    await expect(notice).toContainText('资产刷新开始')
    await notice.getByRole('button', { name: '重试：账户资产刷新失败' }).click()
  } else await refresh.click()
  await expect.poll(() => refreshes).toBe(2)
  await expect(refresh).toBeEnabled()
  await expect(notice).toHaveCount(0)
})

test('a late asset request cannot clear the new account loading state or display its old failure', async ({ page }) => {
  await defaults(page)
  const release = new Map<string, () => void>()
  await page.route(/\/api\/v1\/account\/[78]\/assets\/refresh$/, async route => {
    const id = route.request().url().split('/').at(-3)!
    await new Promise<void>(resolve => { release.set(id, resolve) })
    return id === '7' ? route.fulfill({ status: 502, json: { detail: raw('旧账户资产') } }) : route.fulfill({ json: { assets: { total_asset: 100 } } })
  })
  await page.route('**/api/v1/account/8', route => route.fulfill({ json: { ...account, id: 8, name: '合成账户 B' } }))
  await page.goto('/e2e/errors.html?mode=account')
  await page.getByRole('button', { name: '刷新账户资产', exact: true }).click()
  await expect.poll(() => release.has('7')).toBe(true)
  await page.getByRole('button', { name: '切换账户', exact: true }).click()
  await page.getByRole('button', { name: '刷新账户资产', exact: true }).click()
  await expect.poll(() => release.has('8')).toBe(true)
  release.get('7')!()
  await expect(page.getByRole('button', { name: '正在查询账户资产', exact: true })).toBeDisabled()
  release.get('8')!()
  await expect(page.getByRole('button', { name: '刷新账户资产', exact: true })).toBeEnabled()
  await expect(page.getByText('旧账户资产开始', { exact: false })).toHaveCount(0)
})

test('batch execution keeps accepted ids and retries only the rejected account', async ({ page }) => {
  await defaults(page)
  const counts: Record<string, number> = {}
  await page.route('**/api/v1/account/execute/*', route => {
    const id = route.request().url().split('/').at(-1)!
    counts[id] = (counts[id] ?? 0) + 1
    return id === '8' && counts[id] === 1 ? route.fulfill({ status: 409, json: { detail: raw('账户B拒绝') } })
      : route.fulfill({ json: { execution_id: `accepted-${id}`, accepted: 'queued' } })
  })
  await page.goto('/e2e/errors.html?mode=portfolio')
  await page.getByRole('button', { name: '立即执行', exact: true }).click()
  await page.getByRole('button', { name: '通知执行', exact: true }).click()
  const results = page.getByRole('region', { name: '批量执行受理结果', exact: true })
  await expect(results).toContainText('合成账户 A · 已受理')
  await expect(results).toContainText('accepted-7')
  await expect(results).toContainText('合成账户 B · 受理失败')
  await results.getByRole('button', { name: '查看批量结果详情' }).click()
  const dialog = page.getByRole('dialog', { name: '批量执行受理结果' })
  await expect(dialog).toContainText('accepted-7')
  await expect(dialog).toContainText('尾部根因-账户B拒绝')
  await dialog.getByRole('button', { name: '复制全部结果' }).click()
  const original = await copied(page)
  expect(original).toContain('accepted-7')
  expect(original).toContain(raw('账户B拒绝'))
  await results.getByRole('button', { name: '重试：合成账户 B 执行请求失败' }).evaluate((node: HTMLButtonElement) => node.click())
  await expect(results).toContainText('accepted-8')
  await expect(dialog).not.toContainText('accepted-8')
  await dialog.getByRole('button', { name: '已复制', exact: true }).click()
  expect(await copied(page)).toBe(original)
  await page.keyboard.press('Escape')
  await results.getByRole('button', { name: '查看批量结果详情' }).click()
  await expect(dialog).toContainText('accepted-8')
  await expect(dialog).not.toContainText('尾部根因-账户B拒绝')
  expect(counts).toEqual({ '7': 1, '8': 2 })
})

test('batch details include all rejected accounts and complete originals in one copy', async ({ page }) => {
  await defaults(page)
  await page.route('**/api/v1/account/execute/*', route => {
    const id = route.request().url().split('/').at(-1)!
    return route.fulfill({ status: 409, json: { detail: { message: raw(`账户${id}`), code: `REJECTED_${id}`, request_id: `req-${id}` } } })
  })
  await page.goto('/e2e/errors.html?mode=portfolio')
  await page.getByRole('button', { name: '立即执行', exact: true }).click()
  await page.getByRole('button', { name: '通知执行', exact: true }).click()
  const results = page.getByRole('region', { name: '批量执行受理结果', exact: true })
  await expect(results).toContainText('合成账户 A · 受理失败')
  await expect(results).toContainText('合成账户 B · 受理失败')
  await results.getByRole('button', { name: '查看批量结果详情' }).click()
  const dialog = page.getByRole('dialog', { name: '批量执行受理结果' })
  await dialog.getByRole('button', { name: '复制全部结果' }).click()
  const text = await copied(page)
  for (const id of ['7', '8']) {
    expect(text).toContain(raw(`账户${id}`))
    expect(text).toContain(`REJECTED_${id}`)
    expect(text).toContain(`req-${id}`)
  }
})

test('pending batch copies identify their incomplete scope and stay frozen after the last request settles', async ({ page }) => {
  await defaults(page)
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/account/execute/*', async route => {
    const id = route.request().url().split('/').at(-1)!
    if (id === '8') await pending
    return route.fulfill({ json: { execution_id: `accepted-${id}`, accepted: 'created' } })
  })
  await page.goto('/e2e/errors.html?mode=portfolio')
  await page.getByRole('button', { name: '立即执行', exact: true }).click()
  await page.getByRole('button', { name: '通知执行', exact: true }).click()
  const results = page.getByRole('region', { name: '批量执行受理结果', exact: true })
  await expect(results).toContainText('accepted-7')
  await results.getByRole('button', { name: '查看批量结果详情' }).click()
  const dialog = page.getByRole('dialog', { name: '批量执行受理结果' })
  await expect(dialog).toContainText('1 个仍在提交，结果尚未齐全')
  await dialog.getByRole('button', { name: '复制当前结果' }).click()
  expect(await copied(page)).toContain('受理结果待确认')
  release()
  await expect(results).toContainText('accepted-8')
  await expect(dialog).not.toContainText('accepted-8')
  await expect(dialog.getByRole('button', { name: '复制全部结果' })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await results.getByRole('button', { name: '查看批量结果详情' }).click()
  await expect(dialog.getByRole('button', { name: '复制全部结果' })).toBeVisible()
  await expect(dialog).toContainText('accepted-8')
})

for (const background of [true, false]) test(`quick fix failure preserves diagnostics and supports complete details and actual retry, background=${background}`, async ({ page }) => {
  let failed = true
  await quickFixLsp(page, background, () => failed)
  await page.route('**/api/v1/editor/contract', route => route.fulfill({ json: { diagnostics: [], fixes: [] } }))
  await page.goto('/e2e/errors.html?mode=python')
  const panel = page.getByRole('region', { name: '代码检查' })
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready')
  if (!background) {
    await page.locator('.cm-content').first().click()
    await page.keyboard.press('ControlOrMeta+.')
    await expect(page.getByRole('menu', { name: '快速修复', exact: true })).toContainText('快速修复暂不可用')
    await expect(page.getByRole('menu')).not.toContainText('当前位置没有可用的快速修复')
    await page.keyboard.press('Escape')
  }
  const notice = panel.getByRole('status').filter({ hasText: '快速修复暂不可用' })
  await expect(notice).toContainText('快速修复开始')
  await expect(panel).toContainText(`LSP 诊断：检查完成 · ${background ? 1 : 0} 条诊断`)
  await expect(panel).toContainText('入口契约检查：检查完成')
  if (background) await expect(panel).toContainText('仍然有效的 LSP 诊断')
  await notice.getByRole('button', { name: '查看错误详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('尾部根因-快速修复')
  await page.getByRole('dialog').getByRole('button', { name: '复制错误信息' }).click()
  expect(await copied(page)).toContain(raw('快速修复'))
  await page.keyboard.press('Escape')
  failed = false
  await notice.getByRole('button', { name: '重试：快速修复暂不可用' }).click()
  await expect(page.getByRole('menu', { name: '快速修复', exact: true })).toContainText('当前位置没有可用的快速修复')
  await expect(notice).toHaveCount(0)
  if (background) await expect(panel).toContainText('仍然有效的 LSP 诊断')
})

test('advanced configuration read failure remains on page with full error and retry', async ({ page }) => {
  let reads = 0
  await page.route('**/api/v1/init/status', route => { reads++; return route.fulfill({ status: 500, json: { detail: raw('配置') } }) })
  await page.goto('/e2e/errors.html?mode=system')
  await expect(page.getByRole('status')).toContainText('系统配置读取失败')
  await page.getByRole('button', { name: '查看错误详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('尾部根因-配置')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '重试：系统配置读取失败' }).click()
  await expect.poll(() => reads).toBe(2)
})

test('static contract failure retains LSP diagnostics status and never claims no diagnostics', async ({ page }) => {
  await page.route('**/api/v1/editor/contract', route => route.fulfill({ status: 500, json: { detail: raw('契约') } }))
  await page.goto('/e2e/errors.html?mode=python')
  const panel = page.getByRole('region', { name: '代码检查' })
  await expect(panel).toContainText('入口契约检查：检查失败')
  await expect(panel).toContainText('LSP 诊断：检查完成')
  await expect(panel).not.toContainText('暂无静态诊断')
  await panel.getByRole('button', { name: '查看错误详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('尾部根因-契约')
})

test('late static failure from an older document cannot replace a successful current check', async ({ page }) => {
  let release!: () => void
  const old = new Promise<void>(resolve => { release = resolve })
  let oldRead = false
  await page.route('**/api/v1/editor/contract', async route => {
    const code = route.request().postDataJSON().code as string
    if (code.includes('value = 1')) { oldRead = true; await old; return route.fulfill({ status: 500, json: { detail: '旧文档失败' } }) }
    return route.fulfill({ json: { diagnostics: [], fixes: [] } })
  })
  await page.goto('/e2e/errors.html?mode=python')
  await expect.poll(() => oldRead).toBe(true)
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText('value = 2\n')
  await expect(page.getByRole('button', { name: '代码问题', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '代码问题', exact: true }).click()
  await expect(page.getByText('暂无静态诊断。', { exact: true })).toBeVisible()
  release()
  await expect(page.getByText('暂无静态诊断。', { exact: true })).toBeVisible()
  await expect(page.getByText('旧文档失败', { exact: false })).toHaveCount(0)
})

test('LSP failure leaves valid contract diagnostics visible with an explicit failed source', async ({ page }) => {
  await page.routeWebSocket('**/api/v1/editor/lsp', socket => socket.close({ code: 1011, reason: 'synthetic LSP failure' }))
  await page.route('**/api/v1/editor/contract', route => route.fulfill({ json: { diagnostics: [{ message: '有效契约诊断', source: '入口契约', severity: 2, range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } } }], fixes: [] } }))
  await page.goto('/e2e/errors.html?mode=python')
  const panel = page.getByRole('region', { name: '代码检查' })
  await expect(panel).toContainText('LSP 诊断：检查失败')
  await expect(panel).toContainText('入口契约检查：检查完成 · 1 条诊断')
  await expect(panel).toContainText('有效契约诊断')
  await expect(panel).not.toContainText('暂无静态诊断')
})

test('switching execution while A is pending invalidates its late response and opens B independently', async ({ page }) => {
  let release!: () => void
  const waiting = new Promise<void>(resolve => { release = resolve })
  let readingA = false
  await page.route('**/api/v1/account/**', async route => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/execute_records/')) {
      const id = url.searchParams.get('execution_id')!
      if (id === 'A') { readingA = true; await waiting }
      return route.fulfill({ json: { data: [record(id)], count: 1 } }).catch(() => {})
    }
    if (/\/(events|artifacts)$/.test(url.pathname)) return route.fulfill({ json: { data: [], count: 0 } })
    return route.fulfill({ json: { execution_id: url.pathname.split('/').at(-1), account_id: 7, status: 'FAILED' } })
  })
  await page.goto('/e2e/errors.html?mode=evidence')
  await page.getByRole('button', { name: '查看错误详情' }).click()
  await expect.poll(() => readingA).toBe(true)
  await page.getByRole('button', { name: '切换执行' }).evaluate((node: HTMLButtonElement) => node.click())
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: '查看错误详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('尾部根因-B')
  release()
  await expect(page.getByRole('dialog')).not.toContainText('尾部根因-A')
  await page.getByRole('dialog').getByRole('button', { name: '复制全部', exact: true }).click()
  expect(await copied(page)).toContain(raw('B'))
})

test('a comparison-only failure has its own error and retry without hiding successful snapshot sources', async ({ page }) => {
  await defaults(page)
  let reads = 0
  await page.route('**/api/v1/account/7/rebalance_plan', route => {
    reads++
    return route.fulfill({ status: 409, json: { detail: raw('comparison') } })
  })
  await page.goto('/e2e/errors.html?mode=holdings')
  const notice = page.getByRole('status').filter({ hasText: '调仓计划读取失败' })
  await expect(notice).toContainText('comparison开始')
  await expect(page.getByRole('status').filter({ hasText: '资产快照读取失败' })).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: '目标快照读取失败' })).toHaveCount(0)
  await notice.getByRole('button', { name: '查看错误详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('尾部根因-comparison')
  await page.keyboard.press('Escape')
  await notice.getByRole('button', { name: '重试：调仓计划读取失败' }).click()
  await expect.poll(() => reads).toBe(2)
})


test('账户连接标识读取后端三态，连接错误集中展示，业务错误保留原位置', async ({ page }) => {
  await defaults(page)
  let state: 'unknown' | 'ok' | 'error' = 'unknown'
  let business = false
  const connection = () => ({ status: state, observed_at: state === 'unknown' ? null : '2026-10-14T14:47:18', message: state === 'error' ? '更新令牌错误' : null })
  await page.route('**/api/v1/account/7', route => route.fulfill({ json: { ...account, connection: connection() } }))
  await page.route('**/api/v1/account/7/connection', route => route.fulfill({ json: { ...connection(), error: state === 'error' ? { technical_detail: raw('GM1026'), traceback: raw('GM1026') } : null } }))
  await page.route('**/api/v1/account/7/assets/refresh', route => {
    if (business) return route.fulfill({ status: 502, json: { detail: { message: '资金查询业务拒绝' } } })
    state = 'error'
    return route.fulfill({ status: 502, json: { detail: { message: '更新令牌错误', error_scope: 'account_connection' } } })
  })
  await page.goto('/e2e/errors.html?mode=account')
  await expect(page.getByRole('button', { name: '账户连通状态：未知' })).toBeVisible()
  await page.getByRole('button', { name: '刷新账户资产' }).click()
  await expect(page.getByRole('button', { name: '账户连通状态：异常' })).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: '账户资产刷新失败' })).toContainText('更新令牌错误')
  await page.getByRole('button', { name: '账户连通状态：异常' }).click()
  const modal = page.getByRole('dialog', { name: '账户连接异常' })
  await expect(modal.getByRole('button', { name: '技术详情' })).toHaveAttribute('aria-expanded', 'false')
  await modal.getByRole('button', { name: '技术详情' }).click()
  await expect(modal).toContainText('尾部根因-GM1026')
  await modal.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain('尾部根因-GM1026')
  await modal.getByRole('button', { name: '关闭详情' }).click()
  state = 'ok'
  business = true
  await page.reload()
  await expect(page.getByRole('status', { name: '账户连通状态：正常' })).toBeVisible()
  await page.getByRole('button', { name: '刷新账户资产' }).click()
  await expect(page.getByText('资金查询业务拒绝', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(page.getByRole('status', { name: '账户连通状态：正常' })).toBeVisible()
})

test('主动目标计算保留连接错误反馈，脚本错误不改变连接标识', async ({ page }) => {
  await defaults(page)
  let failed = false
  let business = false
  await page.route('**/api/v1/account/7', route => route.fulfill({ json: { ...account, connection: { status: failed ? 'error' : 'ok', observed_at: '2026-10-14T14:47:18', message: failed ? '更新令牌错误' : null } } }))
  await page.route('**/api/v1/account/7/target_snapshot/refresh', route => {
    if (business) return route.fulfill({ status: 500, json: { detail: { message: '脚本第2行业务错误' } } })
    failed = true
    return route.fulfill({ status: 500, json: { detail: { message: '更新令牌错误', error_scope: 'account_connection' } } })
  })
  await page.goto('/e2e/errors.html?mode=account')
  await page.getByRole('button', { name: '重新计算当前目标权重', exact: true }).click()
  await expect(page.getByRole('button', { name: '账户连通状态：异常' })).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: '目标计算失败' })).toContainText('更新令牌错误')
  failed = false
  business = true
  await page.reload()
  await page.getByRole('button', { name: '重新计算当前目标权重', exact: true }).click()
  await expect(page.getByText('脚本第2行业务错误', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(page.getByRole('status', { name: '账户连通状态：正常' })).toBeVisible()
})

const problem = (name = 'A') => ({ status: 'error', observed_at: '2026-10-14T14:47:18', label: `读取标签-${name}`, message: `原始说明-${name}`,
  presentation: { title: `本次连接问题-${name}`, summary: `已确认的说明-${name}`, sections: [{ title: '现场事实', text: '渠道自由说明', fields: [{ label: '查找结果', value: `本次事实-${name}` }] }, { title: '检查顺序', steps: ['核对账户', '检查访问权限'] }], actions: [{ label: '检查连接设置', intent: 'open_connection_settings' }] },
  error: { technical_detail: raw(name), traceback: raw(name) } })

test('connection problem displays channel blocks, freezes one read and copies full folded evidence', async ({ page }) => {
  let reads = 0, name = 'A'
  await page.route('**/api/v1/account/7/connection', route => { reads++; return route.fulfill({ json: problem(name) }) })
  await page.goto('/e2e/errors.html?mode=connection')
  const badge = page.getByRole('button', { name: '账户连通状态：异常，测试渠道的新故障文案', exact: true })
  await badge.click()
  const dialog = page.getByRole('dialog', { name: '本次连接问题-A' })
  await expect(dialog).toContainText('本次事实-A')
  await expect(dialog).toContainText('检查顺序')
  await expect(dialog.getByRole('button', { name: '技术详情' })).toHaveAttribute('aria-expanded', 'false')
  await expect(dialog.getByRole('button', { name: '完整技术详情' })).toHaveCount(0)
  await expect(dialog.getByLabel('完整技术详情')).not.toBeVisible()
  name = 'B'
  await page.getByRole('button', { name: '后台更新标签' }).evaluate((node: HTMLButtonElement) => node.click())
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain(raw('A'))
  expect(await copied(page)).toContain('读取标签-A')
  expect(await copied(page)).not.toContain('读取标签-B')
  await dialog.getByRole('button', { name: '技术详情' }).click()
  await expect(dialog.getByLabel('完整技术详情')).toContainText('尾部根因-A')
  expect(reads).toBe(1)
  await page.keyboard.press('Escape')
  const updated = page.getByRole('button', { name: '账户连通状态：异常，后台更新后的标签', exact: true })
  await expect(updated).toBeFocused()
  await updated.click()
  await expect(page.getByRole('dialog', { name: '本次连接问题-B' })).toBeVisible()
  await expect(page.getByRole('button', { name: '技术详情' })).toHaveAttribute('aria-expanded', 'false')
  expect(reads).toBe(2)
})

for (const label of ['', '   ']) test(`blank connection label falls back; missing presentation keeps dedicated problem dialog: ${JSON.stringify(label)}`, async ({ page }) => {
  await page.route('**/api/v1/account/7/connection', route => route.fulfill({ json: { ...problem(), label, presentation: null } }))
  await page.goto(`/e2e/errors.html?mode=connection&label=${encodeURIComponent(label)}`)
  const badge = page.getByRole('button', { name: '账户连通状态：异常', exact: true })
  await expect(badge).toContainText('连接异常')
  await badge.click()
  const dialog = page.getByRole('dialog', { name: '账户连接异常' })
  await expect(dialog).toContainText('原始说明-A')
  await expect(dialog.getByRole('button', { name: '技术详情' })).toHaveAttribute('aria-expanded', 'false')
  await expect(dialog.getByRole('link')).toHaveCount(0)
})

for (const status of ['ok', 'unknown']) test(`connection response ${status} hides obsolete channel suggestions`, async ({ page }) => {
  await page.route('**/api/v1/account/7/connection', route => route.fulfill({ json: { ...problem(), status } }))
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  const dialog = page.getByRole('dialog', { name: status === 'ok' ? '连接正常' : '连接未知' })
  await expect(dialog).toBeVisible()
  await expect(dialog).not.toContainText('检查顺序')
  await expect(dialog).not.toContainText('现场事实')
  await expect(dialog.getByRole('button', { name: '技术详情' })).toHaveCount(0)
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).not.toContain(raw('A'))
})

test('connection read failure keeps account state and exposes only request evidence', async ({ page }) => {
  await page.route('**/api/v1/account/7/connection', route => route.fulfill({ status: 500, json: { detail: raw('读取失败') } }))
  await page.goto('/e2e/errors.html?mode=connection')
  const badge = page.getByRole('button', { name: /账户连通状态/ })
  await badge.click()
  const dialog = page.getByRole('dialog', { name: '连接详情读取失败' })
  await expect(dialog).toContainText('未能读取账户 7')
  await expect(dialog.getByRole('link')).toHaveCount(0)
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain(raw('读取失败'))
  await page.keyboard.press('Escape')
  await expect(badge).toContainText('测试渠道的新故障文案')
})

for (const switchAccount of [false, true]) test(`late connection response ignored after ${switchAccount ? 'account switch' : 'close and reopen'}`, async ({ page }) => {
  let release!: () => void, reads = 0
  const delayed = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/account/*/connection', async route => {
    const first = ++reads === 1
    if (first) await delayed
    await route.fulfill({ json: problem(first ? '迟到A' : '当前B') })
  })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  await expect(page.getByRole('status')).toContainText('正在读取')
  if (switchAccount) await page.getByRole('button', { name: '切换连接账户' }).evaluate((node: HTMLButtonElement) => node.click())
  else await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  const dialog = page.getByRole('dialog', { name: '本次连接问题-当前B' })
  await expect(dialog).toBeVisible()
  release()
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).not.toContain('迟到A')
  expect(await copied(page)).toContain(`账户：${switchAccount ? 8 : 7}`)
})

test('connection generic settings action targets current account and ignores malformed blocks and unknown intents', async ({ page }) => {
  const response = problem()
  await page.route('**/api/v1/account/*/connection', route => route.fulfill({ json: { ...response, presentation: { ...response.presentation,
    sections: [null, { title: '空块' }, ...response.presentation.sections], actions: [{ label: '运行脚本', intent: 'execute_script' }, ...response.presentation.actions] } } }))
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: '切换连接账户' }).click()
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).not.toContainText('空块')
  await expect(dialog.getByRole('link', { name: '运行脚本' })).toHaveCount(0)
  const link = dialog.getByRole('link', { name: '检查连接设置' })
  await expect(link).toHaveAttribute('href', '/accounts/8/edit/connection')
  await link.click()
  await expect(page.getByText('已离开测试上下文')).toBeVisible()
})

test('long custom connection label, narrow dialog, reduced motion and keyboard focus remain usable', async ({ page }) => {
  const label = 'NewChannelDisconnected'.repeat(10)
  await page.setViewportSize({ width: 375, height: 620 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/v1/account/7/connection', route => route.fulfill({ json: { ...problem(), label, presentation: null } }))
  await page.goto(`/e2e/errors.html?mode=connection&label=${encodeURIComponent(label)}`)
  const badge = page.getByRole('button', { name: `账户连通状态：异常，${label}`, exact: true })
  await badge.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: label, exact: true })
  await expect(dialog.getByRole('button', { name: '复制连接详情' })).toBeInViewport()
  await expect(dialog.getByRole('button', { name: '关闭详情' })).toBeInViewport()
  await page.keyboard.press('Shift+Tab')
  await expect(dialog.getByRole('button', { name: '关闭详情' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.keyboard.press('Escape')
  await expect(badge).toBeFocused()
  await expect(badge.locator('.overflow-text').first()).toHaveAttribute('data-overflow', 'true')
})
