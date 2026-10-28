import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'

const fixture = JSON.parse(readFileSync(new URL('../../tests/fixtures/browser_analysis.json', import.meta.url), 'utf8'))
const invoke = <T = any>(page: Page, command: string, query = {}): Promise<T> => page.evaluate(({ command, query }) => (window as any).browserAnalysis(1, command, query), { command, query })
const cleanChart = (chart: any) => { const { snapshot_id: _s, snapshot_version: _v, computed_at: _t, ...value } = chart; return value }
async function loaded(page: Page) {
  await page.goto('/e2e/browser-analysis.html')
  await expect(page.locator('#result')).toHaveText('分析引擎已加载')
}
async function ready(page: Page) {
  await expect.poll(async () => (await invoke(page, 'status')).status, { timeout: 15000 }).toBe('ready')
  return invoke(page, 'status')
}

for (const interrupt of [false, true]) {
  test(`WASM matches native math and resumes from OPFS${interrupt ? ' after interruption' : ' across two tabs'}`, async ({ page, context }) => {
    const state = structuredClone(fixture.state)
    const records = new Map<string, any[]>()
    for (const part of fixture.pages.flatMap((p: any) => p.parts)) {
      if (!records.has(part.record_id)) records.set(part.record_id, [])
      records.get(part.record_id)!.push(structuredClone(part))
    }
    const requests: any[] = []
    let fail = false
    await context.route('**/api/v1/account/performance/1/facts*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/facts-state') || url.pathname.endsWith('/facts-prepare')) return route.fulfill({ json: state })
      const request = route.request().postDataJSON()
      requests.push(request)
      if (fail || (interrupt && requests.length === 2)) return route.fulfill({ status: 503, json: { detail: '同步中断' } })
      const available = [...records.values()].filter(parts => BigInt(parts[0].seq) > BigInt(request.cursor.seq) && BigInt(parts[0].seq) <= BigInt(request.until)).sort((a, b) => Number(BigInt(a[0].seq) - BigInt(b[0].seq)))
      const parts = structuredClone(available[0] ?? [])
      const seq = available.length > 1 ? parts[0].seq : request.until
      for (const part of parts) {
        // Send a real JSON integer beyond Number.MAX_SAFE_INTEGER to the WASM parser.
        if (part.table === 'portfolioaccount') part.rows[0][0] = 'BIG_INTEGER_ID'
      }
      const result = { schema_version: 2, source_id: state.source_id, from: request.cursor, until: request.until, parts, cursor: { seq, table: 22, after: null }, complete: available.length <= 1 }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(result).replace('"BIG_INTEGER_ID"', '9007199254740993') })
    })
    await loaded(page)
    if (interrupt) {
      await invoke(page, 'status')
      await expect.poll(async () => (await invoke(page, 'status')).status).toBe('failed')
      expect(requests).toHaveLength(2)
      await page.reload()
      await expect(page.locator('#result')).toHaveText('分析引擎已加载')
    }
    const first = await ready(page)
    if (interrupt) expect(requests[2].cursor).toEqual(requests[1].cursor)
    for (const range of ['30', '90', 'all']) {
      const chart = await invoke(page, 'chart', { snapshot_id: first.snapshot_id, range })
      expect(cleanChart(chart)).toEqual(cleanChart(fixture.charts[range]))
    }
    const detail = await invoke(page, 'execution-detail', { snapshot_id: first.snapshot_id, record_id: 1 })
    expect({ ...detail, snapshot_id: 'native-fixture' }).toEqual(fixture.detail)
    const costs = await invoke(page, 'costs', { snapshot_id: first.snapshot_id, range: 'all', dimension: 'trade', limit: 1 })
    expect(costs.data).toEqual(fixture.costs.data)
    expect(costs.summary).toEqual(fixture.costs.summary)
    const next = await invoke(page, 'costs', { snapshot_id: first.snapshot_id, range: 'all', dimension: 'trade', limit: 1, cursor: costs.next_cursor })
    expect(next.data).toHaveLength(1)
    expect(next.data).not.toEqual(costs.data)
    const downloaded = requests.length
    await page.reload()
    await expect(page.locator('#result')).toHaveText('分析引擎已加载')
    expect((await ready(page)).snapshot_id).toBe(first.snapshot_id)
    expect(requests).toHaveLength(downloaded)

    state.source_version++
    state.cursor = String(BigInt(state.cursor) + 1n)
    for (const part of records.get('2')!) {
      part.seq = state.cursor
      if (part.table === 'business_asset_snapshot') part.rows[0][4] = 50
    }
    fail = true
    await invoke(page, 'status')
    await expect.poll(async () => (await invoke(page, 'status')).status).toBe('failed')
    expect(cleanChart(await invoke(page, 'chart', { snapshot_id: first.snapshot_id, range: 'all' }))).toEqual(cleanChart(fixture.charts.all))
    fail = false
    const other = await context.newPage()
    await loaded(other)
    await invoke(page, 'refresh')
    const [left, right] = await Promise.all([ready(page), ready(other)])
    expect(left.snapshot_id).toBe(right.snapshot_id)
    expect(left.snapshot_id).not.toBe(first.snapshot_id)
    const delta = requests.slice(downloaded)
    expect(delta.every(request => request.cursor.seq === fixture.state.cursor)).toBe(true)
    const revised = await invoke(page, 'chart', { snapshot_id: left.snapshot_id, range: 'all' })
    expect(revised.tables.points.rows).not.toEqual(fixture.charts.all.tables.points.rows)
    await other.close()
  })
}

