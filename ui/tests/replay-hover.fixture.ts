/// <reference types="bun" />
import { mock } from 'bun:test'
import assert from 'node:assert/strict'
declare const ui: string
let at = 0, slots: any[] = [], nextTimer = 0, tree: any
const timers = new Map<number, () => void>(), seeks: number[] = [], selected: string[] = []
let pauses = 0, inspection: any
mock.module('react', () => ({
  useRef: (initial: any) => { const i = at++; return slots[i] ??= { current: initial } },
  useState: (initial: any) => { const i = at++; slots[i] ??= { value: initial }; return [slots[i].value, (next: any) => { slots[i].value = typeof next === 'function' ? next(slots[i].value) : next }] },
  useMemo: (fn: any) => fn(), useCallback: (fn: any) => fn, useLayoutEffect: () => {},
}))
const jsx = (type: any, props: any) => ({ type, props })
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx, Fragment: 'fragment' }))
mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx, Fragment: 'fragment' }))
mock.module('lucide-react', () => Object.fromEntries(['ArrowLeft', 'ArrowRight', 'Check', 'Circle', 'Crosshair', 'Diamond', 'Info', 'Minus', 'Pin', 'Plus', 'RotateCcw', 'Square', 'X'].map(name => [name, () => null])))
for (const name of ['Tooltip', 'ValueSlider', 'Segmented', 'Select']) mock.module(ui + `components/ui/${name}.tsx`, () => ({ [name]: () => null }))
mock.module(ui + 'features/replay/ReplayDepthLadder.tsx', () => ({ ReplayDepthLadder: () => null }))
mock.module(ui + 'lib/viewTransition.ts', () => ({ withViewTransition: (fn: any) => fn() }))
Object.assign(globalThis, { window: { setTimeout: (fn: () => void) => { timers.set(++nextTimer, fn); return nextTimer }, clearTimeout: (id: number) => timers.delete(id) } })
const { ReplayChart } = await import(ui + 'features/replay/ReplayChart.tsx')
const { projectReplay } = await import(ui + 'features/replay/model.ts')
const { replayChartLayout, replayTimeX, replayPriceY, replayYPrice } = await import(ui + 'features/replay/chartGeometry.ts')
const { replayObservedPriceView } = await import(ui + 'features/replay/depthGeometry.ts')
const windowData: any = { recording_id: 'r', window_id: 'w', context: { schema_version: 1, instrument: { X: { price_tick: .01 } } }, coverage: [{ start_us: 0, end_us: 100 }], gaps: [], state_before_start: { time_us: 0, state_through_seq: 0 }, messages: [
  { schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 1, topic: '/quote/X', data: { symbol: 'X', book_valid: true, available: true, valid_until_us: 1000, bids: [{ price: 100, quantity: 3 }], asks: [{ price: 101, quantity: 4 }] } },
  { schema_version: 1, recording_id: 'r', capture_seq: 2, time_us: 50, topic: '/order', data: { symbol: 'X', order_id: 'O1', price: 100, quantity: 1, remaining_quantity: 0, status: 'filled', confirmed_book: false, direction: 'SELL' } },
  { schema_version: 1, recording_id: 'r', capture_seq: 3, time_us: 50, topic: '/trade', data: { symbol: 'X', order_id: 'O1', trade_key: 'T', identity_quality: 'stable', price: 100, quantity: 1 } },
  { schema_version: 1, recording_id: 'r', capture_seq: 4, time_us: 50, topic: '/market_trade/X', data: { symbol: 'X', trade_key: 'M', identity_quality: 'stable', direction: 'SELL', price: 100, quantity: 4 } },
  { schema_version: 1, recording_id: 'r', capture_seq: 5, time_us: 50, topic: '/market_trade/X', data: { symbol: 'X', trade_key: 'M3', identity_quality: 'stable', direction: 'BUY', price: 100, quantity: 4 } },
  { schema_version: 1, recording_id: 'r', capture_seq: 6, time_us: 51, topic: '/market_trade/X', data: { symbol: 'X', trade_key: 'M2', identity_quality: 'stable', direction: 'SELL', price: 100, quantity: 4 } },
], next_cursor: null, complete: true }
const projection = projectReplay(windowData, { time_us: 49 }), scene = projectReplay(windowData, { time_us: 99 }), before = JSON.stringify(projection)
const props: any = { window: windowData, range: windowData.coverage[0], projection, scene, symbol: 'X', executionId: 'E', selectedOrder: null, selectedTrade: null,
onSelectOrder: (key: string) => selected.push(key), onSelectTrade: (key: string) => selected.push(key), onInspect: () => pauses++, onSeek: (time: number, seq?: number) => { seeks.push(time); if (seq != null) seeks.push(seq) } }
function draw() { at = 0; tree = ReplayChart(props); const card = find(tree, n => n.type?.name === 'ReplayInspector'); if (card) inspection = card.props }
function find(node: any, predicate: (n: any) => boolean): any {
  if (Array.isArray(node)) { for (const child of node) { const found = find(child, predicate); if (found) return found } }
  else if (node && typeof node === 'object') return predicate(node) ? node : find(node.props?.children, predicate)
}
const panel = () => find(tree, n => n.props?.['aria-label'] === '图中记录浮卡')
draw()
const layout = replayChartLayout(900, 300), view = replayObservedPriceView(scene.quoteHistory, [], [], .01)!
function hover(price = 100, time = 49) {
  find(tree, n => n.type === 'canvas').props.onPointerMove({ clientX: replayTimeX(time, props.range, layout), clientY: replayPriceY(price, view, layout), currentTarget: { style: {}, getBoundingClientRect: () => ({ left: 0, top: 0 }) } }); draw()
}
hover()
const noFloatingCard = !panel()
const readout = () => find(tree, n => n.props?.['aria-label'] === '悬停读数')
const axisLabels = Boolean(find(tree, n => n.props?.className === 'replay-crosshair-price num')) && Boolean(find(tree, n => n.props?.className === 'replay-crosshair-time num'))
const hasReadout = Boolean(readout())
const hoverDoesNotPause = pauses === 0
const priceLabel = () => find(tree, n => n.props?.className === 'replay-crosshair-price num')
const tickAtHover = find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.children === '100')
assert.equal(tickAtHover.props.style.opacity, 0, '悬停价格附近的刻度应完全让位')
assert.equal(tickAtHover.props.style.left + tickAtHover.props.style.width, priceLabel().props.style.left + priceLabel().props.style.width, '悬停价格与普通刻度右对齐')
assert.ok(find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.style.opacity === 1), '远处刻度仍然完整可见')
const middleTick = () => find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.children === '100.4')
const middleTop = middleTick().props.style.top
function yieldAt(distance: number) {
  const y = middleTop + 8 + distance
  hover(replayYPrice(y, view, layout), 0)
  assert.equal(middleTick().props.style.top, middleTop, '让位不移动普通刻度')
  assert.ok(Math.abs(priceLabel().props.style.top + priceLabel().props.children[0].props.style.top - y) < 1e-8, '读数与短刻线即时跟随，不添加追赶延迟')
  return middleTick().props.style.opacity
}
for (const [distance, opacity] of [[40, 1], [36, 1], [32, .84375], [28, .5], [24, .15625], [20, 0], [0, 0], [-28, .5], [28, .5]]) {
  assert.ok(Math.abs(yieldAt(distance) - opacity) < 1e-8, '刻度按距离平滑退墨，反向移动不积累动画')
}
for (const boundary of [20, 36]) {
  assert.ok(Math.abs(yieldAt(boundary - .01) - yieldAt(boundary + .01)) < .001, '反复跨过让位边界不硬切闪烁')
}
hover(100.123456)
assert.equal(priceLabel().props.children[1], '100.12', '保留品种价格精度')
for (const price of [view.min, view.max]) {
  hover(price, 0)
  const label = priceLabel()
  assert.ok(label.props.style.top >= layout.top && label.props.style.top + 16 <= layout.bottom, '边缘价格读数不越界')
  assert.ok(Math.abs(label.props.style.top + label.props.children[0].props.style.top - replayPriceY(price, view, layout)) < 1e-8, '短刻线保留真实价格位置')
}
hover(view.min)
assert.equal(priceLabel().props.children[1], '100', '吸附到附近成交时保留对象价格')
assert.equal(priceLabel().props.style.top + priceLabel().props.children[0].props.style.top, find(tree, n => n.props?.className === 'replay-crosshair-horizontal').props.style.top, '吸附后短刻线与十字线对齐')
find(tree, n => n.type === 'canvas').props.onPointerLeave(); draw()
assert.ok(!find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.style.opacity !== 1), '移出后恢复普通刻度')
const clearedOnLeave = !readout() && !find(tree, n => n.props?.className === 'replay-crosshair-price num')
const target = { style: {}, focus() {}, setPointerCapture() {}, hasPointerCapture: () => false, getBoundingClientRect: () => ({ left: 0, top: 0 }) }
const clickEvent = { button: 0, pointerId: 1, clientX: replayTimeX(50, props.range, layout), clientY: replayPriceY(100, view, layout), currentTarget: target }
let chartCanvas = find(tree, n => n.type === 'canvas')
chartCanvas.props.onPointerDown(clickEvent); chartCanvas.props.onPointerUp(clickEvent); draw()
const clickSelectsWithoutSeek = Boolean(panel()) && pauses > 0 && seeks.length === 0 && JSON.stringify(projection) === before
const overlappingRecords = inspection.records.some((item: any) => item.kind === 'market') && inspection.records.some((item: any) => item.order) && inspection.records.some((item: any) => item.trade)
const futureLabeled = inspection.record.future
const anchoredBesideClick = panel().props.style.top >= 8 && panel().props.style.top + panel().props.style.maxHeight <= 292 && panel().props.style.left >= 8
const fixedPosition = JSON.stringify(panel().props.style)
hover()
const fixedDuringHover = JSON.stringify(panel().props.style) === fixedPosition
find(tree, n => n.type === 'canvas').props.onPointerLeave(); draw()
const staysOnLeave = Boolean(panel())
find(tree, n => n.type === 'canvas').props.onKeyDown({ key: 'Escape' }); draw()
const escapeCloses = !panel()
chartCanvas = find(tree, n => n.type === 'canvas')
const edgeClick = { ...clickEvent, clientX: replayTimeX(98, props.range, layout), clientY: replayPriceY(101, view, layout) }
chartCanvas.props.onPointerDown(edgeClick); chartCanvas.props.onPointerUp(edgeClick); draw()
const depthClickCloses = !panel()
chartCanvas = find(tree, n => n.type === 'canvas')
chartCanvas.props.onPointerDown(clickEvent); chartCanvas.props.onPointerUp(clickEvent); draw()

