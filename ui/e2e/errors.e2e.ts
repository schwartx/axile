import { expect, test, type Page } from '@playwright/test'

const raw = (name: string) => `${name}开始\n${'synthetic error '.repeat(100)}\nTraceback\n  script.py:123\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因-${name}`
const record = (id = 'A') => ({ id: 1, account_id: 7, execution_id: id, raw_input: {}, raw_result: { status: 'FAILED', technical_detail: raw(id), traceback: raw(id) }, is_success: 0, created_at: '2026-10-14T10:00:00' })
const sync = (status = 'synchronized', revision = 1, last_error: string | null = null) => ({ status, revision, last_error, attempts: 1, last_attempt_at: null, synchronized_at: null })
const account = { id: 7, name: '合成账户 A', market: 'demo', trade_channel: 'paper', is_started: false, cron_expr: '', portfolio_id: 9, algorithm: { method: 'SINGLE-MAKER', params: {} }, empty_positions_algorithm: null, account_config: {}, forbidden_symbols: [], risk_symbols: [], trade_rules: [], long_leverage: 1, short_leverage: 1, weight_precision: .001 }
const snapshot = { portfolio_id: 9, context_account_id: 7, weights: {}, calculated_at: '2026-10-14T10:00:00', sizing: null }
const dashboardItem = { account_id: 7, name: account.name, remark: null, market: 'demo', trade_channel: 'paper', is_started: false,
  portfolio_id: 9, is_scheduled: false, next_run_time: null, total_asset: 100, currency: 'CNY', holdings_count: 0, position_weights: [],
  last_is_success: null, last_exec_at: null, last_output_status: null, off_symbol_count: null,
  connection: { status: 'ready', observed_at: null, message: null } }

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
async function mockConnectionSnapshot(page: Page, connection: Record<string, unknown>) {
  await page.addInitScript(value => { (window as unknown as { initialConnection: unknown }).initialConnection = value }, connection)
}
async function pushConnection(page: Page, connection: Record<string, unknown>, accountId = 7) {
  await page.evaluate(async ({ connection, accountId }) => {
    const modulePath = '/src/stores/liveConnection.ts'
    const { useLiveConnection } = await import(modulePath)
    const state = useLiveConnection.getState()
    state.apply({ stream_id: 'fixture', version: state.version + 1, kind: state.connected ? 'update' : 'snapshot',
      data: [{ account_id: accountId, connection }] })
  }, { connection, accountId })
}
async function copied(page: Page) { return page.evaluate(() => (window as unknown as { copied: string }).copied) }

async function setLiveExecution(page: Page, status: string, kind = 'rebalance') {
  await page.evaluate(async ({ status, kind }) => {
    const modulePath = '/src/stores/liveExec.ts'
    const { useLiveExecStore } = await import(modulePath)
    useLiveExecStore.getState().applyEvent({ account_id: 7, execution_id: 'pause-feedback', kind, phase: 'executing', status })
  }, { status, kind })
}

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
  await expect(page.getByRole('button', { name: '查看服务连接错误详情' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: '查看错误详情', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^重试：/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '重试同步', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '重新计算当前目标权重', exact: true })).toBeDisabled()
  await expect(page.getByText('暂不可用', { exact: true }).filter({ visible: true }).first()).toBeVisible()
  const previous = { ...reads }
  offline = false
  await page.getByRole('button', { name: '重试连接服务器' }).click()
  await expect(page.getByRole('banner')).toContainText('服务正常')
  await expect(page.getByRole('switch', { name: '账户连接' })).toBeVisible()
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
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  let offline = false, patches = 0
  await page.route('**/api/v1/**', route => {
    if (route.request().method() === 'PATCH') patches++
    return offline ? route.abort() : route.fallback()
  })
  await page.goto('/e2e/errors.html?mode=outage')
  await expect(page.getByRole('switch', { name: '账户连接' })).toBeVisible()
  await expect(page.getByRole('button', { name: '刷新账户资产', exact: true })).toBeVisible()
  offline = true
  await page.evaluate(async () => {
    const modulePath = '/src/stores/liveConnection.ts'
    const { useLiveConnection } = await import(modulePath)
    useLiveConnection.getState().disconnect()
  })
  await page.getByRole('button', { name: '刷新页面读取' }).click()
  await expect(page.getByRole('banner')).toContainText('与服务器失联')
  await expect(page.getByRole('switch', { name: '账户连接' })).toBeDisabled()
  await expect(page.getByText(/当前显示 .*的数据/).filter({ visible: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '刷新账户资产', exact: true })).toBeVisible()
  const start = page.getByRole('button', { name: '启动自动执行', exact: true })
  await expect(start).toBeEnabled()
  await start.click()
  await expect(page.getByRole('alert').filter({ hasText: '自动执行配置保存失败' })).toContainText('无法连接 axile 服务')
  expect(patches).toBe(1)
  offline = false
  await pushConnection(page, { status: 'ready', observed_at: null, message: null, error: null })
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
  await expect(name).toHaveValue('仍未保存的组合草稿')
  offline = false
  await page.getByRole('button', { name: '重试连接服务器' }).click()
  await expect(page.getByRole('banner')).toContainText('服务正常')
  await expect(name).toHaveValue('仍未保存的组合草稿')
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

