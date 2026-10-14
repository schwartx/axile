import { expect, test, type Page } from '@playwright/test'

const raw = (name: string) => `${name}开始\n${'synthetic error '.repeat(100)}\nTraceback\n  script.py:123\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因-${name}`
const record = (id = 'A') => ({ id: 1, account_id: 7, execution_id: id, raw_input: {}, raw_result: { status: 'FAILED', technical_detail: raw(id), traceback: raw(id) }, is_success: 0, created_at: '2026-10-14T10:00:00' })
const sync = (status = 'synchronized', revision = 1, last_error: string | null = null) => ({ status, revision, last_error, attempts: 1, last_attempt_at: null, synchronized_at: null })
const account = { id: 7, name: '合成账户 A', market: 'demo', trade_channel: 'paper', is_started: false, cron_expr: '', portfolio_id: 9, algorithm: { method: 'SINGLE-MAKER', params: {} }, empty_positions_algorithm: null, account_config: {}, forbidden_symbols: [], risk_symbols: [], trade_rules: [], long_leverage: 1, short_leverage: 1, weight_precision: .001 }
const snapshot = { portfolio_id: 9, context_account_id: 7, weights: {}, calculated_at: '2026-10-14T10:00:00', sizing: null }

async function defaults(page: Page) {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/v1/editor/')) return route.fallback()
    const json = path.endsWith('/runtime-sync') ? sync() : /\/account\/7$/.test(path) ? account
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
  const notice = page.getByRole('status').filter({ hasText: '账户资产刷新失败' })
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
