import { afterEach, expect, test } from 'bun:test'
import { checkPerformance, performanceEntry, reconcilePerformanceVersion, requestPerformanceRefresh, shouldPollPerformance, snapshotPending } from '@/features/history/performanceCache'
import { chartFixture, metadataFixture } from '@/features/history/analysisTestFixtures'
import type { AnalysisMetadata, PerformanceRange } from '@/lib/api/performance'

const originalFetch = globalThis.fetch
const mockFetch = (implementation: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) => { globalThis.fetch = Object.assign(implementation, { preconnect: originalFetch.preconnect }) }
afterEach(() => { globalThis.fetch = originalFetch })
const urlOf = (input: RequestInfo | URL) => new URL(String(input), 'http://fixture')
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(release => { resolve = release })
  return { promise, resolve }
}
async function settle(id: number, range: PerformanceRange = 'all') {
  const entry = performanceEntry(id, range)
  for (let step = 0; step < 8; step++) {
    await entry.flight
    await entry.resultFlight
    await Promise.resolve()
    if (!entry.flight && !entry.resultFlight) return
  }
  throw new Error('fixture requests did not settle')
}
function serve(id: number, metadata: () => AnalysisMetadata) {
  mockFetch(async input => {
    const url = urlOf(input)
    return Response.json(url.pathname.endsWith('/status') ? metadata() : chartFixture(id, url.searchParams.get('snapshot_id')!, url.searchParams.get('range') as PerformanceRange))
  })
}

test('simultaneous ranges share status while chart reads remain range-bound', async () => {
  const status = deferred<Response>()
  let statusCalls = 0
  const charts: string[] = []
  mockFetch(async input => {
    const url = urlOf(input)
    if (url.pathname.endsWith('/status')) { statusCalls++; return status.promise }
    charts.push(url.searchParams.get('range')!)
    return Response.json(chartFixture(820, 'S1', url.searchParams.get('range') as PerformanceRange))
  })
  const checks = [checkPerformance(820, 'all'), checkPerformance(820, 'all'), checkPerformance(820, '30')]
  status.resolve(Response.json(metadataFixture()))
  await Promise.all(checks)
  await Promise.all([settle(820), settle(820, '30')])
  expect(statusCalls).toBe(1)
  expect(charts.sort()).toEqual(['30', 'all'])
  expect(performanceEntry(820, 'all').state.displayed?.range).toBe('all')
  expect(performanceEntry(820, '30').state.displayed?.range).toBe('30')
  expect(performanceEntry(820, '90').state.displayed).toBeNull()
})

test('same-version status and calculation errors update without another chart download', async () => {
  let metadata = metadataFixture()
  let chartCalls = 0
  mockFetch(async input => {
    if (urlOf(input).pathname.endsWith('/status')) return Response.json(metadata)
    chartCalls++; return Response.json(chartFixture(821))
  })
  await checkPerformance(821, 'all'); await settle(821)
  const old = performanceEntry(821, 'all').state.displayed
  metadata = metadataFixture('S1', { status: 'failed', source_version: 2, error: '计算失败', retry_at: 123 })
  await checkPerformance(821, 'all')
  expect(chartCalls).toBe(1)
  expect(performanceEntry(821, 'all').state.displayed).toBe(old)
  expect(performanceEntry(821, 'all').state.metadata).toMatchObject({ status: 'failed', source_version: 2, error: '计算失败', retry_at: 123 })
  expect(snapshotPending(metadata)).toBe(true)
  expect(snapshotPending({ ...metadata, retry_at: null })).toBe(false)
})

test('late S2 cannot replace S1 after metadata targets S3 or erase newer status', async () => {
  let metadata = metadataFixture()
  const second = deferred<Response>()
  mockFetch(async input => {
    const url = urlOf(input)
    if (url.pathname.endsWith('/status')) return Response.json(metadata)
    const id = url.searchParams.get('snapshot_id')!
    return id === 'S2' ? second.promise : Response.json(chartFixture(822, id))
  })
  await checkPerformance(822, 'all'); await settle(822)
  const entry = performanceEntry(822, 'all')
  const seen: (string | undefined)[] = []
  const observe = () => seen.push(entry.state.displayed?.snapshot_id)
  entry.listeners.add(observe)
  metadata = metadataFixture('S2')
  await checkPerformance(822, 'all')
  expect(entry.state.metadata?.snapshot_id).toBe('S2')
  expect(entry.state.displayed?.snapshot_id).toBe('S1')
  expect(entry.state.loadingResult).toBe(true)
  metadata = metadataFixture('S3', { status: 'stale', error: '新版排队', source_version: 4 })
  await checkPerformance(822, 'all')
  second.resolve(Response.json(chartFixture(822, 'S2')))
  await settle(822)
  expect(seen).not.toContain('S2')
  expect(entry.state.displayed?.snapshot_id).toBe('S3')
  expect(entry.state.metadata).toMatchObject({ status: 'stale', error: '新版排队', source_version: 4 })
  entry.listeners.delete(observe)
})