test('inline details close when the enclosing modal query recovers', async ({ page }) => {
  await page.goto('/e2e/errors.html?mode=embedded')
  const dialog = page.getByRole('dialog', { name: '已有业务弹窗' })
  await dialog.getByRole('button', { name: '查看错误详情' }).click()
  await expect(dialog).toContainText('尾部根因-A')
  await dialog.getByRole('button', { name: '复制错误信息' }).click()
  expect(await copied(page)).toContain(raw('A'))
  await dialog.getByRole('button', { name: '后台查询恢复成功' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(1)
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

test('pausing an active execution explains continued trading after save and retracts on resume or completion', async ({ page }) => {
  await defaults(page)
  await page.setViewportSize({ width: 375, height: 812 })
  let started = false, pauseRequests = 0
  let release!: () => void
  const pauseSaved = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/account/7', async route => {
    if (route.request().method() === 'PATCH') {
      const next = route.request().postDataJSON().is_started
      if (!next) { pauseRequests++; await pauseSaved }
      started = next
      return route.fulfill({ json: { ...account, is_started: started, runtime_sync: sync() } })
    }
    return route.fulfill({ json: { ...account, is_started: started } })
  })
  await page.goto('/e2e/errors.html?mode=account')
  const notice = page.getByRole('status').filter({ hasText: '自动执行已暂停，本次调仓仍会继续，仍可能下单和成交。' })
  await expect(notice).toHaveCount(0)
  await page.getByRole('button', { name: '启动自动执行', exact: true }).click()
  await expect(page.getByRole('button', { name: '暂停自动执行', exact: true })).toBeEnabled()
  await setLiveExecution(page, 'running')
  await expect(notice).toHaveCount(0)
  await page.getByRole('button', { name: '暂停自动执行', exact: true }).click()
  await expect.poll(() => pauseRequests).toBe(1)
  await expect(notice).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: '正在暂停自动执行，本次调仓仍在执行，仍可能下单和成交。' })).toBeVisible()
  release()
  await expect(notice).toBeVisible()
  await expect(page.getByRole('button', { name: '终止执行', exact: true })).toBeVisible()
  const status = page.getByRole('link', { name: /正在执行/ })
  await expect(status).toBeVisible()
  expect((await notice.boundingBox())!.y).toBeGreaterThan((await status.boundingBox())!.y)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('button', { name: '启动自动执行', exact: true }).click()
  await expect(notice).toHaveCount(0)
  await expect(page.getByRole('button', { name: '暂停自动执行', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '暂停自动执行', exact: true }).click()
  await expect(notice).toBeVisible()
  await setLiveExecution(page, 'done')
  await expect(notice).toHaveCount(0)
  await expect(page.getByRole('button', { name: '启动自动执行', exact: true })).toBeVisible()
})

