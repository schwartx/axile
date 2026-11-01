import { mock } from 'bun:test'
declare const ui: string
let at = 0, slots: any[] = [], nextTimer = 0, tree: any
const timers = new Map<number, () => void>(), seeks: number[] = [], selected: string[] = []
let pauses = 0
mock.module('react', () => ({
  useRef: (initial: any) => { const i = at++; return slots[i] ??= { current: initial } },
  useState: (initial: any) => { const i = at++; slots[i] ??= { value: initial }; return [slots[i].value, (next: any) => { slots[i].value = typeof next === 'function' ? next(slots[i].value) : next }] },
  useMemo: (fn: any) => fn(), useCallback: (fn: any) => fn, useLayoutEffect: () => {},
}))
const jsx = (type: any, props: any) => type?.name === 'ReplayInspector' ? type(props) : ({ type, props })
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
function draw() { at = 0; tree = ReplayChart(props) }
function find(node: any, predicate: (n: any) => boolean): any {
  if (Array.isArray(node)) { for (const child of node) { const found = find(child, predicate); if (found) return found } }
  else if (node && typeof node === 'object') return predicate(node) ? node : find(node.props?.children, predicate)
}
const panel = () => find(tree, n => n.props?.['aria-label'] === '图中详情')
const button = (name: string) => find(tree, n => n.type === 'button' && (n.props?.['aria-label'] === name || [n.props?.children].flat().filter(x => typeof x === 'string').join('') === name))
const runTimers = () => { const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn()); draw() }
draw()
const layout = replayChartLayout(900, 300), view = replayObservedPriceView(scene.quoteHistory, [], [], .01)!
function hover() {
  find(tree, n => n.type === 'canvas').props.onPointerMove({ clientX: replayTimeX(49, props.range, layout), clientY: replayPriceY(100, view, layout), currentTarget: { getBoundingClientRect: () => ({ left: 0, top: 0 }) } }); draw()
}
hover()
find(tree, n => n.type === 'canvas').props.onPointerLeave(); panel().props.onPointerEnter(); runTimers()
const entered = Boolean(panel())
const picker = () => find(panel(), n => n.props?.ariaLabel === '选择命中对象')
const options = picker().props.options
const overlappingMarketAccessible = options.filter((option: any) => option.label.startsWith('市场成交')).length === 3
const futureLabeled = Boolean(find(panel(), n => n.type === 'span' && n.props.children === '后续已录 · '))
const previewCompact = Boolean(find(panel(), n => n.props?.inert === true))
button('固定详情').props.onClick(); draw(); panel().props.onPointerLeave(); runTimers()
const pinned = button('取消固定详情').props['aria-pressed']
const markets = options.filter((option: any) => option.label.startsWith('市场成交'))
picker().props.onChange(markets[2].value); draw()
const oneSameDirectionSelected = picker().props.value === markets[2].value
picker().props.onChange(markets[1].value); draw()
const bubbleDirectionsDistinct = oneSameDirectionSelected && picker().props.value === markets[1].value && markets.some((option: any) => option.label.includes('主动买')) && markets.some((option: any) => option.label.includes('主动卖'))
const marketClearsOrderIdentity = !String(picker().props.selectedValue.props.children).includes('委托') && !button('放大过程') && !button('查看委托')
picker().props.onChange(options.find((option: any) => option.label.startsWith('账户委托')).value); draw()
find(panel(), n => n.type === 'button' && Array.isArray(n.props.children) && n.props.children[0]?.props?.children?.startsWith?.('逐笔成交')).props.onClick(); draw()
button('查看成交').props.onClick(); draw()
const inspection = { seeks: [...seeks], selected: [...selected], pauses, unchanged: JSON.stringify(projection) === before }
button('取消固定详情').props.onClick(); draw(); panel().props.onPointerLeave(); runTimers()
const closedAfterUnpin = !panel()
hover(); button('固定详情').props.onClick(); draw(); find(panel(), n => n.type === 'button' && Array.isArray(n.props.children) && n.props.children[0]?.props?.children?.startsWith?.('逐笔成交')).props.onClick(); draw(); button('定位此时刻').props.onClick(); draw()
const located = { seeks: [...seeks], closed: !panel() }
const beforeClickSeeks = seeks.length
const target = { focus() {}, setPointerCapture() {}, hasPointerCapture: () => false, getBoundingClientRect: () => ({ left: 0, top: 0 }) }
let chartCanvas = find(tree, n => n.type === 'canvas')
const clickEvent = { button: 0, pointerId: 1, clientX: replayTimeX(50, props.range, layout), clientY: replayPriceY(100, view, layout), currentTarget: target }
chartCanvas.props.onPointerDown(clickEvent); chartCanvas.props.onPointerUp(clickEvent); draw()
const clickPinsWithoutSeek = seeks.length === beforeClickSeeks && Boolean(button('取消固定详情')?.props['aria-pressed'])

hover(); panel().props.onKeyDown({ key: 'Escape', stopPropagation() {} }); draw()
console.log(JSON.stringify({ entered, previewCompact, overlappingMarketAccessible, bubbleDirectionsDistinct, marketClearsOrderIdentity, futureLabeled, pinned, inspection, closedAfterUnpin, located, clickPinsWithoutSeek, escapeClosed: !panel() }))