test('result, status and refresh failures remain separate and keep the last chart', async () => {
  let metadata = metadataFixture()
  serve(823, () => metadata)
  await checkPerformance(823, 'all'); await settle(823)
  const entry = performanceEntry(823, 'all')
  const old = entry.state.displayed
  let charts = 0
  metadata = metadataFixture('S2', { status: 'pending' })
  mockFetch(async input => {
    if (urlOf(input).pathname.endsWith('/status')) return Response.json(metadata)
    charts++; return new Response('chart unavailable', { status: 503 })
  })
  await checkPerformance(823, 'all'); await settle(823)
  await checkPerformance(823, 'all', false)
  expect(charts).toBe(1)
  expect(entry.state.resultError?.message).toBe('chart unavailable')
  expect(entry.state.statusError).toBeNull()
  expect(shouldPollPerformance(entry)).toBe(true)
  mockFetch(async () => { throw new Error('offline') })
  await checkPerformance(823, 'all', false)
  expect(entry.state.statusError?.message).toBe('offline')
  expect(await requestPerformanceRefresh(823)).toBe(false)
  expect(entry.state.refreshError?.message).toBe('offline')
  expect(entry.state.resultError?.message).toBe('chart unavailable')
  expect(entry.state.displayed).toBe(old)
  serve(823, () => metadata)
  await checkPerformance(823, 'all'); await settle(823)
  expect(entry.state.displayed?.snapshot_id).toBe('S2')
  expect(entry.state.resultError).toBeNull()
  expect(entry.state.statusError).toBeNull()
})

test('410 makes one immediate status recovery and waits for another check to read a replacement', async () => {
  let metadata = metadataFixture()
  serve(824, () => metadata)
  await checkPerformance(824, 'all'); await settle(824)
  let statuses = 0, charts = 0, posts = 0
  metadata = metadataFixture('S2')
  mockFetch(async (input, init) => {
    if (init?.method === 'POST') { posts++; return Response.json({}) }
    const url = urlOf(input)
    if (url.pathname.endsWith('/status')) { statuses++; return Response.json(statuses === 1 ? metadata : metadataFixture('S3')) }
    charts++
    return url.searchParams.get('snapshot_id') === 'S2' ? Response.json({ detail: { code: 'SNAPSHOT_EXPIRED', message: '过期' } }, { status: 410 }) : Response.json(chartFixture(824, 'S3'))
  })
  await checkPerformance(824, 'all'); await settle(824)
  const entry = performanceEntry(824, 'all')
  expect(statuses).toBe(2)
  expect(charts).toBe(1)
  expect(posts).toBe(0)
  expect(entry.state.metadata?.snapshot_id).toBe('S3')
  expect(entry.state.displayed?.snapshot_id).toBe('S1')
  expect(shouldPollPerformance(entry)).toBe(true)
  await checkPerformance(824, 'all', false); await settle(824)
  expect(charts).toBe(2)
  expect(entry.state.displayed?.snapshot_id).toBe('S3')
})

test('an expired ID keeps checking small status without repeatedly downloading that ID', async () => {
  let charts = 0, statuses = 0
  mockFetch(async input => {
    if (urlOf(input).pathname.endsWith('/status')) { statuses++; return Response.json(metadataFixture('expired')) }
    charts++; return new Response('', { status: 410 })
  })
  await checkPerformance(825, 'all'); await settle(825)
  await checkPerformance(825, 'all', false); await settle(825)
  expect(statuses).toBe(3)
  expect(charts).toBe(1)
  expect(shouldPollPerformance(performanceEntry(825, 'all'))).toBe(true)
})

test('marker0 is explicit and does not start a ready-state chart retry loop', async () => {
  let charts = 0
  mockFetch(async input => {
    if (urlOf(input).pathname.endsWith('/status')) return Response.json(metadataFixture('legacy', { read_view_schema_version: 0 }))
    charts++; throw new Error('must not read legacy chart')
  })
  await checkPerformance(826, 'all')
  const entry = performanceEntry(826, 'all')
  expect(entry.state.metadata?.read_view_schema_version).toBe(0)
  expect(entry.state.displayed).toBeNull()
  expect(charts).toBe(0)
  expect(shouldPollPerformance(entry)).toBe(false)
})

