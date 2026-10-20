import { expect, test } from 'bun:test'
import { cardPerformance, currentEquity, openFullPerformance } from './performance'
import { performanceViews, performanceViewports } from '@/features/history/viewState'
import { performanceEntry } from '@/features/history/performanceCache'
import { chartFixture, metadataFixture } from '@/features/history/analysisTestFixtures'
import { sparklinePath } from '@/components/viz/sparklineGeometry'
import type { PerformanceSummary, PerformancePoint } from '@/types/api'

const summary: PerformanceSummary = {
  snapshot_id: 'one', status: 'ready', computed_at: null, observed_at: '2026-09-07T17:00:00+08:00',
  account_equity: 117128.71, account_daily_return: -0.012, points: [],
}

test('金额和日涨跌来自同一观测，日期按上海时间判断', () => {
  expect(cardPerformance(summary, Date.parse('2026-09-07T15:59:00Z'))).toMatchObject({ equity: 117128.71, pct: -1.2, dayLabel: '今日' })
  expect(cardPerformance(summary, Date.parse('2026-09-07T16:00:00Z')).dayLabel).toBe('2026-09-07')
  expect(cardPerformance().equity).toBeNull()
  expect(cardPerformance({ ...summary, account_equity: null }).equity).toBeNull()
  expect(cardPerformance({ ...summary, status: 'stale' })).toMatchObject({ equity: 117128.71, statusLabel: '绩效更新中' })
  expect(cardPerformance({ ...summary, status: 'failed' })).toMatchObject({ equity: 117128.71, statusLabel: '绩效更新失败' })
})

const point = (day: number, value: number | null): PerformancePoint => ({ date: `2026-01-${String(day).padStart(2, '0')}`, observed_at: `2026-01-${String(day).padStart(2, '0')}T00:00:00+08:00`, account_return: value, portfolio_return: null, account_daily_return: null, portfolio_daily_return: null, difference: null })

test('迷你线按观测锚点等距绘制并保留空值断点', () => {
  const path = sparklinePath([point(1, 0), point(2, .1), point(3, null), point(5, .2), point(6, .3)], 106, 36)
  expect(path).toBe('M3.00,33.00 L28.00,23.00  M78.00,13.00 L103.00,3.00')
  expect(sparklinePath([point(1, 0), point(2, 0)], 106, 36)).toBe('M3.00,18.00 L103.00,18.00')
  expect(sparklinePath([], 106, 36)).toBe('')
})

test('小图入口清除旧区间和缩放，先取状态再按固定版本预取轻图表', async () => {
  const original = globalThis.fetch
  const urls: string[] = []
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    const url = new URL(String(input), 'http://fixture')
    if (url.pathname.endsWith('/status')) return Response.json(metadataFixture('prefetched'))
    if (url.pathname.endsWith('/chart')) return Response.json(chartFixture(991, 'prefetched'))
    throw new Error(`unexpected prefetch ${url.pathname}`)
  }, { preconnect: original.preconnect })
  try {
    performanceViews.set(991, { range: '30', view: 'daily', selection: null })
    performanceViewports.set('991:all', { start: 1, end: 2 })
    openFullPerformance(991)
    const entry = performanceEntry(991, 'all')
    await entry.flight
    await entry.resultFlight
    expect(performanceViews.get(991)).toEqual({ range: 'all', view: 'cumulative', scale: 'observations', selection: null })
    expect(performanceViewports.has('991:all')).toBe(false)
    expect(urls).toHaveLength(2)
    expect(urls[0]).toBe('/api/v1/account/performance/991/status')
    const chart = new URL(urls[1], 'http://fixture')
    expect(chart.pathname).toBe('/api/v1/account/performance/991/chart')
    expect(chart.searchParams.get('snapshot_id')).toBe('prefetched')
    expect(chart.searchParams.get('range')).toBe('all')
    expect(entry.state.displayed?.snapshot_id).toBe('prefetched')
    expect(entry.state.displayed?.result.points).toHaveLength(2)
    expect(entry.state.resultError).toBeNull()
  } finally { globalThis.fetch = original }
})

test('小图预取遇到空快照只读状态，不触发计算或执行详情请求', async () => {
  const original = globalThis.fetch
  const requests: { url: string; method: string }[] = []
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ url: String(input), method: init?.method ?? 'GET' })
    return Response.json(metadataFixture(null, { status: 'empty', snapshot_version: null, read_view_schema_version: 0 }))
  }, { preconnect: original.preconnect })
  try {
    openFullPerformance(992)
    const entry = performanceEntry(992, 'all')
    await entry.flight
    expect(requests).toEqual([{ url: '/api/v1/account/performance/992/status', method: 'GET' }])
    expect(entry.state.metadata?.status).toBe('empty')
    expect(entry.state.displayed).toBeNull()
    expect(entry.state.statusError).toBeNull()
  } finally { globalThis.fetch = original }
})


test('当前资产独立于历史绩效，零余额与未查询有区别', () => {
  expect(currentEquity({ total_asset: 0 })).toBeNull()
  expect(currentEquity({ total_asset: 0, asset_observed_at: '2026-09-15' })).toBe(0)
  expect(currentEquity({ total_asset: 100000, asset_observed_at: '2026-09-15' })).toBe(100000)
  expect(cardPerformance(summary).equity).toBe(117128.71)
})

test('有前收时日涨跌用当前权益除以前收', () => {
  const now = Date.parse('2026-09-07T15:47:50+08:00')
  const live = { equity: 101544.39, observedAt: '2026-09-07T15:47:50+08:00', previousClose: 100000 }
  expect(cardPerformance(summary, now, live)).toMatchObject({ dayLabel: '今日' })
  expect(cardPerformance(summary, now, live).pct).toBeCloseTo(1.54439)
  expect(cardPerformance(summary, now, { ...live, equity: null }).pct).toBeCloseTo(-1.2)
  expect(cardPerformance(summary, now, { ...live, previousClose: null }).pct).toBeCloseTo(-1.2)
})
