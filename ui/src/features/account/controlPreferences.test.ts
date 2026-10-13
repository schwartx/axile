import { describe, expect, test } from 'bun:test'
import { canAddMetric, controlCandidates, DEFAULT_CONTROL_METRICS, metricLabel, normalizeControlPreferences, searchControlCandidates } from './controlPreferences'

describe('control summary preferences', () => {
  const operations = ['place_order', 'cancel_order', 'query_order', ...Array.from({ length: 12 }, (_, i) => `extension_${i}`)].map((key) => ({ key, display_name: key.startsWith('extension') ? `扩展${key.slice(10)}` : key, category: '查询' }))
  test('account union includes unlimited operations, excludes global and symbol-only operations', () => {
    const rows = controlCandidates(operations, operations.slice(0, 10).map((row) => row.key), [{ scope: 'account', operation: 'live_only' }, { scope: 'symbol', operation: 'symbol_only' }])
    expect(rows).toHaveLength(11)
    expect(rows.slice(0, 3).map((row) => row.category)).toEqual(['常用', '常用', '常用'])
    expect(rows.some((row) => row.key === 'extension_11' || row.key === 'symbol_only')).toBe(false)
    expect(rows.at(-1)?.name).toBe('live_only')
  })
  test('search matches names, keys and aliases while preserving order', () => {
    const rows = controlCandidates(operations, operations.map((row) => row.key), [])
    expect(searchControlCandidates(rows, ' 下单 ')[0]?.key).toBe('place_order')
    expect(searchControlCandidates(rows, '撤销委托')[0]?.key).toBe('cancel_order')
    expect(searchControlCandidates(rows, ' EXTENSION_1 ').map((row) => row.key)).toEqual(['extension_1', 'extension_10', 'extension_11'])
    expect(searchControlCandidates(rows, '扩展2')[0]?.key).toBe('extension_2')
  })
  test('duplicate combinations and maximum are rejected, alternate windows allowed', () => {
    const id = 'account:place_order:per_day'
    expect(canAddMetric([id], id)).toBe(false)
    expect(canAddMetric([id], 'account:place_order:per_minute')).toBe(true)
    expect(canAddMetric(['a', 'b', 'c'], id)).toBe(false)
  })
  test('legacy extension preferences survive, invalid preferences fall back', () => {
    const old = ['account:historical-extension:per_minute']
    expect(normalizeControlPreferences(old)).toEqual(old)
    for (const invalid of [[], ['account:x:min_interval_ms'], ['account:x:per_day', 'account:x:per_day']]) expect(normalizeControlPreferences(invalid)).toEqual(DEFAULT_CONTROL_METRICS)
    expect(metricLabel(old[0], true)).toBe('本分钟historical-extension')
    expect(metricLabel(old[0], true, { 'historical-extension': '扩展查询' })).toBe('本分钟扩展查询')
  })
})