test('pause feedback distinguishes pending sync and clearing, and hides while queued or terminating', async ({ page }) => {
  await defaults(page)
  await page.route('**/api/v1/account/7/runtime-sync', route => route.fulfill({ json: sync('pending', 2) }))
  await page.route('**/api/v1/account/7/terminate', route => route.fulfill({ status: 202, json: {
    message: '已受理终止请求', account_id: 7, execution_id: 'pause-feedback', status: 'TERMINATING',
  } }))
  await page.goto('/e2e/errors.html?mode=account')
  await setLiveExecution(page, 'queued')
  const notice = page.getByRole('status').filter({ hasText: /本次(?:调仓|清仓)仍/ })
  await expect(notice).toHaveCount(0)
  await setLiveExecution(page, 'running', 'clear_positions')
  await expect(notice).toHaveText('暂停设置已保存，本次清仓仍在执行，仍可能下单和成交。')
  await expect(notice).toBeVisible()
  await page.getByRole('button', { name: '终止执行', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '停止并撤单', exact: true }).click()
  await expect(notice).toHaveCount(0)
  await expect(page.getByRole('button', { name: '终止中', exact: true })).toBeVisible()
  await setLiveExecution(page, 'terminating', 'clear_positions')
  await expect(notice).toHaveCount(0)
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
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  let refreshes = 0
  await page.route('**/api/v1/account/7/assets/refresh', async route => {
    if (++refreshes === 2) {
      await pending
      if (failed) return route.fulfill({ status: 502, json: { detail: raw('资产刷新') } })
    }
    return route.fulfill({ json: { assets: { total_asset: 100 }, created_at: '2026-10-14T10:00:00' } })
  })
  await page.route('**/api/v1/account/7', route => route.fulfill({ json: { ...account, is_started: route.request().method() === 'PATCH', runtime_sync: sync() } }))
  await page.goto('/e2e/errors.html?mode=account')
  await expect.poll(() => refreshes).toBe(1)
  await page.getByRole('button', { name: '刷新账户资产', exact: true }).click()
  await expect.poll(() => refreshes).toBe(2)
  await expect(page.getByRole('button', { name: '正在查询账户资产', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '启动自动执行', exact: true }).click()
  await expect(page.getByRole('button', { name: '暂停自动执行', exact: true })).toBeEnabled()
  release()
  const refresh = page.getByRole('button', { name: '刷新账户资产', exact: true })
  await expect(refresh).toBeEnabled()
  const notice = page.getByRole('button', { name: '查看资产刷新详情：刷新失败' })
  if (failed) {
    await notice.click()
    await expect(page.getByRole('dialog', { name: '资产刷新详情' })).toContainText('资产刷新开始')
    await page.keyboard.press('Escape')
    await refresh.click()
  } else await refresh.click()
  await expect.poll(() => refreshes).toBe(3)
  await expect(refresh).toBeEnabled()
  await expect(notice).toHaveCount(0)
})

test('a late asset request cannot clear the new account loading state or display its old failure', async ({ page }) => {
  await defaults(page)
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  const release = new Map<string, () => void>()
  await page.route(/\/api\/v1\/account\/[78]\/assets\/refresh$/, async route => {
    const id = route.request().url().split('/').at(-3)!
    await new Promise<void>(resolve => { release.set(id, resolve) })
    return id === '7' ? route.fulfill({ status: 502, json: { detail: raw('旧账户资产') } }) : route.fulfill({ json: { assets: { total_asset: 100 } } })
  })
  await page.route('**/api/v1/account/8', route => route.fulfill({ json: { ...account, id: 8, name: '合成账户 B' } }))
  await page.goto('/e2e/errors.html?mode=account')
  await expect.poll(() => release.has('7')).toBe(true)
  await page.getByRole('button', { name: '切换账户', exact: true }).click()
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


test('账户连接标识读取后端三态，连接错误归连接入口，资产业务错误归资产入口', async ({ page }) => {
  await defaults(page)
  let state: 'unknown' | 'ready' | 'unavailable' = 'unknown'
  let business = false
  const connection = () => ({ status: state, observed_at: state === 'unknown' ? null : '2026-10-14T14:47:18', message: state === 'unavailable' ? '更新令牌错误' : null })
  await page.route('**/api/v1/account/7', route => route.fulfill({ json: { ...account, connection: connection() } }))
  await mockConnectionSnapshot(page, { ...connection(), error: state === 'unavailable' ? { technical_detail: raw('GM1026'), traceback: raw('GM1026') } : null })
  await page.route('**/api/v1/account/7/assets/refresh', async route => {
    if (business) return route.fulfill({ status: 502, json: { detail: { message: '资金查询业务拒绝' } } })
    state = 'unavailable'
    await pushConnection(page, { ...connection(), error: { technical_detail: raw('GM1026'), traceback: raw('GM1026') } })
    return route.fulfill({ status: 502, json: { detail: { message: '更新令牌错误', error_scope: 'account_connection' } } })
  })
  await page.goto('/e2e/errors.html?mode=account')
  await expect(page.getByRole('button', { name: '连接详情，账户连通状态：待确认' })).toBeVisible()
  state = 'ready'
  await pushConnection(page, { ...connection(), error: null })
  await expect(page.getByRole('button', { name: '连接详情，账户连通状态：暂不可用' })).toBeVisible()
  await expect(page.getByRole('button', { name: /查看资产刷新详情/ })).toHaveCount(0)
  await page.getByRole('button', { name: '连接详情，账户连通状态：暂不可用' }).click()
  const modal = page.getByRole('dialog', { name: '账户连接详情' })
  await expect(modal.getByRole('button', { name: '技术详情' })).toHaveAttribute('aria-expanded', 'false')
  await modal.getByRole('button', { name: '技术详情' }).click()
  await expect(modal).toContainText('尾部根因-GM1026')
  await modal.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain('尾部根因-GM1026')
  await modal.getByRole('button', { name: '关闭详情' }).click()
  state = 'ready'
  business = true
  await mockConnectionSnapshot(page, { ...connection(), error: null })
  await page.reload()
  await expect(page.getByRole('switch', { name: '账户连接' })).toHaveAttribute('aria-checked', 'true')
  await page.getByRole('button', { name: '查看资产刷新详情：刷新失败' }).click()
  await expect(page.getByRole('dialog', { name: '资产刷新详情' })).toContainText('资金查询业务拒绝')
  await expect(page.getByRole('switch', { name: '账户连接' })).toHaveAttribute('aria-checked', 'true')
})

test('主动目标计算保留连接错误反馈，脚本错误不改变连接标识', async ({ page }) => {
  await defaults(page)
  let failed = false
  let business = false
  await page.route('**/api/v1/account/7', route => route.fulfill({ json: { ...account, connection: { status: failed ? 'unavailable' : 'ready', observed_at: '2026-10-14T14:47:18', message: failed ? '更新令牌错误' : null } } }))
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  await page.route('**/api/v1/account/7/target_snapshot/refresh', async route => {
    if (business) return route.fulfill({ status: 500, json: { detail: { message: '脚本第2行业务错误' } } })
    failed = true
    await pushConnection(page, { status: 'unavailable', observed_at: null, message: '更新令牌错误', error: null })
    return route.fulfill({ status: 500, json: { detail: { message: '更新令牌错误', error_scope: 'account_connection' } } })
  })
  await page.goto('/e2e/errors.html?mode=account')
  await page.getByRole('button', { name: '重新计算当前目标权重', exact: true }).click()
  await expect(page.getByRole('button', { name: '连接详情，账户连通状态：暂不可用' })).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: '目标计算失败' })).toContainText('更新令牌错误')
  failed = false
  business = true
  await page.reload()
  await page.getByRole('button', { name: '重新计算当前目标权重', exact: true }).click()
  await expect(page.getByText('脚本第2行业务错误', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(page.getByRole('switch', { name: '账户连接' })).toHaveAttribute('aria-checked', 'true')
})

const problem = (name = 'A') => ({ status: 'unavailable', observed_at: '2026-10-14T14:47:18', label: `读取标签-${name}`, message: `原始说明-${name}`,
  presentation: { title: `本次连接问题-${name}`, summary: `已确认的说明-${name}`, sections: [{ title: '现场事实', text: '渠道自由说明', fields: [{ label: '查找结果', value: `本次事实-${name}` }] }, { title: '检查顺序', steps: ['核对账户', '检查访问权限'] }], actions: [{ label: '检查连接设置', intent: 'open_connection_settings' }] },
  error: { technical_detail: raw(name), traceback: raw(name) } })

for (const width of [1100, 375]) test(`connection technical evidence separates callback stack and metadata at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 850 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const message = 'CTP 交易前置已断开'
  const technical = 'CTP 交易前置断线: 4097'
  const stack = '  File "spi.py", line 25, in OnFrontDisconnected\n    self.owner._disconnected("交易", reason)\n  File "ctp_execute.py", line 803, in _disconnected\n    connection_monitor(self).failed(error)\n'
  const traceback = `axile.executor.ctp.ctp_execute.CtpSessionRecoveryRequired: ${technical}\n`
  await mockConnectionSnapshot(page, { ...problem(), message,
    error: { message, technical_detail: technical, traceback, exception_type: 'CtpSessionRecoveryRequired',
      error_scope: 'account_connection', reason_code: '4097', reporting_stack: stack,
      traceback_unavailable_reason: '该异常在首次上报时尚未抛出，没有异常调用栈。', extra: { source: 'callback' } } })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  const dialog = page.getByRole('dialog')
  const content = dialog.getByRole('region', { name: '完整技术详情' })
  await expect(content).not.toBeVisible()
  await dialog.getByRole('button', { name: '技术详情', exact: true }).click()
  await expect(content.getByText(message, { exact: true })).toHaveCount(1)
  await expect(content.getByText('异常类型', { exact: true })).toBeVisible()
  await expect(content.getByText('4097', { exact: true })).toBeVisible()
  await expect(content.getByText('账户连接', { exact: true })).toBeVisible()
  await expect(content.getByText('该异常在首次上报时尚未抛出，没有异常调用栈。')).toBeVisible()
  await expect(content.getByRole('heading', { name: '原始异常栈' })).toHaveCount(0)
  const code = content.getByLabel('上报现场栈', { exact: true })
  expect(await code.textContent()).toBe(stack)
  await content.getByRole('button', { name: '复制上报现场栈' }).click()
  expect(await copied(page)).toBe(stack)
  await expect(content.getByLabel('extra')).toContainText('callback')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await expect(dialog.getByRole('button', { name: '关闭详情' })).toBeInViewport()
  await page.screenshot({ path: testInfo.outputPath(`connection-technical-${width}.png`) })
  await dialog.getByRole('button', { name: '技术详情', exact: true }).click()
  await expect(content).not.toBeVisible()
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  const full = await copied(page)
  expect(full).toContain(stack)
  expect(full).toContain(traceback)
  expect(full).toContain('"source": "callback"')
})

test('connection original and propagated tracebacks retain indentation, causes and tail evidence', async ({ page }) => {
  const original = 'Traceback (most recent call last):\n  File "sdk.py", line 42, in connect\n    raise OSError("SDK root")\nOSError: SDK root\n'
  const propagated = `${original}\nThe above exception was the direct cause of the following exception:\n\nTraceback (most recent call last):\n  File "worker.py", line 12, in run\n    raise RuntimeError("wrapper") from error\nRuntimeError: wrapper-tail\n`
  await mockConnectionSnapshot(page, { ...problem(), error: {
    technical_detail: original, traceback: original, propagation_traceback: propagated } })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: '技术详情', exact: true }).click()
  expect(await dialog.getByLabel('原始异常栈', { exact: true }).textContent()).toBe(original)
  expect(await dialog.getByLabel('异常传播栈', { exact: true }).textContent()).toBe(propagated)
  await expect(dialog).not.toContainText('未包含异常调用帧')
  await dialog.getByRole('button', { name: '复制异常传播栈' }).click()
  expect(await copied(page)).toBe(propagated)
})

test('connection problem shares pushed updates and copies full folded evidence without reads', async ({ page }) => {
  let reads = 0
  await mockConnectionSnapshot(page, problem())
  await page.route('**/api/v1/account/7/connection', route => { reads++; return route.abort() })
  await page.goto('/e2e/errors.html?mode=connection')
  const badge = page.getByRole('button', { name: /账户连通状态/ })
  await badge.click()
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toBeVisible()
  await pushConnection(page, problem('B'))
  const dialog = page.getByRole('dialog', { name: '账户连接详情' })
  await expect(dialog).toContainText('本次连接问题-B')
  await expect(dialog).toContainText('已确认的说明-B')
  await expect(dialog).toContainText('本次事实-B')
  await expect(dialog.getByRole('button', { name: '技术详情' })).toHaveAttribute('aria-expanded', 'false')
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain(raw('B'))
  expect(await copied(page)).not.toContain(raw('A'))
  await dialog.getByRole('button', { name: '技术详情' }).click()
  await expect(dialog.getByLabel('完整技术详情')).toContainText('尾部根因-B')
  await page.keyboard.press('Escape')
  await expect(badge).toBeFocused()
  await badge.click()
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toBeVisible()
  expect(reads).toBe(0)
  const reason = '合成连接失败原因'
  await pushConnection(page, { ...problem('C'), presentation: { title: reason, summary: reason } })
  await expect(dialog.getByText(reason, { exact: true })).toHaveCount(1)
})

for (const label of ['', '   ']) test(`blank connection label falls back; missing presentation keeps dedicated problem dialog: ${JSON.stringify(label)}`, async ({ page }) => {
  await mockConnectionSnapshot(page, { ...problem(), label, presentation: null })
  await page.goto(`/e2e/errors.html?mode=connection&label=${encodeURIComponent(label)}`)
  const badge = page.getByRole('button', { name: '连接详情，账户连通状态：暂不可用', exact: true })
  await expect(page.getByRole('switch', { name: '账户连接' })).toContainText('连接异常')
  await badge.click()
  const dialog = page.getByRole('dialog', { name: '账户连接详情' })
  await expect(dialog).toContainText('原始说明-A')
  await expect(dialog.getByRole('button', { name: '技术详情' })).toHaveAttribute('aria-expanded', 'false')
  await expect(dialog.getByRole('link')).toHaveCount(0)
})

for (const status of ['ready', 'unknown']) test(`connection response ${status} hides obsolete channel suggestions`, async ({ page }) => {
  await mockConnectionSnapshot(page, { ...problem(), status })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  const dialog = page.getByRole('dialog', { name: '账户连接详情' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText(status === 'ready' ? '已连接' : '状态待确认')
  await expect(dialog).not.toContainText('检查顺序')
  await expect(dialog).not.toContainText('现场事实')
  await expect(dialog.getByRole('button', { name: '技术详情' })).toHaveCount(0)
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).not.toContain(raw('A'))
})

test('connection stream loss retains the label and disables actions until a new snapshot', async ({ page }) => {
  await mockConnectionSnapshot(page, { ...problem(), status: 'ready' })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已连接')
  await page.evaluate(async () => {
    const modulePath = '/src/stores/liveConnection.ts'
    const { useLiveConnection } = await import(modulePath)
    useLiveConnection.getState().disconnect()
  })
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已连接')
  await expect(page.getByRole('switch', { name: '账户连接' })).toContainText('已连接')
  await expect(page.getByRole('switch', { name: '账户连接' })).toBeDisabled()
  await expect(page.getByRole('dialog').getByRole('button', { name: '重新连接', exact: true })).toBeDisabled()
  await expect(page.getByRole('dialog').getByRole('button', { name: '断开连接', exact: true })).toBeDisabled()
  await pushConnection(page, { ...problem(), status: 'disconnected' })
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已断开')
  await expect(page.getByRole('switch', { name: '账户连接' })).toBeEnabled()
})

test('retained connection failures stay quiet during resync and prolonged foreground loss uses the global entry', async ({ page }) => {
  await defaults(page)
  await page.clock.install()
  await mockConnectionSnapshot(page, { ...problem(), label: '令牌更新失败', presentation: null })
  await page.goto('/e2e/errors.html?mode=connection&global')
  const toggle = page.getByRole('switch', { name: '账户连接' })
  const header = page.getByRole('banner')
  await expect(header).toContainText('服务正常')
  await page.evaluate(async () => {
    const modulePath = '/src/stores/liveConnection.ts'
    const { useLiveConnection } = await import(modulePath)
    useLiveConnection.getState().disconnect()
  })
  await expect(toggle).toContainText('令牌更新失败')
  await expect(toggle).toBeDisabled()
  await page.clock.runFor(4500)
  await expect(header).toContainText('服务正常')
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.fastForward(60_000)
  await expect(header).not.toContainText('实时同步中断')
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(4900)
  await expect(header).not.toContainText('实时同步中断')
  await page.clock.runFor(200)
  await expect(header).toContainText('实时同步中断')
  await expect(toggle).toContainText('令牌更新失败')
  await pushConnection(page, { ...problem(), status: 'ready' })
  await expect(toggle).toContainText('已连接')
  await expect(toggle).toBeEnabled()
  await expect(header).toContainText('服务正常')
})

test('connection generic settings action targets current account and ignores malformed blocks and unknown intents', async ({ page }) => {
  const response = problem()
  await mockConnectionSnapshot(page, { ...response, presentation: { ...response.presentation,
    sections: [null, { title: '空块' }, ...response.presentation.sections], actions: [{ label: '运行脚本', intent: 'execute_script' }, ...response.presentation.actions] } })
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
  await mockConnectionSnapshot(page, { ...problem(), label, presentation: null })
  await page.goto(`/e2e/errors.html?mode=connection&label=${encodeURIComponent(label)}`)
  const badge = page.getByRole('button', { name: `连接详情，账户连通状态：暂不可用，${label}`, exact: true })
  await badge.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '账户连接详情', exact: true })
  await expect(dialog.getByRole('button', { name: '复制连接详情' })).toBeInViewport()
  await expect(dialog.getByRole('button', { name: '关闭详情' })).toBeInViewport()
  await page.keyboard.press('Shift+Tab')
  await expect(dialog.getByRole('button', { name: '关闭详情' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.keyboard.press('Escape')
  await expect(badge).toBeFocused()
  await expect(page.getByRole('switch', { name: '账户连接' }).locator('.overflow-text')).toHaveAttribute('data-overflow', 'true')
})

test('resident connection actions are explicit and never trigger trading', async ({ page }) => {
  await defaults(page)
  let status = 'unavailable'
  await mockConnectionSnapshot(page, { status, observed_at: null, message: '令牌获取超时，连接恢复冷却中', error: { technical_detail: 'GetEncryptedToken Deadline Exceeded' } })
  const writes: string[] = []
  await page.route('**/api/v1/account/7/connection**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST') {
      writes.push(path)
      status = path.endsWith('/disconnect') ? 'disconnected' : 'ready'
      await pushConnection(page, { status, observed_at: null, message: null, error: null })
    }
    await route.fulfill({ json: { status, observed_at: null, message: status === 'unavailable' ? '令牌获取超时，连接恢复冷却中' : null, error: status === 'unavailable' ? { technical_detail: 'GetEncryptedToken Deadline Exceeded' } : null } })
  })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  await expect(page.getByRole('dialog')).toContainText('令牌获取超时')
  expect(writes).toEqual([])
  await page.getByRole('dialog').getByRole('button', { name: '重新连接', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已连接')
  await page.getByRole('dialog').getByRole('button', { name: '断开连接', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已断开')
  await expect(page.getByRole('dialog').getByRole('button', { name: '断开连接', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '关闭详情', exact: true }).click()
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已断开')
  expect(writes).toEqual(['/api/v1/account/7/connection/reconnect', '/api/v1/account/7/connection/disconnect'])
})

test('failed resident reconnect is not replayed by read recovery', async ({ page }) => {
  await defaults(page)
  let writes = 0
  await mockConnectionSnapshot(page, { status: 'unavailable', observed_at: null, message: '连接失败，冷却中', error: { technical_detail: 'GetEncryptedToken Deadline Exceeded' } })
  await page.route('**/api/v1/account/7/connection**', async route => {
    if (route.request().method() === 'POST') {
      writes++
      return route.fulfill({ status: 502, json: { detail: '获取令牌超时，请查看连接详情' } })
    }
    return route.fulfill({ json: { status: 'unavailable', observed_at: null, message: '连接失败，冷却中', error: { technical_detail: 'GetEncryptedToken Deadline Exceeded' } } })
  })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  await page.getByRole('dialog').getByRole('button', { name: '重新连接', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('region', { name: '连接操作结果' })).toContainText('获取令牌超时')
  await page.evaluate(async () => {
    const modulePath = '/src/lib/readRecovery.ts'
    const { notifyReadRecovery } = await import(modulePath)
    await notifyReadRecovery()
  })
  expect(writes).toBe(1)
  await expect(page.getByRole('dialog').getByRole('button', { name: '重新连接', exact: true })).toBeEnabled()
})


test('connection history stays separate from current session after service restart', async ({ page }, testInfo) => {
  const writes: string[] = []
  await page.route('**/api/v1/account/7/connection**', route => { writes.push(route.request().method()); return route.abort() })
  await mockConnectionSnapshot(page, { status: 'uninitialized', worker_alive: false, observed_at: null, message: null, error: null,
    last_success_at: '2026-10-15T15:59:00', last_failure_at: '2026-10-15T15:58:00',
    last_failure: { observed_at: '2026-10-15T15:58:00', message: '历史连接超时', error: { technical_detail: 'historical-native-timeout' } } })
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  const dialog = page.getByRole('dialog', { name: '账户连接详情' })
  await expect(dialog).toContainText('当前服务尚未建立')
  await expect(dialog).toContainText('最近访问成功')
  const history = dialog.getByRole('button', { name: /历史连接失败/ })
  await expect(history).toHaveAttribute('aria-expanded', 'false')
  await expect(dialog.getByRole('button', { name: '断开连接', exact: true })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '连接账户', exact: true })).toBeInViewport()
  await history.click()
  await expect(history).toHaveAttribute('aria-expanded', 'true')
  await expect(dialog).toContainText('不代表当前会话仍在故障')
  await expect(dialog).not.toContainText('连接正常')
  await dialog.getByRole('button', { name: '技术详情' }).click()
  await expect(dialog).toContainText('historical-native-timeout')
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain('historical-native-timeout')
  expect(writes).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('connection-history.png') })
})

test('connection updates reach badge and open dialog without periodic HTTP reads', async ({ page }) => {
  await page.clock.install()
  let reads = 0
  await mockConnectionSnapshot(page, { status: 'connecting', worker_alive: true, observed_at: null, message: null, error: null })
  await page.route('**/api/v1/account/7/connection**', route => { reads++; return route.abort() })
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /账户连通状态/ }).click()
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('连接中…')
  await pushConnection(page, { status: 'ready', worker_alive: true, observed_at: null, message: null, error: null })
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已连接')
  await expect(page.getByRole('button', { name: '连接详情，账户连通状态：就绪' })).toBeVisible()
  await page.clock.fastForward(120_000)
  expect(reads).toBe(0)
  await pushConnection(page, { status: 'disconnected', worker_alive: false, observed_at: null, message: null, error: null })
  await expect(page.getByRole('dialog', { name: '账户连接详情' })).toContainText('已断开')
  expect(reads).toBe(0)
})

test('paused account stays offline until explicit connect then refreshes assets and orders', async ({ page }) => {
  await defaults(page)
  await page.clock.install()
  await page.addInitScript(() => localStorage.setItem(`axile:orders:${location.origin}:7:fetch-mode`, 'automatic'))
  await mockConnectionSnapshot(page, { status: 'uninitialized', worker_alive: false, observed_at: null, message: null, error: null })
  let connects = 0, assets = 0, orders = 0
  const trades: string[] = []
  page.on('request', request => {
    if (/\/execute\/|\/empty_positions\//.test(request.url())) trades.push(request.url())
  })
  await page.route('**/api/v1/account/7/assets/refresh', route => {
    assets++
    return route.fulfill({ json: { assets: { total_asset: 100, available_cash: 100, market_value: 0, positions: [] } } })
  })
  await page.route('**/api/v1/account/7/orders', route => {
    orders++
    return route.fulfill({ json: { data: [], count: 0, complete: true, sampled_at: '2026-10-15T16:00:00', cancellations: [] } })
  })
  await page.route('**/api/v1/account/7/connection/reconnect', async route => {
    connects++
    const connection = { status: 'ready', worker_alive: true, observed_at: null, message: null, error: null }
    await pushConnection(page, connection)
    return route.fulfill({ json: connection })
  })
  await page.goto('/e2e/errors.html?mode=account')
  await expect(page.getByRole('button', { name: '刷新账户资产', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '当日委托与成交', exact: true })).toBeVisible()
  await page.clock.fastForward(60_000)
  expect({ connects, assets, orders }).toEqual({ connects: 0, assets: 0, orders: 0 })
  await page.getByRole('switch', { name: '账户连接' }).click()
  await expect(page.getByRole('button', { name: '连接详情，账户连通状态：就绪' })).toBeVisible()
  await expect.poll(() => assets).toBe(1)
  expect(orders).toBe(0) // 委托由 SSE 推送，连接恢复不发起自动 REST 查询。
  expect(connects).toBe(1)
  expect(trades).toEqual([])
})

for (const label of ['交易前置失败', '认证失败']) test(`connection action icon preserves channel reason and details: ${label}`, async ({ page }) => {
  await mockConnectionSnapshot(page, { ...problem(), label, presentation: null })
  await page.goto('/e2e/errors.html?mode=connection')
  const status = page.getByRole('button', { name: `连接详情，账户连通状态：暂不可用，${label}`, exact: true })
  await expect(page.getByRole('switch', { name: '账户连接' })).toContainText(label)
  await expect(status.locator('svg')).toBeVisible()
  await expect(page.getByRole('switch', { name: '账户连接' }).locator('svg')).toBeVisible()
  await expect(page.getByRole('switch', { name: '账户连接' })).toHaveAttribute('aria-checked', 'false')
  await status.click()
  await expect(page.getByRole('dialog', { name: '账户连接详情', exact: true })).toBeVisible()
  await expect(page.getByRole('dialog').getByRole('button', { name: '技术详情' })).toBeVisible()
})

test('connection switch toggles independently of details and waits for pushed state', async ({ page }) => {
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  const writes: string[] = []
  let complete: (() => void) | undefined
  await page.route('**/api/v1/account/7/connection/**', async route => {
    writes.push(new URL(route.request().url()).pathname)
    await new Promise<void>(resolve => { complete = resolve })
    await route.fulfill({ json: {} })
  })
  await page.goto('/e2e/errors.html?mode=connection')
  const toggle = page.getByRole('switch', { name: '账户连接' })
  const details = page.getByRole('button', { name: /连接详情，账户连通状态/ })
  await expect(toggle).toContainText('已连接')
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await details.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(writes).toEqual([])
  await page.keyboard.press('Escape')
  await toggle.click()
  await expect(toggle).toContainText('断开中…')
  await expect(toggle).toBeDisabled()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(toggle.locator('svg')).toHaveAttribute('data-busy', 'true')
  await expect(toggle.locator('svg')).toHaveAttribute('data-phase', 'disconnecting')
  await expect(toggle.locator('g').first()).toHaveCSS('animation-name', 'none')
  await expect(toggle).toHaveCSS('border-top-width', '1px')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(toggle.locator('g').first()).toHaveCSS('animation-name', 'none')
  await expect.poll(() => writes.length).toBe(1)
  complete!()
  await expect(toggle).toBeEnabled()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await pushConnection(page, { status: 'disconnected', observed_at: null, message: null, error: null })
  await expect(toggle).toContainText('已断开')
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await toggle.click()
  await expect(toggle).toContainText('连接中…')
  await expect(toggle.locator('svg')).toHaveAttribute('data-phase', 'connecting')
  await expect.poll(() => writes.length).toBe(2)
  complete!()
  await pushConnection(page, { status: 'connecting', observed_at: null, message: null, error: null })
  await expect(toggle).toBeDisabled()
  await pushConnection(page, { status: 'ready', observed_at: null, message: null, error: null })
  await expect(toggle).toContainText('已连接')
  await expect(toggle).toBeEnabled()
  await expect(toggle.locator('svg')).toHaveAttribute('data-busy', 'false')
  await expect(toggle.locator('svg')).toHaveAttribute('data-phase', 'closed')
  expect(writes).toEqual(['/api/v1/account/7/connection/disconnect', '/api/v1/account/7/connection/reconnect'])
})

test('connection failure keeps channel label and puts feedback in details', async ({ page }) => {
  await defaults(page)
  await page.setViewportSize({ width: 375, height: 620 })
  const label = '交易运行时异常'
  await mockConnectionSnapshot(page, { status: 'unavailable', label, observed_at: null, message: 'GM 启动失败', error: null })
  await page.route('**/api/v1/account/7/connection/reconnect', route =>
    route.fulfill({ status: 502, json: { detail: 'GM 启动失败，具体原因未确认' } }))
  await page.goto('/e2e/errors.html?mode=connection')
  const toggle = page.getByRole('switch', { name: '账户连接' })
  const details = page.getByRole('button', { name: /连接详情，账户连通状态/ })
  await expect(toggle).toContainText(label)
  await expect(details).toHaveText('')
  const before = await toggle.boundingBox()
  await toggle.click()
  const alert = page.getByRole('alert').filter({ hasText: '连接操作未完成' })
  await expect(alert).toHaveCount(0)
  await expect(toggle).toContainText(label)
  const after = await toggle.boundingBox()
  expect(after!.height).toBe(before!.height)
  expect(after!.y).toBe(before!.y)
  await details.click()
  await expect(page.getByRole('dialog')).toContainText('GM 启动失败，具体原因未确认')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('global connection loss owns network feedback and recovery does not revive it', async ({ page }, testInfo) => {
  await defaults(page)
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  await page.goto('/e2e/errors.html?mode=connection&global')
  await page.evaluate(async () => {
    const domainPath = '/src/stores/domain.ts'
    const operationPath = '/src/stores/connectionOperations.ts'
    const connectionPath = '/src/stores/liveConnection.ts'
    const { useLiveConnection } = await import(connectionPath)
    useLiveConnection.getState().disconnect()
    const { useDomainStore } = await import(domainPath)
    const { useConnectionOperations } = await import(operationPath)
    const error = new TypeError('Failed to fetch')
    useDomainStore.setState({ accountsError: error })
    useConnectionOperations.setState({ operations: { 7: { pending: null, error } } })
  })
  await expect(page.getByRole('banner')).toContainText('与服务器失联')
  await expect(page.getByText('无法连接 axile 服务', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('alert').filter({ hasText: '连接操作未完成' })).toHaveCount(0)
  await expect(page.getByRole('switch', { name: '账户连接' })).toBeDisabled()
  await page.screenshot({ path: testInfo.outputPath('connection-global-loss.png') })
  await page.evaluate(async () => {
    const path = '/src/stores/domain.ts'
    const { useDomainStore } = await import(path)
    useDomainStore.setState({ accountsError: null })
  })
  await expect(page.getByRole('alert').filter({ hasText: '连接操作未完成' })).toHaveCount(0)
})

test('failed GM start belongs to connection entry and details, with no card error block', async ({ page }, testInfo) => {
  await defaults(page)
  await mockConnectionSnapshot(page, { status: 'disconnected', worker_alive: false, observed_at: null, message: null, error: null })
  let writes = 0
  await page.route('**/api/v1/account/7/connection/reconnect', route => {
    writes++
    return route.fulfill({ status: 500, json: { detail: { message: 'GM 启动失败，具体原因未确认', traceback: 'native-gm-start-traceback' } } })
  })
  await page.goto('/e2e/errors.html?mode=connection')
  const toggle = page.getByRole('switch', { name: '账户连接' })
  await toggle.click()
  await expect(toggle).toContainText('GM 启动失败，具体原因未确认')
  await expect(toggle).toHaveAttribute('data-tone', 'warning')
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.getByRole('button', { name: /连接详情，账户连通状态/ }).click()
  const dialog = page.getByRole('dialog', { name: '账户连接详情', exact: true })
  await expect(dialog.getByRole('region', { name: '连接操作结果' })).toContainText('GM 启动失败')
  await expect(dialog).toContainText('native-gm-start-traceback')
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain('native-gm-start-traceback')
  await page.screenshot({ path: testInfo.outputPath('connection-failure-details.png') })
  await page.keyboard.press('Escape')
  await expect(toggle).toContainText('GM 启动失败，具体原因未确认')
  await pushConnection(page, { status: 'ready', worker_alive: true, observed_at: null, message: null, error: null })
  await expect(toggle).toContainText('已连接')
  await pushConnection(page, { status: 'disconnected', worker_alive: false, observed_at: null, message: null, error: null })
  await expect(toggle).toContainText('已断开')
  expect(writes).toBe(1)
})

test('asset refresh busy and failure stay beside the asset action', async ({ page }, testInfo) => {
  await defaults(page)
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  let status = 409, calls = 0
  await page.route('**/api/v1/account/7/assets/refresh', route => {
    calls++
    return route.fulfill({ status, json: status === 409 ? { detail: '账户正在执行或刷新资产，请稍后再试' }
      : status === 500 ? { detail: { message: 'asset-provider-failure', traceback: 'asset-native-traceback' } }
      : { assets: { total_asset: 100, available_cash: 100, market_value: 0, positions: [] } } })
  })
  await page.goto('/e2e/errors.html?mode=account')
  const refresh = page.getByRole('button', { name: '刷新账户资产', exact: true })
  await expect(refresh).toBeEnabled()
  await expect.poll(() => calls).toBe(1)
  await expect(page.getByRole('button', { name: /查看资产刷新详情/ })).toHaveCount(0)
  await expect(page.getByText('连接后的资产更新未完成', { exact: true })).toHaveCount(0)
  await refresh.click()
  const busy = page.getByRole('button', { name: '查看资产刷新详情：账户忙碌' })
  await expect(busy).toBeVisible()
  await expect(busy).not.toHaveClass(/text-warn/)
  await busy.click()
  await expect(page.getByRole('dialog', { name: '资产刷新详情' })).toContainText('账户正在执行或刷新资产')
  await page.keyboard.press('Escape')
  status = 500
  await refresh.click()
  const failed = page.getByRole('button', { name: '查看资产刷新详情：刷新失败' })
  await expect(failed).toBeVisible()
  await expect(page.getByRole('switch', { name: '账户连接' })).toContainText('已连接')
  await failed.click()
  await expect(page.getByRole('dialog', { name: '资产刷新详情' })).toContainText('asset-native-traceback')
  await page.keyboard.press('Escape')
  await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('asset-refresh-inline-feedback.png') })
  status = 200
  await refresh.click()
  await expect(page.getByRole('button', { name: /查看资产刷新详情/ })).toHaveCount(0)
  await expect(refresh).toBeEnabled()
  expect(calls).toBe(4)
})

test('automatic asset failure has an inline detail entry and a successful manual refresh clears it', async ({ page }) => {
  await defaults(page)
  await mockConnectionSnapshot(page, { status: 'ready', observed_at: null, message: null, error: null })
  let calls = 0
  await page.route('**/api/v1/account/7/assets/refresh', route => ++calls === 1
    ? route.fulfill({ status: 500, json: { detail: 'automatic-asset-failure' } })
    : route.fulfill({ json: { assets: { total_asset: 100 } } }))
  await page.goto('/e2e/errors.html?mode=account')
  const failed = page.getByRole('button', { name: '查看资产刷新详情：刷新失败' })
  await expect(failed).toBeVisible()
  await failed.click()
  await expect(page.getByRole('dialog', { name: '资产刷新详情' })).toContainText('automatic-asset-failure')
  await page.keyboard.press('Escape')
  await expect(failed).toBeFocused()
  await page.getByRole('button', { name: '刷新账户资产', exact: true }).click()
  await expect(failed).toHaveCount(0)
  await expect(page.getByRole('button', { name: '刷新账户资产', exact: true })).toBeEnabled()
  expect(calls).toBe(2)
})

for (const width of [390, 1280]) test(`connection progress stays live in the card and details at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 })
  const definitions = [
    ['environment', '准备掘金运行环境'], ['runtime', '启动策略桥'], ['account_login', '等待交易账户就绪'],
    ['account_assets', '核对账户资产'], ['pending_orders', '核对未完成委托'], ['ready', '账户就绪'],
  ]
  const steps = definitions.map(([id, label], index) => ({ id, label, status: index < 2 ? 'passed' : index === 2 ? 'running' : 'pending' }))
  const progress = { run_id: 'live-progress', channel: 'gm', purpose: 'connect', status: 'running', steps,
    created_at: '2026-10-16T14:00:00+08:00', finished_at: null, error: null }
  const connection = { status: 'connecting', worker_alive: true, observed_at: null, message: null, error: null, progress }
  await mockConnectionSnapshot(page, connection)
  let reads = 0
  await page.route('**/api/v1/account/7/**', route => { reads++; return route.fulfill({ json: {} }) })
  await page.goto('/e2e/errors.html?mode=connection')
  const card = page.getByRole('switch', { name: '账户连接' })
  await expect(card).toContainText('等待交易账户就绪')
  await expect(card.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '2')
  await page.getByRole('button', { name: /连接详情，账户连通状态/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '2')
  await expect(dialog.getByRole('list', { name: '连接检查步骤' })).toHaveAttribute('aria-busy', 'true')
  await page.screenshot({ path: testInfo.outputPath('account-connection-progress.png'), fullPage: true })
  const checking = { ...progress, steps: steps.map((step, index) => ({ ...step, status: index < 3 ? 'passed' : index === 3 ? 'running' : 'pending' })) }
  await pushConnection(page, { ...connection, progress: checking })
  await expect(card).toContainText('核对账户资产')
  await expect(dialog.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '3')
  const finished = { ...progress, status: 'succeeded', finished_at: '2026-10-16T14:00:04+08:00', steps: steps.map(step => ({ ...step, status: 'passed' })) }
  await pushConnection(page, { ...connection, status: 'ready', progress: finished })
  await expect(card.getByRole('progressbar')).toHaveCount(0)
  await expect(dialog.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '6')
  await expect(page.getByRole('dialog', { name: '账户连接详情', exact: true })).toContainText('已连接')
  expect(reads).toBe(0)
})

