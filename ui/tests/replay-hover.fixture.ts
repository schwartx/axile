/// <reference types="bun" />
import { mock } from 'bun:test'
import assert from 'node:assert/strict'
declare const ui: string
let at = 0, slots: any[] = [], nextTimer = 0, tree: any
const timers = new Map<number, () => void>(), frames = new Map<number, FrameRequestCallback>(), seeks: number[] = [], selected: string[] = []
const cleanups = new Map<number, () => void>()
const equalDeps = (left: any[], right: any[]) => left?.length === right.length && left.every((value, index) => Object.is(value, right[index]))
let pauses = 0, inspection: any, basePaints = 0, hoverPaints = 0
mock.module('react', () => ({
  useRef: (initial: any) => { const i = at++; return slots[i] ??= { current: initial } },
  useState: (initial: any) => { const i = at++; slots[i] ??= { value: initial }; return [slots[i].value, (next: any) => { slots[i].value = typeof next === 'function' ? next(slots[i].value) : next }] },
  useMemo: (fn: any, deps: any[]) => { const i = at++; if (!slots[i] || !equalDeps(slots[i].deps, deps)) slots[i] = { value: fn(), deps }; return slots[i].value },
  useCallback: (fn: any, deps: any[]) => { const i = at++; if (!slots[i] || !equalDeps(slots[i].deps, deps)) slots[i] = { value: fn, deps }; return slots[i].value },
  useLayoutEffect: (fn: any, deps: any[]) => { const i = at++; if (!slots[i] || !equalDeps(slots[i].deps, deps)) { cleanups.get(i)?.(); slots[i] = { deps }; if (fn.toString().includes('prepareCanvas(canvas.current')) basePaints++; if (fn.toString().includes('prepareCanvas(hoverCanvas.current')) hoverPaints++; const cleanup = fn(); if (cleanup) cleanups.set(i, cleanup); else cleanups.delete(i) } },
}))
const jsx = (type: any, props: any) => ({ type, props })
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx, Fragment: 'fragment' }))
mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx, Fragment: 'fragment' }))
mock.module('lucide-react', () => Object.fromEntries(['ArrowLeft', 'ArrowRight', 'Check', 'Circle', 'Crosshair', 'Diamond', 'Info', 'Minus', 'Pin', 'Plus', 'RotateCcw', 'Square', 'X'].map(name => [name, () => null])))
for (const name of ['Tooltip', 'ValueSlider', 'Segmented', 'Select']) mock.module(ui + `components/ui/${name}.tsx`, () => ({ [name]: () => null }))
mock.module(ui + 'features/replay/ReplayDepthLadder.tsx', () => ({ ReplayDepthLadder: () => null }))
mock.module(ui + 'lib/viewTransition.ts', () => ({ withViewTransition: (fn: any) => fn() }))
Object.assign(globalThis, { window: { setTimeout: (fn: () => void) => { timers.set(++nextTimer, fn); return nextTimer }, clearTimeout: (id: number) => timers.delete(id), requestAnimationFrame: (fn: FrameRequestCallback) => { frames.set(++nextTimer, fn); return nextTimer }, cancelAnimationFrame: (id: number) => frames.delete(id) } })
let inspectedOrders = 0
const inspectionModule = await import(ui + 'features/replay/inspection.ts')
const inspectOrder = inspectionModule.inspectReplayOrder
mock.module(ui + 'features/replay/inspection.ts', () => ({ ...inspectionModule, inspectReplayOrder: (...args: Parameters<typeof inspectOrder>) => { inspectedOrders++; return inspectOrder(...args) } }))
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
const layout = replayChartLayout(900, 300, 40), view = replayObservedPriceView(scene.quoteHistory, [], [], .01)!
function hover(price = 100, time = 49) {
  find(tree, n => n.type === 'canvas').props.onPointerMove({ clientX: replayTimeX(time, props.range, layout), clientY: replayPriceY(price, view, layout), currentTarget: { style: {}, getBoundingClientRect: () => ({ left: 0, top: 0 }) } }); draw()
}
const paintsBeforeHover = basePaints, overlaysBeforeHover = hoverPaints
hover()
assert.equal(basePaints, paintsBeforeHover, '悬停不重新绘制底图')
assert.ok(hoverPaints > overlaysBeforeHover, '悬停只触发独立高亮画布')
const noFloatingCard = !panel()
const readout = () => find(tree, n => n.props?.['aria-label'] === '悬停读数')
const axisLabels = Boolean(find(tree, n => n.props?.className === 'replay-crosshair-price num')) && Boolean(find(tree, n => n.props?.className === 'replay-hover-time'))
const hasReadout = Boolean(readout())
const hoverDoesNotPause = pauses === 0
const cursorTime = () => find(tree, n => n.props?.className === 'replay-cursor-time num')
const hoverTime = () => find(find(tree, n => n.props?.className === 'replay-chart-meta'), n => n.type === 'time')
const timeRuler = () => find(tree, n => n.props?.className === 'replay-time-ruler')
for (const time of [0, 49, 99]) {
  hover(100.5, time)
  const current = cursorTime().props.style
  assert.ok(hoverTime(), '悬停时间始终位于图表上方的固定读数位')
  assert.ok(!find(find(tree, n => n.props?.className === 'replay-chart-plot'), n => n.type === 'time' && n.props?.['aria-label']?.startsWith('悬停时间')), '图内不再绘制悬停时间')
  assert.match(hoverTime().props.children, /^\d{2}:\d{2}:\d{2}\.\d{6}$/, '悬停仍保留微秒精度')
  for (const tick of timeRuler().props.children) {
    const label = tick.props.children[1].props.style
    const left = layout.left + tick.props.style.left + label.left
    const intersects = (other: any) => left < other.left + other.width && other.left < left + label.width
    assert.equal(label.opacity, intersects(current) ? 0 : 1, '普通刻度仅避让当前播放时间，不受悬停位置影响')
  }
}
hover()
const priceLabel = () => find(tree, n => n.props?.className?.startsWith('replay-crosshair-price num'))
const tickAtHover = find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.children === '100.00')
assert.equal(tickAtHover.props.style.opacity, 0, '悬停与刻度同价时，仅隐藏重叠文字')
assert.equal(tickAtHover.props.style.width, 36, '价格轴只保留一个紧凑槽位')
assert.equal(priceLabel().props.style.left, tickAtHover.props.style.left, '游标读数与固定刻度共用同一价格轴')
assert.equal(priceLabel().props.style.width, tickAtHover.props.style.width)
const middleTick = () => find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.children === '100.40')
const middleTop = middleTick().props.style.top
const fixedAxis = JSON.stringify(middleTick().props.style)
for (const distance of [40, 36, 32, 28, 24, 20, 0, -28, 28, 0]) {
  const y = middleTop + 8 + distance
  hover(replayYPrice(y, view, layout), 0)
  assert.equal(middleTick().props.style.top, middleTop, '靠近、跨越和反向移动不移动固定刻度')
  assert.equal(middleTick().props.style.opacity, Math.abs(middleTop - priceLabel().props.style.top) < 16 ? 0 : 1, '只隐藏实际相交的文字，没有邻近渐隐')
  assert.equal(priceLabel().props.style.left, layout.right + 2, '单轴宽度与主图边界不随悬停变化')
  assert.ok(Math.abs(priceLabel().props.style.top + priceLabel().props.children[0].props.style.top - y) < 1e-8, '读数短横即时跟随真实价格，不添加追赶延迟')
}
hover(100.123456)
assert.equal(priceLabel().props.children[1].props.children, '100.12', '保留品种价格精度')
for (const price of [view.min, view.max]) {
  hover(price, 0)
  const label = priceLabel()
  assert.ok(label.props.style.top >= layout.top && label.props.style.top + 16 <= layout.bottom, '边缘价格读数不越界')
  assert.ok(Math.abs(label.props.style.top + label.props.children[0].props.style.top - replayPriceY(price, view, layout)) < 1e-8, '短刻线保留真实价格位置')
}
hover(view.min)
assert.equal(priceLabel().props.children[1].props.children, '100.00', '吸附到附近成交时保留对象价格')
assert.equal(priceLabel().props.style.top + priceLabel().props.children[0].props.style.top, find(tree, n => n.props?.className === 'replay-crosshair-horizontal').props.style.top, '吸附后短刻线与十字线对齐')
find(tree, n => n.type === 'canvas').props.onPointerLeave(); draw()
assert.equal(JSON.stringify(middleTick().props.style), fixedAxis, '移出后固定刻度与轴宽保持不变')
assert.ok(!find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.style.opacity !== 1), '移出后普通刻度持续可见')
assert.equal(hoverTime().props.children, '—', '移出后读数位保留占位，不引起布局跳动')
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
const otherTick = find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.children === '100.60')
assert.equal(otherTick.props.style.left, selectedLabel.props.style.left, '委托与轴刻度共用左边界')
assert.equal(otherTick.props.style.width, selectedLabel.props.style.width, '委托与轴刻度共用宽度和右边界')
assert.equal(otherTick.props.style.opacity, 1, '邻近但不重叠的普通刻度保持完整可见')
for (const label of [selectedLabel, nearbyLabel]) {
  assert.ok(Math.abs(label.props.style.top + label.props.children[0].props.style.top - replayPriceY(Number(label.props['aria-label'].split(' ')[1]), view, layout)) < 1e-8, '密集委托的刻线始终位于真实价格')
}
hover(100.5, 0)
assert.equal(priceLabel().props.children[1].props.children, '100.50', '悬停与委托同价时仍保留独立读数')
assert.equal(orderLabel('100.5').props['data-pending'], true)
assert.equal(priceLabel().props['data-pending'], true, '同价读数保留待撤委托警示')
assert.equal(orderLabel('100.5').props.children[1].props.style.opacity, 0, '同价委托文字由悬停读数接替，不重复绘制')
assert.equal(priceLabel().props.style.left, orderLabel('100.5').props.style.left, '游标和委托共用一个槽位')
const orderAxis = JSON.stringify(otherTick.props.style)
hover(100.9, 0)
assert.equal(orderLabel('100.5').props.children[1].props.style.opacity, 1, '不重叠的悬停不影响固定委托文本')
assert.equal(JSON.stringify(find(tree, n => n.props?.className === 'replay-price-tick num' && n.props.children === '100.60').props.style), orderAxis, '不重叠的刻度保持不变')
assert.ok(priceLabel(), '异价悬停保持独立读数')
find(tree, n => n.type === 'canvas').props.onPointerLeave(); draw()
assert.equal(orderLabel('100.5').props.children[1].props.style.opacity, 1, '移出后恢复委托价格')
props.selectedOrder = 'B'; draw()
assert.equal(orderLabel('100.51').props['data-collapsed'], undefined, '切换选中委托后优先级随之切换')
assert.equal(orderLabel('100.5').props['data-collapsed'], true)
props.scene = { ...scene, orderHistory: [...scene.orderHistory, ...Array.from({ length: 2000 }, (_, index) => ({ ...working, key: `distant-${index}`, order_id: `distant-${index}`, terminal: true, confirmed: false, status: 'filled', time_us: 90, capture_seq: 100 + index }))] }
props.selectedOrder = null; draw()
inspectedOrders = 0
hover(100.5, 10)
assert.equal(inspectedOrders, 0, '未命中时不格式化任何委托 mark')
hover(100, 50)
assert.ok(inspectedOrders > 0 && inspectedOrders < 10, '只格式化命中的委托，不扫描格式化远处的数千个 marks')
const seekStart = { ...clickEvent, clientX: replayTimeX(props.projection.time_us, props.range, layout) }
const seekCount = seeks.length
chartCanvas = find(tree, n => n.type === 'canvas')
chartCanvas.props.onPointerDown(seekStart)
for (const time of [60, 70, 80]) chartCanvas.props.onPointerMove({ ...seekStart, clientX: replayTimeX(time, props.range, layout) })
assert.equal(seeks.length, seekCount, 'seek 拖动同帧不立即投影')
assert.equal(frames.size, 1, 'seek 拖动只预约一帧')
chartCanvas.props.onPointerUp({ ...seekStart, clientX: replayTimeX(85, props.range, layout) })
assert.equal(seeks.at(-1), 85, 'pointerup 提交最终位置，即使最后位置尚无 move')
assert.equal(seeks.length, seekCount + 1)
assert.equal(frames.size, 0)
for (const end of ['onPointerCancel', 'onLostPointerCapture']) {
  chartCanvas.props.onPointerDown(seekStart)
  chartCanvas.props.onPointerMove({ ...seekStart, clientX: replayTimeX(80, props.range, layout) })
  chartCanvas.props[end]()
  assert.equal(seeks.at(-1), 80, `${end} 提交最后拖动位置`)
  assert.equal(frames.size, 0)
}
const axis = () => find(tree, n => n.props?.className === 'replay-chart-axis')
const slider = () => find(tree, n => n.props?.label === '录制定位')
axis().props.onPointerDownCapture({ isPrimary: true, button: 0 })
const beforeSlider = seeks.length
slider().props.onChange(60); slider().props.onChange(70); slider().props.onChange(80)
assert.equal(seeks.length, beforeSlider, 'slider pointer 同帧合并')
slider().props.onChangeEnd(80)
assert.equal(seeks.at(-1), 80)
assert.equal(seeks.length, beforeSlider + 1)
assert.equal(frames.size, 0)
slider().props.onChange(81)
assert.equal(seeks.at(-1), 81, '非拖动定位保持即时')
for (const change of [() => { props.window = { ...props.window, window_id: 'next' } }, () => { props.symbol = 'Y' }]) {
  axis().props.onPointerDownCapture({ isPrimary: true, button: 0 }); slider().props.onChange(90)
  const count = seeks.length
  chartCanvas = find(tree, n => n.type === 'canvas')
  chartCanvas.props.onPointerDown(seekStart)
  change(); draw()
  find(tree, n => n.type === 'canvas').props.onPointerMove({ ...seekStart, clientX: replayTimeX(95, props.range, layout) })
  assert.equal(frames.size, 0, '窗口或品种切换取消待执行 seek，旧手势不能重新预约')
  assert.equal(seeks.length, count)
}
axis().props.onPointerDownCapture({ isPrimary: true, button: 0 }); slider().props.onChange(90)
const beforeUnmount = seeks.length
for (const cleanup of cleanups.values()) cleanup()
assert.equal(frames.size, 0, '卸载取消待执行 seek')
assert.equal(seeks.length, beforeUnmount)
console.log(JSON.stringify({ noFloatingCard, axisLabels, hasReadout, hoverDoesNotPause, clearedOnLeave, clickSelectsWithoutSeek, overlappingRecords, futureLabeled, clearedOnDrag, marketIdentity, explicitSeek, noPin, anchoredBesideClick, fixedDuringHover, staysOnLeave, escapeCloses, depthClickCloses, emptyClickCloses, returnsToCursor, resetsView }))
