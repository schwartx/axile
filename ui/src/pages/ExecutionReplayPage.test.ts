import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

const source = readFileSync(new URL('../../tests/replay-page.fixture.ts', import.meta.url), 'utf8')
function pageCase(scenario: string) {
  const script = 'const ui=' + JSON.stringify(new URL('../', import.meta.url).pathname) + '; const scenario=' + JSON.stringify(scenario) + ';\n' + source
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || result.stdout || String(result.error))
  return JSON.parse(result.stdout.trim())
}

test('统一页头及多品种选择接入现有组件，不渲染原生下拉或range', () => {
  const result = pageCase('controls')
  expect(result.title).toMatchObject({ accountId: 7, name: 'demo', page: '执行回放', flip: false, back: { to: '/accounts/7/executions/E2' } })
  expect(result.searchable).toBe(true)
  expect(result.symbol).toBe('Y')
  expect(result.nativeControl).toBe(false)
})

test('回放按入口返回，保留列表筛选和详情原始来源，拒绝外部及跨账户目标', () => {
  expect(pageCase('return-journal')).toEqual({ to: '/accounts/7/executions?status=执行失败&range=7', label: '返回执行记录' })
  expect(pageCase('return-detail')).toEqual({ to: '/accounts/7/executions/E2', label: '返回执行详情', state: { journalReturn: '/accounts/7/executions?range=7' } })
  expect(pageCase('return-performance')).toEqual({ to: '/accounts/7/executions/E2', label: '返回执行详情', state: { performanceReturn: '/accounts/7/history' } })
  for (const scenario of ['return-external', 'return-other-account']) {
    expect(pageCase(scenario)).toEqual({ to: '/accounts/7/executions/E2', label: '返回执行详情' })
  }
})

test('主图时间轴定位实时更新游标并保持暂停', () => {
  const result = pageCase('seek')
  expect(result.preview).toContain('08:00:05.000000')
  expect(result.chartTime).toBe(5_000_000)
  expect(result.cursor).toContain('08:00:07.000000')
  expect(result.playing).toBe(false)
})

test('五档倍速保持游标与播放状态，播放中调速连续推进', () => {
  const result = pageCase('speed')
  expect(result.options).toEqual(['0.1', '0.25', '0.5', '1', '2'].map(value => ({ value, label: value + '×' })))
  expect(result.initial).toBe('1')
  expect(result.paused).toMatchObject({ playing: false, speed: '0.1' })
  expect(result.paused.cursor).toContain('08:00:05.000000')
  expect(result.slow).toContain('08:00:05.100000')
  expect(result.switched).toEqual({ cursor: result.slow, playing: true })
  expect(result.fast).toContain('08:00:07.100000')
})

test('详情默认选中已观察的本次委托，并保留用户选择的账户上下文单', () => {
  const result = pageCase('selection')
  expect(result.initial).toBe(JSON.stringify(['X', 'O2']))
  expect(result.selected).toBe(JSON.stringify(['X', 'M']))
})

test('首屏保持真实起点，显式定位首个盘口后才展示该观察且仍暂停', () => {
  const result = pageCase('first-quote')
  expect(result.before).toBe(false)
  expect(result.prominent).toBe(true)
  expect(result.after).toBe(true)
  expect(result.cursor).toContain('08:00:01.000000')
  expect(result.playing).toBe(false)
})

test('点选图中成交暂停播放并关联委托及详情，但不改变游标或已应用观察', () => {
  const result = pageCase('inspect')
  expect(result.linked).toMatchObject({ order: JSON.stringify(['X', 'O1']), trade: 'r:3:T', chartTrade: 'r:3:T', tab: 'trades' })
  expect(result.unchanged).toBe(true)
  expect(result.playing).toBe(false)
  expect(result.selectedOrder).toBe(JSON.stringify(['X', 'M']))
  expect(result.selectedTrade).toBeNull()
  expect(result.tab).toBe('orders')
})

test('整段图层与游标状态独立，未来选择和倒退不泄漏终态', () => {
  const result = pageCase('overview')
  expect(result.initial).toMatchObject({ range: { start_us: 0, end_us: 40_000_000 }, sceneTrades: 1, currentTrades: 0, sceneStatus: 'filled', currentStatus: 'working' })
  expect(result.earlyBook).toBe(1)
  expect(result.lateBook).toBe(9)
  expect(result.restoredBook).toBe(1)
  expect(result.preview).toContain('未到达')
  expect(result.preview).not.toContain('已成交')
  expect(result.future).toEqual({ selected: 'r:5:future', currentTrades: 0 })
})

test('整次执行跨录制段展示并播放，缺口内不沿用盘口和委托状态', () => {
  const result = pageCase('full-execution')
  expect(result.initial).toEqual({ range: { start_us: 0, end_us: 40_000_000 }, slices: [
    ['r0', { start_us: 0, end_us: 10_000_000 }], ['r1', { start_us: 20_000_000, end_us: 40_000_000 }],
  ], quotes: [[1_000_000, 10_000_000], [21_000_000, 40_000_000]], cursor: 0 })
  expect(result.hasRecordingDialog).toBe(false)
  expect(result.requests[0].ranges).toEqual([
    { recording: 'r0', start_us: 0, end_us: 10_000_000 },
    { recording: 'r1', start_us: 20_000_000, end_us: 40_000_000 },
  ])
  expect(result.gap.quotes).toEqual([])
  expect(result.gap.orders).toEqual([])
  expect(result.gap.status).toBe(true)
  expect(result.gap.rows).toContain('状态未知')
  expect(result.later).toEqual({ quantity: 9, trades: ['r0:4:T', 'r1:4:T'] })
  expect(result.played).toBe(25_000_000)
})

test('共用录制打开一次执行时，读取范围和游标从该执行开始', () => {
  const result = pageCase('selected-execution')
  expect(result.range).toEqual({ start_us: 25_000_123, end_us: 40_000_000 })
  expect(result.cursor).toBe(25_000_123)
  expect(result.playing).toBe(false)
})

test('页面隐藏后暂停，返回时不自动恢复播放', () => {
  const result = pageCase('hidden')
  expect(result.paused).toBe(true)
  expect(result.after).toBe(result.atHide)
  expect(result.playing).toBe(false)
})

test('播放结束后再次播放，从执行起点重新开始', () => {
  const result = pageCase('restart')
  expect(result.stopped).toContain('08:00:39.999999')
  expect(result.cursor).toContain('08:00:00.500000')
  expect(result.playing).toBe(true)
})

test('全程读取失败可以重试，不展示失效状态', () => {
  expect(pageCase('retry')).toEqual({ failed: true, recovered: true, readCount: 2 })
})

test('全屏保留游标和明细页签，按钮和浏览器退出均同步状态', () => {
  expect(pageCase('fullscreen')).toEqual({ entered: true, label: '退出全屏', preserved: true, error: false, escaped: true, exited: true })
})

test('浏览器拒绝全屏时提示失败并保持普通布局', () => {
  expect(pageCase('fullscreen-error')).toMatchObject({ entered: false, label: '全屏', preserved: true, error: true, exited: true })
})