test('connection step failure retains completed progress and a lost stream stops animation', async ({ page }) => {
  const progress = { run_id: 'failing-progress', channel: 'tq', purpose: 'connect', status: 'running',
    steps: [
      { id: 'api_connection', label: '建立天勤账户连接', status: 'passed' },
      { id: 'event_pump', label: '启动实时更新', status: 'running' },
      { id: 'account_assets', label: '核对账户资产', status: 'pending' },
      { id: 'pending_orders', label: '核对未完成委托', status: 'pending' },
      { id: 'ready', label: '账户就绪', status: 'pending' },
    ], created_at: '2026-10-16T14:00:00+08:00', finished_at: null, error: null }
  const connection = { status: 'connecting', worker_alive: true, observed_at: null, message: null, error: null, progress }
  await mockConnectionSnapshot(page, connection)
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /连接详情，账户连通状态/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.locator('.animate-spin')).toHaveCount(1)
  await page.evaluate(async () => {
    const path = '/src/stores/liveConnection.ts'
    const { useLiveConnection } = await import(path)
    useLiveConnection.getState().disconnect()
  })
  await expect(dialog.locator('.animate-spin')).toHaveCount(0)
  const error = { reason_code: 'CONNECTION.FAILED', message: '事件泵退出', details: '原始事件泵错误' }
  const failed = { ...progress, status: 'failed', error,
    steps: progress.steps.map((step, i) => ({ ...step, status: i === 0 ? 'passed' : i === 1 ? 'failed' : 'skipped', error: i === 1 ? error : null })) }
  await pushConnection(page, { ...connection, status: 'unavailable', label: '实时更新失败', progress: failed })
  await expect(dialog.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '1')
  await expect(dialog.getByRole('alert')).toContainText('事件泵退出')
  await expect(dialog.getByRole('list', { name: '连接检查步骤' })).toHaveAttribute('aria-busy', 'false')
})

