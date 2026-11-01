import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { OrdersAndTrades } from '@/features/replay/ReplayDetails'
import { projectReplay } from '@/features/replay/model'
import type { ReplayWindow } from '@/features/replay/types'

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
  expect(html).toContain('成交详情 · O2')
  expect(html).toContain('定位此成交')
  expect(render(false)).toContain('disabled="">成交时刻未被覆盖')
  const future = render(true, '3:T3')
  expect(future).toContain('选中成交在游标处尚未观察到')
  expect(future).not.toContain('成交详情 · O2')
})
