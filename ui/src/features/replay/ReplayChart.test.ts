import { expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { projectReplay } from '@/features/replay/model'
import type { ReplayWindow } from '@/features/replay/types'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

it('悬浮详情可移入与固定，查看不定位，取消固定离开关闭，显式定位精确成交时间', () => {
  const source = readFileSync(new URL('../../../tests/replay-hover.fixture.ts', import.meta.url), 'utf8')
  const script = `const ui=${JSON.stringify(new URL('../../', import.meta.url).pathname)};\n${source}`
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || result.stdout)
  expect(JSON.parse(result.stdout.trim())).toEqual({ entered: true, previewCompact: true, overlappingMarketAccessible: true, bubbleDirectionsDistinct: true, marketClearsOrderIdentity: true, futureLabeled: true, pinned: true, inspection: { seeks: [], selected: ['3:T'], pauses: 6, unchanged: true }, closedAfterUnpin: true, located: { seeks: [50, 3], closed: true }, clickPinsWithoutSeek: true, escapeClosed: true })
})

function chart() {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 }, messages: [],
    complete: true, next_cursor: null, coverage: [{ start_us: 100, end_us: 200 }], gaps: [],
    state_before_start: { time_us: 100, state_through_seq: 5, orders: Object.fromEntries([
      { order_id: 'O1', execution_id: 'E1', quantity: 3, filled_quantity: 0, remaining_quantity: 3 },
      { order_id: 'O2', execution_id: 'E2', quantity: 2, filled_quantity: 0, remaining_quantity: 2 },
      { order_id: 'M', execution_id: null, origin: 'manual', quantity: 4, filled_quantity: 0, remaining_quantity: 4 },
      { order_id: 'U', execution_id: null, quantity: 1, remaining_quantity: null },
    ].map(order => [JSON.stringify(['X', order.order_id]), { symbol: 'X', price: 100, status: 'working',
      confirmed_book: true, sync_generation: 1, ...order }])) } }
  return { window, projection: projectReplay(window, { time_us: 100 }) }
}

it('真实图表组件的可读ORD按归属列量，不把他单与未知归入本次', () => {
  const { window, projection } = chart()
  const html = renderToStaticMarkup(createElement(ReplayChart, { window, projection, symbol: 'X', executionId: 'E2',
    range: window.coverage[0], selectedOrder: null, selectedTrade: null, onSelectTrade: () => {}, onInspect: () => {}, onSelectOrder: () => {}, onSeek: () => {} }))
  expect(html).toContain('本次执行 2 · 1单')
  expect(html).toContain('其他执行 3 · 1单')
  expect(html).toContain('人工 4 · 1单')
  expect(html).toContain('归属未知 未知 · 1单')
  expect(html).toContain('实线为本次，点线为账户上下文')
  expect(html).toContain('账户挂单是游标时刻该账户在此价位有效委托的未成交剩余量，包含其他执行')
  expect(html).toContain('账户逐笔详情与选中标注')
  expect(html).toContain('不再叠菱形、方框或选中圆环')
})

it('切换当前执行只改变归属展示，不改账户订单或数量', () => {
  const { window, projection } = chart(), before = JSON.stringify(projection)
  const html = renderToStaticMarkup(createElement(ReplayChart, { window, projection, symbol: 'X', executionId: 'E1',
    range: window.coverage[0], selectedOrder: null, selectedTrade: null, onSelectTrade: () => {}, onInspect: () => {}, onSelectOrder: () => {}, onSeek: () => {} }))
  expect(html).toContain('本次执行 3 · 1单')
  expect(html).toContain('其他执行 2 · 1单')
  expect(JSON.stringify(projection)).toBe(before)
})

it('默认整段图含后续成交，盘口可读状态仍只取游标前缀，定位轴位于主图内', () => {
  const { window } = chart()
  window.messages = [{ schema_version: 1, recording_id: 'r', capture_seq: 6, time_us: 150, topic: '/trade', data: { symbol: 'X', order_id: 'F', trade_key: 'F', identity_quality: 'stable', price: 100, quantity: 1 } }]
  const projection = projectReplay(window, { time_us: 100 }), scene = projectReplay(window, { time_us: 199 })
  const before = JSON.stringify(projection)
  const html = renderToStaticMarkup(createElement(ReplayChart, { window, projection, scene, symbol: 'X', executionId: 'E', range: window.coverage[0], selectedOrder: null, selectedTrade: null, onSelectTrade: () => {}, onInspect: () => {}, onSelectOrder: () => {}, onSeek: () => {} }))
  expect(html).toContain('data-review-mode="review"')
  expect(html).toContain('已录自有成交 1 笔')
  expect(html).toContain('class="replay-chart-axis"')
  expect(html).toContain('aria-label="录制定位"')
  expect(projection.ownTrades).toHaveLength(0)
  expect(JSON.stringify(projection)).toBe(before)
})