hover()
chartCanvas = find(tree, n => n.type === 'canvas')
chartCanvas.props.onPointerDown(clickEvent)
chartCanvas.props.onPointerMove({ ...clickEvent, clientX: clickEvent.clientX + 30 }); draw()
const clearedOnDrag = !readout() && !find(tree, n => n.props?.className === 'replay-crosshair-price num')
chartCanvas.props.onPointerUp({ ...clickEvent, clientX: clickEvent.clientX + 30 }); draw()
find(tree, n => n.type === 'canvas').props.onKeyDown({ key: 'Escape' }); draw()
const { ReplayInspector } = await import(ui + 'features/replay/ReplayInspector.tsx')
let chosen = inspection.record, located: any
const details = () => ReplayInspector({ record: chosen, records: inspection.records, onChoose: (record: any) => { chosen = record }, onClose() {}, onView() {}, onSeek: () => { located = [chosen.time, chosen.seq] } })
let detailTree = details()
const picker = find(detailTree, n => n.props?.ariaLabel === '选择命中对象')
const marketOption = picker.props.options.find((option: any) => option.label.startsWith('市场成交'))
picker.props.onChange(marketOption.value)
const marketIdentity = chosen.kind === 'market' && !chosen.order
chosen = inspection.records.find((record: any) => record.trade)
detailTree = details()
find(detailTree, n => n.type === 'button' && n.props?.children === '定位此时刻').props.onClick()
const explicitSeek = located[0] === 50 && located[1] === 3
const noPin = !JSON.stringify(detailTree).includes('固定详情')
find(tree, n => n.type === 'canvas').props.onKeyDown({ key: 'Home', preventDefault() {} }); draw()
chartCanvas = find(tree, n => n.type === 'canvas')
chartCanvas.props.onPointerDown(clickEvent); chartCanvas.props.onPointerUp(clickEvent); draw()
const emptyClick = { ...clickEvent, clientX: replayTimeX(10, props.range, layout), clientY: replayPriceY(100.5, view, layout) }
const seeksBeforeEmptyClick = seeks.length
chartCanvas = find(tree, n => n.type === 'canvas')
chartCanvas.props.onPointerDown(emptyClick); chartCanvas.props.onPointerUp(emptyClick); draw()
const emptyClickCloses = !panel() && seeks.length === seeksBeforeEmptyClick
props.range = { start_us: 0, end_us: 1_000_000 }; draw()
const button = (label: string) => find(tree, n => n.type === 'button' && n.props?.['aria-label'] === label)
find(tree, n => n.type === 'canvas').props.onKeyDown({ key: '+', preventDefault() {} }); draw()
find(tree, n => n.type === 'canvas').props.onKeyDown({ key: '+', preventDefault() {} }); draw()
const seeksBeforeReturn = seeks.length
const returnButton = button('回到游标')
if (!returnButton) throw Error('缩放后应能返回视野外的游标')
assert.ok(!find(tree, n => n.props?.className === 'replay-cursor-line'), '视野外不把游标伪装为边缘竖线')
assert.equal(find(tree, n => n.props?.className === 'replay-chart-axis').props['data-outside'], true, '视野外隐藏三角播放头')
assert.ok(find(tree, n => n.props?.className === 'replay-cursor-time num').props['aria-label'].includes('视野之外'), '当前读数说明视野外状态')
returnButton.props.onClick(); draw()
const returnsToCursor = !button('回到游标') && seeks.length === seeksBeforeReturn
button('恢复视图').props.onClick(); draw()
const resetsView = button('恢复视图').props.disabled

