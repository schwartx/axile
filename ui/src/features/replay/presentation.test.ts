import { expect, it } from 'bun:test'
import { replayFieldText, replayRange, replayStream, replaySymbols, replayTime } from '@/features/replay/presentation'
import type { ReplayWindow } from '@/features/replay/types'

it('显示才转换毫秒，标签保留微秒并显示上海跨日', () => {
  expect(replayTime(Date.parse('2026-01-01T16:30:00Z') * 1000 + 123456, true)).toBe('2026-01-02 00:30:00.123456')
})

it('只渲染声明允许的标量，不把非法数值当零', () => {
  expect(replayFieldText(false, { key: 'x', label: '示例', kind: 'bool' })).toBe('否')
  expect(replayFieldText({ password: 'x' }, { key: 'x', label: '示例', kind: 'text' })).toBeNull()
  expect(replayFieldText(NaN, { key: 'x', label: '示例', kind: 'number' })).toBeNull()
})

it('窗口不跨越覆盖缺口，未覆盖的seek不使用未来数据', () => {
  const coverage = [{ start_us: 100, end_us: 200 }, { start_us: 300, end_us: 500 }]
  expect(replayRange(coverage, 150, 100)).toEqual({ start_us: 150, end_us: 200 })
  expect(replayRange(coverage, 250)).toBeNull()
})

it('渠道模板只提供覆盖说明，不能把未观察品种标为完整可用', () => {
  expect(replayStream({ schema_version: 1, stream_defaults: { depth: { availability: 'available' } } }, 'A', 'depth')?.availability).toBe('partial')
  expect(replayStream({ schema_version: 1, streams: { A: { depth: { availability: 'unavailable', reason: '缺失' } } }, stream_defaults: { depth: { availability: 'available' } } }, 'A', 'depth')?.reason).toBe('缺失')
})

it('缺少品种元数据时仍可发现实际录到的品种名称', () => {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 }, state_before_start: { orders: { a: { symbol: 'A' } } }, messages: [{ schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 1, topic: '/execution', data: { symbols: ['B'] } }], coverage: [], gaps: [], next_cursor: null, complete: true }
  expect(replaySymbols(window)).toEqual(['B', 'A'])
})
