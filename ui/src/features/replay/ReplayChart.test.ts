import { expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { projectReplay } from '@/features/replay/model'
import type { ReplayWindow } from '@/features/replay/types'

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
  expect(html).toContain('自有量包含账户全部有效单')
  expect(html).toContain('本次成交')
  expect(html).toContain('账户上下文成交')
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