find(tree, n => n.type === 'canvas').props.onPointerLeave()
const working = { ...Object.values(scene.orders)[0] as any, confirmed: true, terminal: false, remaining_volume: 1, status: 'working' }
const activeOrders = Object.fromEntries([
  { ...working, key: 'A', price: 100.5, cancel_pending: true },
  { ...working, key: 'B', price: 100.51 },
  { ...working, key: 'C', price: 100.5 },
  { ...working, key: 'D', price: 100.4 },
].map(order => [order.key, order]))
props.projection = { ...projection, orders: activeOrders }
props.scene = { ...scene, orders: activeOrders }
props.range = windowData.coverage[0]
props.selectedOrder = 'A'
draw()
const orderLabel = (price: string) => find(tree, n => n.props?.className === 'replay-order-price num' && n.props?.['aria-label'] === `账户挂单价格 ${price}`)
const selectedLabel = orderLabel('100.5'), nearbyLabel = orderLabel('100.51')
assert.equal(selectedLabel.props['data-collapsed'], undefined, '选中委托优先保留价格文本')
assert.equal(nearbyLabel.props['data-collapsed'], true, '密集委托保留刻线，不叠加价格文本')
assert.equal(nearbyLabel.props.children[1].props.style.opacity, 0)
assert.equal(selectedLabel.props['data-pending'], true, '同价任一委托待撤即保留警示语义')
assert.ok(!middleTick(), '委托价与普通刻度同价时只绘制委托标签')
const otherTick = find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.children === '100.6')
assert.equal(otherTick.props.style.left, selectedLabel.props.style.left, '委托与轴刻度共用左边界')
assert.equal(otherTick.props.style.width, selectedLabel.props.style.width, '委托与轴刻度共用宽度和右边界')
assert.ok(otherTick.props.style.opacity < 1, '相邻普通刻度向委托价让位')
for (const label of [selectedLabel, nearbyLabel]) {
  assert.ok(Math.abs(label.props.style.top + label.props.children[0].props.style.top - replayPriceY(Number(label.props['aria-label'].split(' ')[1]), view, layout)) < 1e-8, '密集委托的刻线始终位于真实价格')
}
hover(100.5, 0)
const mergedLabel = find(tree, n => n.props?.className === 'replay-crosshair-price num replay-order-price')
assert.equal(mergedLabel.props.children[1], '100.5', '悬停与委托同价时合并读数')
assert.equal(mergedLabel.props['data-pending'], true)
assert.equal(orderLabel('100.5').props.children[1].props.style.opacity, 0, '合并后不重复显示委托价格')
hover(100.55, 0)
assert.equal(orderLabel('100.5').props.children[1].props.style.opacity, 0, '异价悬停靠近时委托文本让位，保留价位刻线')
assert.ok(priceLabel(), '异价悬停保持独立读数')
find(tree, n => n.type === 'canvas').props.onPointerLeave(); draw()
assert.equal(orderLabel('100.5').props.children[1].props.style.opacity, 1, '移出后恢复委托价格')
props.selectedOrder = 'B'; draw()
assert.equal(orderLabel('100.51').props['data-collapsed'], undefined, '切换选中委托后优先级随之切换')
assert.equal(orderLabel('100.5').props['data-collapsed'], true)
console.log(JSON.stringify({ noFloatingCard, axisLabels, hasReadout, hoverDoesNotPause, clearedOnLeave, clickSelectsWithoutSeek, overlappingRecords, futureLabeled, clearedOnDrag, marketIdentity, explicitSeek, noPin, anchoredBesideClick, fixedDuringHover, staysOnLeave, escapeCloses, depthClickCloses, emptyClickCloses, returnsToCursor, resetsView }))