test('large local history downloads once and applies a bounded delta', async ({ page, context }) => {
  const file = process.env.AXILE_ANALYSIS_HISTORY
  test.skip(!file, 'Set AXILE_ANALYSIS_HISTORY to an account-scoped facts export')
  test.setTimeout(120000)
  const history = JSON.parse(readFileSync(file!, 'utf8'))
  let state = history.state
  let pages = history.pages
  let requests = 0
  const account = state.account_id
  const call = (command: string, query = {}) => page.evaluate(({ account, command, query }) => (window as any).browserAnalysis(account, command, query), { account, command, query })
  await context.route(`**/api/v1/account/performance/${account}/facts*`, async route => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/facts-state') || url.pathname.endsWith('/facts-prepare')) return route.fulfill({ json: state })
    requests++
    const request = route.request().postDataJSON()
    const next = pages.find((value: any) => JSON.stringify(value.from) === JSON.stringify(request.cursor))
    expect(next, 'request must resume an exported page cursor').toBeDefined()
    return route.fulfill({ json: next })
  })
  const wait = async () => {
    await expect.poll(async () => {
      const value = await call('status')
      expect(value.error).toBeNull()
      return value.status
    }, { timeout: 90000, intervals: [100, 250, 500] }).toBe('ready')
    return call('status')
  }
  await loaded(page)
  const start = performance.now()
  const initial = await wait()
  const firstMs = performance.now() - start
  expect(requests).toBe(history.pages.length)
  const chart = await call('chart', { snapshot_id: initial.snapshot_id, range: 'all' })
  expect(chart.tables.points.rows.length).toBeGreaterThan(0)
  const downloaded = requests
  await loaded(page)
  const reopenStart = performance.now()
  expect((await wait()).snapshot_id).toBe(initial.snapshot_id)
  const reopenMs = performance.now() - reopenStart
  expect(requests).toBe(downloaded)
  state = history.delta.state
  pages = history.delta.pages
  const deltaStart = performance.now()
  const updated = await wait()
  expect(updated.snapshot_id).not.toBe(initial.snapshot_id)
  expect(requests - downloaded).toBe(history.delta.pages.length)
  expect(requests - downloaded).toBeLessThan(history.pages.length)
  const deltaMs = performance.now() - deltaStart
  const deltaPages = requests - downloaded
  let correctionMs: number | undefined
  if (history.correction) {
    state = history.correction.state
    pages = history.correction.pages
    const correctionStart = performance.now()
    expect((await wait()).snapshot_id).not.toBe(updated.snapshot_id)
    correctionMs = Math.round(performance.now() - correctionStart)
    expect(requests - downloaded - deltaPages).toBe(history.correction.pages.length)
  }
  console.log(JSON.stringify({ firstMs: Math.round(firstMs), reopenMs: Math.round(reopenMs), deltaMs: Math.round(deltaMs), correctionMs, pages: downloaded, deltaPages }))
})