test('refresh coalesces the mutation and checks active ranges without loading inactive cached charts', async () => {
  serve(827, () => metadataFixture())
  await Promise.all([checkPerformance(827, 'all'), checkPerformance(827, '90')])
  await Promise.all([settle(827), settle(827, '90')])
  const entry = performanceEntry(827, 'all')
  const listener = () => {}
  entry.listeners.add(listener)
  let posts = 0
  const ranges: string[] = []
  mockFetch(async (input, init) => {
    const url = urlOf(input)
    if (init?.method === 'POST') { posts++; return Response.json({ status: 'pending' }) }
    if (url.pathname.endsWith('/status')) return Response.json(metadataFixture('S2'))
    ranges.push(url.searchParams.get('range')!)
    return Response.json(chartFixture(827, 'S2'))
  })
  await Promise.all([requestPerformanceRefresh(827), requestPerformanceRefresh(827)])
  await settle(827)
  expect(posts).toBe(1)
  expect(ranges).toEqual(['all'])
  expect(entry.state.displayed?.snapshot_id).toBe('S2')
  expect(performanceEntry(827, '90').state.displayed?.snapshot_id).toBe('S1')
  entry.listeners.delete(listener)
})

test('a dashboard publication during status loading is not lost to the old flight', async () => {
  const first = deferred<Response>()
  let statuses = 0
  const chartIds: string[] = []
  mockFetch(async input => {
    const url = urlOf(input)
    if (url.pathname.endsWith('/status')) { statuses++; return statuses === 1 ? first.promise : Response.json(metadataFixture('S3')) }
    chartIds.push(url.searchParams.get('snapshot_id')!)
    return Response.json(chartFixture(828, 'S3'))
  })
  const entry = performanceEntry(828, 'all')
  const listener = () => {}
  entry.listeners.add(listener)
  const initial = checkPerformance(828, 'all')
  reconcilePerformanceVersion(828, 'S3')
  first.resolve(Response.json(metadataFixture('S2')))
  await initial; await settle(828)
  expect(statuses).toBe(2)
  expect(chartIds).toEqual(['S3'])
  expect(entry.state.displayed?.snapshot_id).toBe('S3')
  reconcilePerformanceVersion(828, 'S3')
  expect(statuses).toBe(2)
  entry.listeners.delete(listener)
})

test('a range mounted during POST gets fresh status after the mutation', async () => {
  serve(830, () => metadataFixture())
  await checkPerformance(830, 'all'); await settle(830)
  const mutation = deferred<Response>(), oldStatus = deferred<Response>()
  let mutationDone = false
  mockFetch(async (input, init) => {
    const url = urlOf(input)
    if (init?.method === 'POST') { const response = await mutation.promise; mutationDone = true; return response }
    if (url.pathname.endsWith('/status')) return mutationDone ? Response.json(metadataFixture('S2')) : oldStatus.promise
    return Response.json(chartFixture(830, url.searchParams.get('snapshot_id')!, url.searchParams.get('range') as PerformanceRange))
  })
  const refresh = requestPerformanceRefresh(830)
  const range = performanceEntry(830, '30')
  const listener = () => {}
  range.listeners.add(listener)
  const oldCheck = checkPerformance(830, '30')
  mutation.resolve(Response.json({ status: 'pending' }))
  await Promise.resolve(); await Promise.resolve()
  oldStatus.resolve(Response.json(metadataFixture('S1')))
  await Promise.all([refresh, oldCheck]); await settle(830, '30')
  expect(range.state.displayed?.snapshot_id).toBe('S2')
  expect(range.state.displayed?.range).toBe('30')
  range.listeners.delete(listener)
})

test('a wrong snapshot or account response never replaces the displayed version', async () => {
  let metadata = metadataFixture()
  serve(829, () => metadata)
  await checkPerformance(829, 'all'); await settle(829)
  const entry = performanceEntry(829, 'all')
  const old = entry.state.displayed
  metadata = metadataFixture('S2')
  mockFetch(async input => Response.json(urlOf(input).pathname.endsWith('/status') ? metadata : chartFixture(999, 'S2')))
  await checkPerformance(829, 'all'); await settle(829)
  expect(entry.state.displayed).toBe(old)
  expect(entry.state.resultError?.message).toContain('不匹配')
})
