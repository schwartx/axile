import { mock } from 'bun:test'
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
mock.module('lucide-react', () => Object.fromEntries(['Circle', 'Crosshair', 'Diamond', 'Info', 'Minus', 'Pin', 'Plus', 'RotateCcw', 'Square', 'X'].map(name => [name, () => null])))
for (const name of ['Tooltip', 'ValueSlider', 'Segmented', 'Select']) mock.module(ui + `components/ui/${name}.tsx`, () => ({ [name]: () => null }))
mock.module(ui + 'features/replay/ReplayDepthLadder.tsx', () => ({ ReplayDepthLadder: () => null }))
mock.module(ui + 'lib/viewTransition.ts', () => ({ withViewTransition: (fn: any) => fn() }))
Object.assign(globalThis, { window: { setTimeout: (fn: () => void) => { timers.set(++nextTimer, fn); return nextTimer }, clearTimeout: (id: number) => timers.delete(id) } })
const { ReplayChart } = await import(ui + 'features/replay/ReplayChart.tsx')
const { projectReplay } = await import(ui + 'features/replay/model.ts')
const { replayChartLayout, replayTimeX, replayPriceY } = await import(ui + 'features/replay/chartGeometry.ts')
const { replayObservedPriceView } = await import(ui + 'features/replay/depthGeometry.ts')
const windowData: any = { recording_id: 'r', window_id: 'w', context: { schema_version: 1, instrument: { X: { price_tick: .01 } } }, coverage: [{ start_us: 0, end_us: 100 }], gaps: [], state_before_start: { time_us: 0, state_through_seq: 0 }, messages: [
  { schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 1, topic: '/quote/X', data: { symbol: 'X', book_valid: true, valid_until_us: 1000, bids: [{ price: 100, quantity: 3 }], asks: [{ price: 101, quantity: 4 }] } },
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
function hover() {
  find(tree, n => n.type === 'canvas').props.onPointerMove({ clientX: replayTimeX(49, props.range, layout), clientY: replayPriceY(100, view, layout), currentTarget: { getBoundingClientRect: () => ({ left: 0, top: 0 }) } }); draw()
}
hover()
const noFloatingCard = !panel()
const readout = () => find(tree, n => n.props?.['aria-label'] === '悬停读数')
const axisLabels = Boolean(find(tree, n => n.props?.className === 'replay-crosshair-price num')) && Boolean(find(tree, n => n.props?.className === 'replay-crosshair-time num'))
const hasReadout = Boolean(readout())
const hoverDoesNotPause = pauses === 0
find(tree, n => n.type === 'canvas').props.onPointerLeave(); draw()
const clearedOnLeave = !readout() && !find(tree, n => n.props?.className === 'replay-crosshair-price num')
const target = { focus() {}, setPointerCapture() {}, hasPointerCapture: () => false, getBoundingClientRect: () => ({ left: 0, top: 0 }) }
const clickEvent = { button: 0, pointerId: 1, clientX: replayTimeX(50, props.range, layout), clientY: replayPriceY(100, view, layout), currentTarget: target }
let chartCanvas = find(tree, n => n.type === 'canvas')
chartCanvas.props.onPointerDown(clickEvent); chartCanvas.props.onPointerUp(clickEvent); draw()
const clickSelectsWithoutSeek = Boolean(panel()) && pauses > 0 && seeks.length === 0 && JSON.stringify(projection) === before
const overlappingRecords = inspection.records.some((item: any) => item.kind === 'market') && inspection.records.some((item: any) => item.order) && inspection.records.some((item: any) => item.trade)
const futureLabeled = inspection.record.future
const anchoredBesideClick = panel().props.style.bottom === 300 - clickEvent.clientY + 12 && panel().props.style.left >= 8
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
const edgeFlips = Boolean(panel()) && panel().props.style.left < edgeClick.clientX && panel().props.style.top === edgeClick.clientY + 12
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
console.log(JSON.stringify({ noFloatingCard, axisLabels, hasReadout, hoverDoesNotPause, clearedOnLeave, clickSelectsWithoutSeek, overlappingRecords, futureLabeled, clearedOnDrag, marketIdentity, explicitSeek, noPin, anchoredBesideClick, fixedDuringHover, staysOnLeave, escapeCloses, edgeFlips, emptyClickCloses }))
