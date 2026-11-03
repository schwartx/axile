import { describe, expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { currentReplayQuote, replayChartLayout, replayHeatCells, replayPriceY } from '@/features/replay/chartGeometry'
import { replayOrderFocus } from '@/features/replay/chartInteraction'
import { recordedDepth, replayBestLevel, replayDepthBuckets, replayObservedPriceView, replayOrderBookPosition, replayOrderPositionContext, replayTradeLiquidity } from '@/features/replay/depthGeometry'
import { ReplayDepthLadder } from '@/features/replay/ReplayDepthLadder'
import type { ReplayOrder, ReplayProjection, ReplayQuote } from '@/features/replay/types'
import type { ReactElement } from 'react'

const quote = (bids = 2, asks = bids): ReplayQuote => ({ symbol: 'X', bids: Array.from({ length: bids }, (_, index) => ({ price: 100 - index, quantity: index + 1 })),
  asks: Array.from({ length: asks }, (_, index) => ({ price: 102 + index, quantity: index + 1 })), book_valid: true, available: true, valid_until_us: 1000,
  available_depth: 5, depth_coverage: null, last_price: null, last_price_kind: 'unknown', time_us: 100, capture_seq: 1 })
const order = (rest: Partial<ReplayOrder> = {}): ReplayOrder => ({ key: 'O', order_id: 'O', symbol: 'X', direction: 'BUY', price: 99,
  confirmed: true, terminal: false, remaining_volume: 1, time_us: 200, capture_seq: 2, ...rest } as ReplayOrder)
const layout = replayChartLayout(1200, 420), range = { start_us: 0, end_us: 2000 }

describe('动态盘口及委托位置', () => {
  it.each([[2, 2], [4, 4], [2, 7], [60, 80]])('实际买%d卖%d档不受 available_depth 固定值影响，聚合也不丢档', (bids, asks) => {
    const depth = quote(bids, asks), levels = recordedDepth(depth), view = replayObservedPriceView([depth], [], [], 1)!
    const buckets = replayDepthBuckets(levels, [], view, layout)
    expect(levels).toHaveLength(bids + asks)
    expect(buckets.flatMap(bucket => bucket.levels)).toHaveLength(bids + asks)
    expect(buckets.every(bucket => bucket.labelY >= layout.top && bucket.labelY <= layout.bottom)).toBe(true)
    expect(buckets.every((bucket, index) => index === 0 || bucket.labelY - buckets[index - 1].labelY >= 17.99)).toBe(true)
    if (bids + asks > 100) expect(buckets.some(bucket => bucket.minPrice !== bucket.maxPrice)).toBe(true)
  })
  it('1224桌面高度仍直接列出全部十档，数量不大时不提前聚合', () => {
    const depth = quote(5), small = replayChartLayout(992, 281), view = replayObservedPriceView([depth], [], [], 1)!
    const buckets = replayDepthBuckets(recordedDepth(depth), [], view, small)
    expect(buckets).toHaveLength(10)
    expect(buckets.every(bucket => bucket.levels.length === 1)).toBe(true)
  })
  it('盘口读数行保留真实价格引导，挂量柱与读数同行', () => {
    const depth = quote(5), view = { min: 90, max: 115, tick: 1 }
    const buckets = replayDepthBuckets(recordedDepth(depth), [order()], view, layout)
    const row = buckets.find(bucket => bucket.minPrice === 99)!
    expect(row.y).toBe(replayPriceY(99, view, layout))
    const html = renderToStaticMarkup(createElement(ReplayDepthLadder, { quote: depth, orders: [order()], view, layout, height: 420,
      executionId: 'E', selected: 'O', onSelect: () => {}, onZoom: () => {}, priceText: String, quantityText: String }))
    for (let rank = 1; rank <= 5; rank++) { expect(html).toContain(`买${rank}`); expect(html).toContain(`卖${rank}`) }
    expect(html).toContain('价位不代表排队顺序')
    expect(html).not.toContain(' title=')
    expect((html.match(/class="replay-ladder-amount"/g) ?? [])).toHaveLength(10)
  })
  it('零量保留但不是买一；未知量保留价位，聚合不冒充零', () => {
    const depth = quote(4)
    depth.bids[0].quantity = 0; depth.bids[1].quantity = null
    expect(replayBestLevel(depth, '买')?.price).toBe(99)
    const view = { min: 90, max: 110, tick: 1 }
    const rows = replayDepthBuckets(recordedDepth(depth), [], view, layout)
    expect(rows.find(row => row.minPrice === 99)).toMatchObject({ quantity: null, unknown: true })
    expect(rows.find(row => row.minPrice === 100)).toMatchObject({ quantity: 0, unknown: false })
    const heat = replayHeatCells([depth], range, 500, view, layout, [])
    expect(heat.cells.some(cell => cell.quantity === null && cell.unknown)).toBe(true)
    expect(heat.cells.some(cell => cell.quantity === 0 && !cell.unknown)).toBe(true)
  })
  it('同价多单显示数量与笔数，可选第二张；未知剩余量不被合计为零', () => {
    const selected: string[] = [], orders = [order({ key: 'A', order_id: 'A', execution_id: 'E' }), order({ key: 'B', order_id: 'B', remaining_volume: null })]
    let tree!: ReactElement
    function Render() {
      tree = ReplayDepthLadder({ quote: quote(), orders, view: { min: 95, max: 105, tick: 1 }, layout, height: 420, executionId: 'E', selected: 'A', onSelect: key => selected.push(key), onZoom: () => {}, priceText: String, quantityText: value => value == null ? '未知' : String(value) })
      return tree
    }
    renderToStaticMarkup(createElement(Render))
    const nodes: ReactElement<Record<string, any>>[] = []
    const visit = (node: any) => { if (Array.isArray(node)) node.forEach(visit); else if (node && typeof node === 'object' && node.props) { nodes.push(node); visit(node.props.children); visit(node.props.content) } }
    visit(tree)
    expect(nodes.find(node => node.type === 'button' && String(node.props['aria-label']).includes('展开 2 张同价挂单'))).toBeDefined()
    expect(nodes.some(node => node.type === 'span' && node.props.children === '未知 (2)')).toBe(true)
    const button = nodes.find(node => node.type === 'button' && Array.isArray(node.props.children) && node.props.children[0]?.props.children[0] === 'B')!
    button.props.onClick()
    expect(selected).toEqual(['B'])
    expect(orders[1].remaining_volume).toBeNull()
  })
  it('密集热力以价格像素区间保留峰值与未知标记，不累加数量', () => {
    const depth = quote(2)
    depth.bids = [{ price: 100.00001, quantity: 2 }, { price: 100.00002, quantity: 9 }, { price: 100.00003, quantity: null }]
    const heat = replayHeatCells([depth], range, 500, { min: 90, max: 110, tick: .00001 }, layout, [])
    expect(heat.cells.some(cell => cell.quantity === 9 && cell.unknown && cell.minPrice === 100.00001 && cell.maxPrice === 100.00003)).toBe(true)
    expect(heat.cells.some(cell => cell.quantity === 11)).toBe(false)
  })
  it('当前档位与终态的挂单时档位分开，过期和缺口不能沿用旧盘口', () => {
    const depth = quote(), active = order(), terminal = order({ terminal: true, confirmed: false, status: 'filled', time_us: 400, capture_seq: 4 })
    const changed = { ...quote(), bids: [{ price: 98, quantity: 3 }], time_us: 300, capture_seq: 3 }
    const projection = { time_us: 500, quotes: { X: changed }, quoteHistory: [depth, changed], orderHistory: [active, terminal] } as unknown as ReplayProjection
    expect(replayOrderBookPosition(active, depth)).toBe('已录买2')
    expect(replayOrderBookPosition(order({ direction: 'SELL', price: 100 }), depth)).toBe('对手买1价位')
    expect(replayOrderPositionContext(terminal, projection, [])).toEqual({ label: '挂单时：已录买2', time: 200 })
    expect(replayOrderPositionContext(active, projection, []).label).toBe('当前：买一与卖一之间')
    expect(currentReplayQuote(depth, 1000, [])).toBeNull()
    expect(replayOrderPositionContext(terminal, projection, [{ start_us: 180, end_us: 250, topic: 'depth' }]).label).toContain('盘口未知')
    expect(replayOrderPositionContext(terminal, { ...projection, orderHistory: [terminal] }, []).label).toBe('挂单时盘口未记录')
  })
  it('委托过程用已经关联的请求展开时间视野，不修改游标或读取未来身份', () => {
    const selected = order({ terminal: true, time_us: 5000 }), bounds = { start_us: 0, end_us: 10_000 }
    const projection = { time_us: 6000, orderHistory: [order({ time_us: 4000 }), selected], ownTrades: [],
      commands: [{ command_id: 'c', time_us: 3000 }, { command_id: 'c', order_id: 'O', symbol: 'X', time_us: 4100 }, { command_id: 'future', time_us: 100 }] } as unknown as ReplayProjection
    const before = JSON.stringify(projection)
    expect(replayOrderFocus(selected, projection, bounds)).toEqual({ start_us: 2000, end_us: 6000 })
    expect(JSON.stringify(projection)).toBe(before)
    expect(replayOrderFocus(selected, { ...projection, commands: [projection.commands[0]] }, bounds).start_us).toBe(3000)
  })
  it('主被动只采用有效布尔字段，不猜测缺失与冲突', () => {
    const trade = { attributes: { is_maker: false }, conflict: false }
    expect(replayTradeLiquidity(trade)).toBe('主动成交')
    expect(replayTradeLiquidity({ ...trade, attributes: { is_maker: true } })).toBe('被动成交')
    expect(replayTradeLiquidity({ ...trade, conflict: true })).toBe('主被动未知')
    expect(replayTradeLiquidity({ ...trade, attributes: { is_maker: false, field_quality: { is_maker: 'invalid' } } })).toBe('主被动未知')
    expect(replayTradeLiquidity({ ...trade, attributes: {} })).toBe('主被动未知')
  })
})

it('主图从顶部绘制，盘口读数避开自己的表头且保留真实价位引导点', () => {
  const layout = replayChartLayout(1200, 420), view = { min: 99, max: 101, tick: 1 }
  const buckets = replayDepthBuckets(recordedDepth(quote(1)), [], view, layout)
  expect(layout.top).toBe(0)
  expect(buckets.length).toBeGreaterThan(0)
  expect(buckets.every(bucket => bucket.labelY >= 28 + 9)).toBe(true)
  for (const bucket of buckets) expect(bucket.y).toBe(replayPriceY((bucket.minPrice + bucket.maxPrice) / 2, view, layout))
})
