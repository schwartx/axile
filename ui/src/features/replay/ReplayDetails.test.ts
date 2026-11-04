import { expect, test } from 'bun:test'
import { Children, createElement, isValidElement, type ReactElement, type ReactNode } from 'react'
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
    expect(new Set(fields).size).toBe(1)
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
  expect(new Set(ids).size).toBe(1)
  expect(html).not.toContain('定位此成交')
  expect(render(false)).toContain('此记录时刻未被录制覆盖，无法定位')
  const future = render(true, '3:T3')
  expect(future).toContain('所选成交不在这段记录中。')
  expect(future).not.toContain('class="replay-row-expansion" data-open="true"')
})

test('未来委托展示首条观察，经过游标后按当时状态恢复，未来成交不进入当前统计', () => {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 },
    complete: true, next_cursor: null, state_before_start: { time_us: 0, state_through_seq: 0 }, coverage: [{ start_us: 0, end_us: 300 }], gaps: [],
    messages: [
      { schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 100, topic: '/order', data: { symbol: 'X', order_id: 'O', execution_id: 'E', direction: 'BUY', price: 99, quantity: 2, filled_quantity: 0, remaining_quantity: 2, status: 'working', confirmed_book: true, sync_generation: 1 } },
      { schema_version: 1, recording_id: 'r', capture_seq: 2, time_us: 100, topic: '/trade', data: { symbol: 'X', order_id: 'O', trade_key: 'T', identity_quality: 'stable', execution_id: 'E', direction: 'BUY', price: 101, quantity: 2 } },
      { schema_version: 1, recording_id: 'r', capture_seq: 3, time_us: 200, topic: '/order', data: { symbol: 'X', order_id: 'O', execution_id: 'E', direction: 'BUY', price: 101, quantity: 2, filled_quantity: 2, remaining_quantity: 0, status: 'filled', sync_generation: 1 } },
    ] }
  const scene = projectReplay(window, { time_us: 299 })
  const render = (time_us: number, capture_seq?: number) => {
    const projection = projectReplay(window, { time_us, capture_seq }), before = JSON.stringify(projection)
    const props = { projection, scene, context: window.context, symbol: 'X', selected: JSON.stringify(['X', 'O']),
      selectedTradeKey: '2:T', onSelect() {}, onSelectTrade() {}, executionId: 'E', onTabChange() {}, onSeek() {}, coverage: window.coverage }
    const html = renderToStaticMarkup(createElement(OrdersAndTrades, { ...props, tab: 'orders' }))
    const trades = renderToStaticMarkup(createElement(OrdersAndTrades, { ...props, tab: 'trades' }))
    expect(JSON.stringify(projection)).toBe(before)
    return { html, trades, projection, row: html.match(/<tr data-future="(?:true|false)"[\s\S]*?<\/tr>/)![0] }
  }
  const early = render(50)
  expect(early.row).toContain('data-future="true"')
  expect(early.row).toContain('未到达')
  expect(early.row).toContain('>99</td>')
  expect(early.row).toContain('— / 2')
  expect(early.row).not.toContain('已成交')
  expect(early.html).not.toContain('定位此委托')
  expect(early.html).not.toContain('这段记录没有')
  expect(early.projection.executionTotals).toEqual({})
  expect(early.projection.ownTrades).toHaveLength(0)
  expect(Object.keys(early.projection.observedPositions)).toHaveLength(0)
  const sameTime = render(100, 1)
  expect(sameTime.row).toContain('data-future="false"')
  expect(sameTime.row).toContain('已受理')
  expect(sameTime.trades).toContain('<button data-future="true"')
  expect(sameTime.projection.ownTrades).toHaveLength(0)
  const reached = render(100, 2)
  expect(reached.trades).toContain('<button data-future="false"')
  expect(reached.projection.executionTotals.E.X).toBe(2)
  const final = render(200)
  expect(final.row).toContain('已成交')
  expect(final.row).toContain('2 / 2')
  expect(final.row).toContain('>101</td>')
  expect(render(50).row).toBe(early.row)
})

test('已到达成交使用当时字段，后续关联信息在到达前不覆盖列表', () => {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 },
    complete: true, next_cursor: null, state_before_start: { time_us: 0, state_through_seq: 0 }, coverage: [{ start_us: 0, end_us: 300 }], gaps: [],
    messages: [
      { schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 100, topic: '/trade', data: { symbol: 'X', trade_key: 'T', identity_quality: 'stable', price: 99, quantity: 1 } },
      { schema_version: 1, recording_id: 'r', capture_seq: 2, time_us: 200, topic: '/trade', data: { symbol: 'X', trade_key: 'T', identity_quality: 'stable', order_id: 'LATER-ORDER' } },
    ] }
  const render = (time_us: number) => renderToStaticMarkup(createElement(OrdersAndTrades, { projection: projectReplay(window, { time_us }),
    scene: projectReplay(window, { time_us: 299 }), context: window.context, symbol: 'X', selected: null, selectedTradeKey: null,
    onSelect() {}, onSelectTrade() {}, executionId: 'E', tab: 'trades', onTabChange() {}, onSeek() {}, coverage: window.coverage }))
  expect(render(100)).toContain('委托待关联')
  expect(render(100)).not.toContain('LATER-ORDER')
  expect(render(200)).toContain('LATER-ORDER')
})

type RecordEvents = {
  children?: ReactNode
  'data-future'?: boolean
  onClick?: () => void
  onDoubleClick?: () => void
}
function findElement(node: ReactNode, predicate: (element: ReactElement<RecordEvents>) => boolean): ReactElement<RecordEvents> | undefined {
  for (const child of Children.toArray(node)) {
    if (!isValidElement<RecordEvents>(child)) continue
    if (predicate(child)) return child
    const found = findElement(child.props.children, predicate)
    if (found) return found
  }
}

test('委托和成交单击选中并定位；未来记录可定位，覆盖边界外仅选中', () => {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 }, complete: true,
    next_cursor: null, state_before_start: { time_us: 0, state_through_seq: 0 }, coverage: [{ start_us: 100, end_us: 300 }], gaps: [],
    messages: ['/order', '/trade'].map((topic, index) => ({ schema_version: 1, recording_id: 'r', capture_seq: index + 1,
      time_us: 100, topic, data: { symbol: 'X', order_id: 'O', trade_key: 'T', identity_quality: 'stable', price: 99, quantity: 1, status: 'working' } })) }
  const scene = projectReplay(window, { time_us: 200 })
  for (const cursor of [50, 200]) for (const end of [100, 300]) {
    const selected: string[] = [], sought: number[] = []
    for (const type of ['tr', 'button']) {
      const tree = OrdersAndTrades({ projection: projectReplay(window, { time_us: cursor }), scene, context: window.context,
        symbol: 'X', selected: null, selectedTradeKey: null, onSelect: key => selected.push(key), onSelectTrade: key => selected.push(key),
        executionId: 'E', tab: type === 'tr' ? 'orders' : 'trades', onTabChange() {}, onSeek: time => sought.push(time), coverage: [{ start_us: 100, end_us: end }] })
      const row = findElement(tree, element => element.type === type && element.props['data-future'] !== undefined)!
      expect(row.props['data-future']).toBe(cursor < 100)
      const count = selected.length
      row.props.onClick!()
      expect(selected).toHaveLength(count + 1)
      expect(row.props.onDoubleClick).toBeUndefined()
      expect(sought).toEqual(end === 300 ? [100] : [])
      sought.length = 0
    }
  }
})
