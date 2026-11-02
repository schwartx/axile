import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { OrdersAndTrades } from '@/features/replay/ReplayDetails'
import { projectReplay } from '@/features/replay/model'
import type { ReplayWindow } from '@/features/replay/types'

test('委托补充信息在选中行下方展开，移除常驻详情栏并保留完整字段入口', () => {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 },
    complete: true, next_cursor: null, state_before_start: { time_us: 0, state_through_seq: 0 }, coverage: [{ start_us: 0, end_us: 300 }], gaps: [],
    messages: [1, 2].map(index => ({ schema_version: 1, recording_id: 'r', capture_seq: index, time_us: index * 100,
      topic: '/order', data: { symbol: 'X', order_id: `O${index}`, price: 99, quantity: 1, status: 'working', direction: 'BUY' } })) }
  const projection = projectReplay(window, { time_us: 200 })
  const render = (id: string) => renderToStaticMarkup(createElement(OrdersAndTrades, {
    projection, context: window.context, symbol: 'X', selected: JSON.stringify(['X', id]), onSelect: () => {}, executionId: 'E',
    selectedTradeKey: null, onSelectTrade: () => {}, tab: 'orders', onTabChange: () => {}, onSeek: () => {}, coverage: window.coverage,
  }))
  for (const id of ['O1', 'O2']) {
    const html = render(id)
    expect(html).not.toContain('<aside')
    expect(html).not.toContain('委托详情')
    expect(html).toContain(`id="replay-order-extra-${encodeURIComponent(JSON.stringify(['X', id]))}" class="replay-row-expansion" data-open="true"`)
    expect(html.match(/class="replay-row-expansion" data-open="true"/g)).toHaveLength(1)
    expect(html).toContain('排队未知')
    expect(html).toContain('完整记录')
    const fields = [...html.matchAll(/id="(replay-order-fields-[^"]+)"/g)].map(match => match[1])
    expect(new Set(fields).size).toBe(2)
    for (const field of fields) expect(html).toContain(`popoverTarget="${field}"`)
  }
})

test('成交默认详情与列表选中项一致，覆盖外时间明确禁用定位', () => {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 },
    complete: true, next_cursor: null, state_before_start: { time_us: 0, state_through_seq: 0 }, coverage: [{ start_us: 0, end_us: 300 }], gaps: [],
    messages: [1, 2].map(index => ({ schema_version: 1, recording_id: 'r', capture_seq: index, time_us: index * 100,
      topic: '/trade', data: { symbol: 'X', trade_key: `T${index}`, order_id: `O${index}`, identity_quality: 'stable',
        quantity: index, price: 99, direction: 'BUY', execution_id: 'E' } })) }
  const projection = projectReplay(window, { time_us: 200 })
  const render = (covered: boolean, selectedTradeKey: string | null = null) => renderToStaticMarkup(createElement(OrdersAndTrades, {
    projection, context: window.context, symbol: 'X', selected: null, onSelect: () => {}, executionId: 'E',
    selectedTradeKey, onSelectTrade: () => {}, tab: 'trades', onTabChange: () => {}, onSeek: () => {}, coverage: covered ? window.coverage : [],
  }))
  const html = render(true)
  const pressed = html.match(/<button(?=[^>]*aria-pressed="true")[^>]*>[\s\S]*?<\/button>/g) ?? []
  expect(pressed.some(button => button.includes('O2'))).toBe(true)
  expect(pressed.some(button => button.includes('O1'))).toBe(false)
  expect(html).not.toContain('<aside')
  expect(html).toContain(`id="replay-trade-extra-${encodeURIComponent('2:T2')}" class="replay-row-expansion" data-open="true"`)
  expect(html).toContain(`id="replay-trade-extra-${encodeURIComponent('1:T1')}" class="replay-row-expansion" data-open="false" inert=""`)
  const ids = [...html.matchAll(/id="(replay-trade-fields-[^"]+)"/g)].map(match => match[1])
  expect(new Set(ids).size).toBe(2)
  expect(html).toContain('定位此成交')
  expect(render(false)).toContain('disabled="">成交时刻未被覆盖')
  const future = render(true, '3:T3')
  expect(future).toContain('选中成交在游标处尚未观察到')
  expect(future).not.toContain('class="replay-row-expansion" data-open="true"')
})
