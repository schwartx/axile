import { expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { projectReplay } from '@/features/replay/model'
import type { ReplayWindow } from '@/features/replay/types'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

it('悬停只显示轴标签与行内读数，点击打开附近浮卡且不定位，移出和拖动清理读数', () => {
  const source = readFileSync(new URL('../../../tests/replay-hover.fixture.ts', import.meta.url), 'utf8')
  const script = `const ui=${JSON.stringify(new URL('../../', import.meta.url).pathname)};\n${source}`
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || result.stdout)
  expect(JSON.parse(result.stdout.trim())).toEqual({ noFloatingCard: true, axisLabels: true, hasReadout: true, hoverDoesNotPause: true, clearedOnLeave: true, clickSelectsWithoutSeek: true, overlappingRecords: true, futureLabeled: true, clearedOnDrag: true, marketIdentity: true, explicitSeek: true, noPin: true, anchoredBesideClick: true, fixedDuringHover: true, staysOnLeave: true, escapeCloses: true, depthClickCloses: true, emptyClickCloses: true, returnsToCursor: true, resetsView: true })
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
  expect(html).toContain('账户挂单为当前时刻的未成交剩余量，包含其他执行')
  expect(html).toContain('账户成交标注')
  expect(html).toContain('轨迹不代表排队顺序')
})

it('切换当前执行只改变归属展示，不改账户订单或数量', () => {
  const { window, projection } = chart(), before = JSON.stringify(projection)
  const html = renderToStaticMarkup(createElement(ReplayChart, { window, projection, symbol: 'X', executionId: 'E1',
    range: window.coverage[0], selectedOrder: null, selectedTrade: null, onSelectTrade: () => {}, onInspect: () => {}, onSelectOrder: () => {}, onSeek: () => {} }))
  expect(html).toContain('本次执行 3 · 1单')
  expect(html).toContain('其他执行 2 · 1单')
  expect(JSON.stringify(projection)).toBe(before)
})

it('同一时间轴包含未来成交并标记未到达，不修改游标状态', () => {
  const { window } = chart()
  window.messages = [{ schema_version: 1, recording_id: 'r', capture_seq: 6, time_us: 150, topic: '/trade', data: { symbol: 'X', order_id: 'F', trade_key: 'F', identity_quality: 'stable', price: 100, quantity: 1 } }]
  const projection = projectReplay(window, { time_us: 100 }), scene = projectReplay(window, { time_us: 199 })
  const before = JSON.stringify(projection)
  const html = renderToStaticMarkup(createElement(ReplayChart, { window, projection, scene, symbol: 'X', executionId: 'E', range: window.coverage[0], selectedOrder: null, selectedTrade: null, onSelectTrade: () => {}, onInspect: () => {}, onSelectOrder: () => {}, onSeek: () => {} }))
  expect(html).not.toContain('整段复盘')
  expect(html).not.toContain('逐步回放')
  expect(html).toContain('未到达')
  expect(html).toContain('已录自有成交 1 笔')
  expect(html).toContain('class="replay-chart-axis"')
  expect(html).toContain('aria-label="录制定位"')
  expect(projection.ownTrades).toHaveLength(0)
  expect(JSON.stringify(projection)).toBe(before)
})

it('首个盘口到达前灰显预览，同刻按采集序号生效，过期或缺口不借用未来盘口', () => {
  const window: ReplayWindow = { recording_id: 'r', window_id: 'w', context: { schema_version: 1 },
    complete: true, next_cursor: null, state_before_start: { time_us: 0, state_through_seq: 0 }, coverage: [{ start_us: 0, end_us: 300 }], gaps: [],
    messages: [100, 200].map((time, index) => ({ schema_version: 1, recording_id: 'r', capture_seq: index + 1, time_us: time,
      topic: '/quote/X', data: { symbol: 'X', book_valid: true, available: true, valid_until_us: time + 50,
        bids: [{ price: 99, quantity: index ? 9 : 3 }], asks: [{ price: 101, quantity: index ? 8 : 4 }] } })) }
  const render = (time_us: number, capture_seq?: number) => {
    const projection = projectReplay(window, { time_us, capture_seq }), before = JSON.stringify(projection)
    const html = renderToStaticMarkup(createElement(ReplayChart, { window, projection, scene: projectReplay(window, { time_us: 299 }),
      range: window.coverage[0], symbol: 'X', executionId: 'E', selectedOrder: null, selectedTrade: null,
      onSelectOrder() {}, onSelectTrade() {}, onInspect() {}, onSeek() {} }))
    expect(JSON.stringify(projection)).toBe(before)
    return html
  }
  const early = render(50)
  expect(early).toContain('未到达 · 已录买 1 / 卖 1 档')
  expect(early).toContain('class="replay-depth-ladder num" data-future="true"')
  expect(early).toContain('<span>3</span>')
  expect(early).not.toContain('<span>9</span>')
  expect(render(100, 0)).toContain('class="replay-depth-ladder num" data-future="true"')
  expect(render(100, 1)).toContain('class="replay-depth-ladder num" data-future="false"')
  expect(render(175)).toContain('盘口未记录／已失效')
  expect(render(175)).not.toContain('未到达 · 已录买')
  expect(render(200)).toContain('<span>9</span>')
  window.gaps = [{ start_us: 110, end_us: 130, topic: 'depth', symbol: 'X' }]
  expect(render(120)).toContain('盘口未记录／已失效')
  expect(render(120)).not.toContain('<span>9</span>')
})
