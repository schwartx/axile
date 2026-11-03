import { expect, it } from 'bun:test'
import { rememberReplaySymbols, replayCommandPresentation, replayFieldText, replayExecutionRange, replayRange, replayStream, replaySymbols, replayTime } from '@/features/replay/presentation'
import { projectReplay } from '@/features/replay/model'
import type { ReplayData, ReplayRecording, ReplayWindow } from '@/features/replay/types'

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

function symbolWindow(recording_id: string, observations: ReplayData[], state_before_start: ReplayData = {}): ReplayWindow {
  return { recording_id, window_id: 'w', context: { schema_version: 1, instrument: {} }, state_before_start,
    messages: observations.map((data, index) => ({ schema_version: 1, recording_id, capture_seq: index + 1,
      time_us: index + 1, topic: '/execution', data })), coverage: [], gaps: [], next_cursor: null, complete: true }
}

it('后窗发现的品种累积可选，切换录制段不丢先前目录', () => {
  const first = symbolWindow('r-A', [{ symbol: 'A' }])
  const later = symbolWindow('r-A', [{ symbols: ['X'] }])
  const initial = rememberReplaySymbols({}, first)
  expect(initial).toEqual({ 'r-A': ['A'] })
  expect(rememberReplaySymbols(initial, first)).toBe(initial)
  const other = rememberReplaySymbols(initial, symbolWindow('r-B', [{ symbol: 'B' }]))
  const discovered = rememberReplaySymbols(other, later)
  expect(discovered).toEqual({ 'r-A': ['A', 'X'], 'r-B': ['B'] })
  expect(rememberReplaySymbols(discovered, first)).toBe(discovered)
})

it('窗口筛选尚未返回X时，冻结录制目录已让X可选', () => {
  const recording: ReplayRecording = { recording_id: 'r', context: { schema_version: 1, instrument: {} },
    symbols: ['A', 'X'], coverage: [] }
  const first = symbolWindow('r', [{ symbol: 'A' }])
  const known = rememberReplaySymbols({}, first)
  expect(replaySymbols(recording, known.r)).toEqual(['A', 'X'])
  expect(replaySymbols({ ...recording, symbols: undefined }, known.r)).toEqual(['A'])
})

it('嵌套账户/委托集合及窗前执行目标贡献名称，补充属性不当品种目录', () => {
  const window = symbolWindow('r', [
    { positions: [{ symbol: 'P' }], attributes: { symbol: 'not-an-instrument' } },
    { orders: [{ symbol: 'O' }, { symbol: 'P' }] },
  ], { executions: { E: { symbols: ['X'], by_symbol: { Y: { target_quantity: 5 } } } }, commands: { C: { symbol: 'C' } } })
  expect(replaySymbols(window)).toEqual(['P', 'O', 'C', 'X', 'Y'])
})

it('空集合的规范scope和旧数组scope仍贡献名称，窗前连接范围也可发现', () => {
  const window = symbolWindow('r', [
    { scope: { kind: 'symbols', symbols: ['SCOPED'] }, orders: [] },
    { scope: ['LEGACY'], positions: [] },
    { scope: { kind: 'account', symbols: [] }, attributes: { scope: ['not-an-instrument'] } },
  ], { connections: { C: { scope: { kind: 'symbols', symbols: ['BEFORE_START'] } } } })
  expect(replaySymbols(window)).toEqual(['SCOPED', 'LEGACY', 'BEFORE_START'])
})

it('请求错误文案和强调共同采用明确失败证据，无效或缺失质量不当拒绝', () => {
  for (const data of [{ phase: 'error' }, { phase: 'native_error' }, { phase: 'result', success: false },
    { phase: 'result', accepted: false, success: true }]) {
    expect(replayCommandPresentation(data)).toEqual({ failed: true, text: '观察到请求错误，订单状态仍以回报为准' })
  }
  for (const quality of ['missing', 'invalid']) {
    expect(replayCommandPresentation({ phase: 'result', accepted: false, success: false,
      field_quality: { accepted: quality, success: quality } })).toEqual({ failed: false, text: '同步结果已返回，场内状态以回报为准' })
    expect(replayCommandPresentation({ phase: 'native_error', field_quality: { phase: quality } })).toEqual({ failed: false, text: '操作结果未知，订单状态以回报为准' })
  }
  expect(replayCommandPresentation({ phase: 'native_request' })).toEqual({ failed: false, text: '请求已发出，确认另见订单回报' })
})

it('重入原生拒绝不会被同步返回true掩盖，完整前缀和检查点的请求展示一致', () => {
  const request = { command_id: 'c', operation: 'cancel_order', symbol: 'A', order_id: 'a', phase: 'request' }
  const messages = [request, { ...request, phase: 'native_error', accepted: false }, { ...request, phase: 'result', success: true }]
    .map((data, index) => ({ schema_version: 1, recording_id: 'r', capture_seq: index + 1, time_us: index + 1, topic: '/command', data }))
  const window: ReplayWindow = { ...symbolWindow('r', []), messages, state_before_start: { recording_id: 'r', time_us: 0, state_through_seq: 0 } }
  const full = projectReplay(window, { time_us: 3 })
  const restore = projectReplay({ ...window, messages: [], state_before_start: { recording_id: 'r', time_us: 3, state_through_seq: 3,
    commands: { c: { ...messages[2].data, accepted: false, observed_seq: 3, observed_at_us: 3 } } } }, { time_us: 3 })
  expect(replayCommandPresentation(full.commands[0].data).failed).toBe(false)
  expect(replayCommandPresentation(full.commands.at(-1)!.data).failed).toBe(true)
  expect(replayCommandPresentation(restore.commands[0].data)).toEqual(replayCommandPresentation(full.commands.at(-1)!.data))
})


it('共用录制中的后一执行从自己的开始时间定位，缺口只向后选覆盖段', () => {
  const coverage = [{ start_us: 100, end_us: 200 }, { start_us: 300, end_us: 500 }, { start_us: 600, end_us: 900 }]
  expect(replayExecutionRange(coverage, 650, 1000)).toEqual({ start_us: 650, end_us: 900 })
  expect(replayExecutionRange(coverage, 550, 1000)).toEqual({ start_us: 600, end_us: 900 })
  expect(replayExecutionRange(coverage, 650, 20)).toEqual({ start_us: 650, end_us: 670 })
  expect(replayExecutionRange(coverage, null, 1000)).toEqual({ start_us: 100, end_us: 200 })
  expect(replayExecutionRange(coverage, 900, 1000)).toBeNull()
})