for (const kind of ['交易', '行情']) test(`CTP ${kind} logout updates an open connection dialog and preserves history`, async ({ page }) => {
  await defaults(page)
  const ready = { status: 'ready', observed_at: null, message: null, error: null }
  await mockConnectionSnapshot(page, ready)
  await page.goto('/e2e/errors.html?mode=connection')
  await page.getByRole('button', { name: /连接详情/ }).click()
  const dialog = page.getByRole('dialog', { name: '账户连接详情' })
  const failure = {
    observed_at: '2026-10-20T12:00:00', label: `${kind}会话已登出`, message: `CTP ${kind}会话已登出`,
    error: { reason_code: 'CTP_USER_LOGOUT', technical_detail: 'OnRspUserLogout request_id=42 ErrorID=0' },
    presentation: { title: `${kind}会话已登出`, summary: '当前账户会话已失效。', sections: [{ fields: [{ label: '请求编号', value: '42' }] }] },
  }
  await pushConnection(page, { ...failure, status: 'unavailable' })
  await expect(page.getByRole('switch', { name: '账户连接' })).toHaveAttribute('aria-checked', 'false')
  await expect(dialog).toContainText(`${kind}会话已登出`)
  await expect(dialog).toContainText('42')
  await dialog.getByRole('button', { name: '复制连接详情' }).click()
  expect(await copied(page)).toContain('CTP_USER_LOGOUT')
  await pushConnection(page, { ...ready, last_failure: failure })
  await expect(page.getByRole('switch', { name: '账户连接' })).toHaveAttribute('aria-checked', 'true')
  await expect(dialog.getByRole('button', { name: /历史连接失败/ })).toBeVisible()
  await pushConnection(page, { status: 'disconnected', manually_disconnected: true, observed_at: null, message: null, error: null })
  await expect(dialog).toContainText('账户会话已主动断开')
})
