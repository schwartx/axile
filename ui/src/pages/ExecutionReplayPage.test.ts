import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

const source = readFileSync(new URL('../../tests/replay-page.fixture.ts', import.meta.url), 'utf8')
function pageCase(scenario: string) {
  const script = `const ui=${JSON.stringify(new URL('../', import.meta.url).pathname)}; const scenario=${JSON.stringify(scenario)};\n${source}`
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

test('主图时间轴定位实时更新游标，采用最终位置并保持暂停', () => {
  const result = pageCase('draft-commit')
  expect(result.preview).toContain('08:00:05.000000')
  expect(result.chartTimeBeforeCommit).toBe(5_000_000)
  expect(result.cursor).toContain('08:00:07.000000')
  expect(result.playing).toBe(false)
})

test('五档倍速保持游标与播放状态，慢放及播放中调速按新倍率连续推进', () => {
  const result = pageCase('speed')
  expect(result.options).toEqual(['0.1', '0.25', '0.5', '1', '2'].map(value => ({ value, label: `${value}×` })))
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
  expect(result.retained).toBe(true)
  expect(result.after).toBe(true)
  expect(result.cursor).toContain('08:00:01.000000')
  expect(result.playing).toBe(false)
})

test('从后窗选择此前成交时，重新读取真实成交所在窗口而非夹到当前窗起点', () => {
  const result = pageCase('trade-seek')
  expect(result.cursor).toContain('08:00:05.000000')
  expect(result.playing).toBe(false)
})

test('窗口播放结束后再次播放，从当前窗口起点重新开始', () => {
  const result = pageCase('restart')
  expect(result.stopped).toContain('08:00:39.999999')
  expect(result.cursor).toContain('08:00:20.500000')
  expect(result.playing).toBe(true)
})

test('跨窗口加载期间隐藏，加载完成与返回页面都保持暂停', () => {
  const result = pageCase('hidden')
  expect(result.hiddenPlaying).toBe(false)
  expect(result.playing).toBe(false)
  expect(result.cursor).toContain('08:00:20.000000')
})

test('隐藏后在新窗完成前返回，也不恢复先前的自动续播', () => {
  const result = pageCase('hidden-return-before-load')
  expect(result.hiddenPlaying).toBe(false)
  expect(result.playing).toBe(false)
  expect(result.cursor).toContain('08:00:20.000000')
})

test('没有主动暂停时，下一窗口读取完成后继续播放', () => {
  const result = pageCase('continue')
  expect(result.playing).toBe(true)
  expect(result.cursor).toContain('08:00:20.000000')
})

test('下一窗读取中显式定位回缓存首窗，仍应用新定位并丢弃迟到响应', () => {
  const result = pageCase('cached-seek')
  expect(result.initial).toContain('08:00:10.000000')
  expect(result.cursor).toContain('08:00:00.000000')
  expect(result.afterLateResponse).toBe(result.cursor)
  expect(result.afterNextSeek).toContain('08:00:05.000000')
})

test.each(['drag', 'duration'])('跨窗读取中主动%s，响应到达后仍保持暂停', action => {
  const result = pageCase(`pause-${action}`)
  expect(result.pausedBeforeLoad).toBe(true)
  expect(result.playingAfterLoad).toBe(false)
  if (action === 'drag') expect(result.seekLabel).toContain('08:00:25.000000')
})

test('首窗读取失败后仍能定位后窗，播放及步进不执行失效状态', () => {
  const result = pageCase('missing')
  expect(result.failed).toEqual({ seek: true, playDisabled: true, nextDisabled: false })
  expect(result.recovered).toBe(true)
  expect(result.orders).toBe(true)
})

test('读取失败时在原窗口内改定位也重新请求，而非只改失效游标', () => {
  const result = pageCase('missing-seek')
  expect(result.recovered).toBe(true)
  expect(result.orders).toBe(true)
  expect(result.cursor).toContain('08:00:00.500000')
})

test('实际订单列表及详情区分归属，冲突保留执行身份且不误计本次', () => {
  const result = pageCase('ownership')
  expect(result.O1.row).toContain('其他执行')
  expect(result.O1.detail).toContain('其他执行 · 执行 E1')
  expect(result.O2.row).toContain('本次执行')
  expect(result.O2.detail).toContain('本次执行 · 执行 E2')
  expect(result.M.row).toContain('人工')
  expect(result.M.detail).toContain('归属： 人工')
  expect(result.U.row).toContain('归属未知')
  expect(result.C.row).toContain('归属未知')
  expect(result.C.detail).toContain('归属未知 · 执行 E2')
})


test('点选图中成交暂停播放并关联委托及详情，但不改变游标或已应用观察', () => {
  const result = pageCase('inspect')
  expect(result.linked).toMatchObject({ order: JSON.stringify(['X', 'O1']), trade: '3:T', chartTrade: '3:T', tab: 'trades' })
  expect(result.unchanged).toBe(true)
  expect(result.playing).toBe(false)
  expect(result.selectedOrder).toBe(JSON.stringify(['X', 'M']))
  expect(result.selectedTrade).toBeNull()
  expect(result.tab).toBe('orders')
})

test('默认读取整段，后续图层与游标状态独立，未来选择和倒退不泄漏终态', () => {
  const result = pageCase('overview')
  expect(result.initial).toMatchObject({ range: { start_us: 0, end_us: 40_000_000 }, sceneTrades: 1, currentTrades: 0, sceneStatus: 'filled', currentStatus: 'working' })
  expect(result.earlyBook).toBe(1)
  expect(result.lateBook).toBe(9)
  expect(result.restoredBook).toBe(1)
  expect(result.preview).toContain('未到达')
  expect(result.preview).not.toContain('已成交')
  expect(result.future).toEqual({ selected: '5:future', currentTrades: 0 })
})

test('后移短窗再切回整段，恢复当前覆盖时段起点且不跨越缺口', () => {
  expect(pageCase('whole-after-window')).toEqual([
    { later: { start_us: 2_000_000, end_us: 4_000_000 }, whole: { start_us: 0, end_us: 40_000_000 } },
    { later: { start_us: 62_000_000, end_us: 64_000_000 }, whole: { start_us: 60_000_000, end_us: 100_000_000 } },
  ])
})
