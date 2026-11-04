/// <reference types="bun" />
import { mock } from 'bun:test'
import assert from 'node:assert/strict'
declare const ui: string
const viewFrames = new Map<number, FrameRequestCallback>(); let viewFrameId = 0
let at = 0, dirty = false, tree: any
const slots: any[] = [], effects: (() => void)[] = [], timers = new Map<number, () => void>()
let timerId = 0
mock.module('react', () => ({
  useRef: (initial: any) => { const i = at++; return slots[i] ??= { current: initial } },
  useState: (initial: any) => {
    const i = at++; slots[i] ??= { value: initial }
    return [slots[i].value, (next: any) => { const value = typeof next === 'function' ? next(slots[i].value) : next; if (!Object.is(value, slots[i].value)) { slots[i].value = value; dirty = true } }]
  },
  useMemo: (fn: any, deps: any[]) => {
    const i = at++, previous = slots[i]
    if (!previous || deps.some((value, index) => !Object.is(value, previous.deps[index]))) slots[i] = { deps, value: fn() }
    return slots[i].value
  }, useCallback: (fn: any) => fn,
  useLayoutEffect: (fn: any, deps: any[]) => {
    const i = at++, previous = slots[i]
    if (!previous || deps.some((value, index) => !Object.is(value, previous.deps[index]))) {
      slots[i] = { deps }; effects.push(() => { previous?.cleanup?.(); slots[i].cleanup = fn() })
    }
  },
}))
const listeners = new Map<string, (event: any) => void>()
const target = { clientWidth: 900, clientHeight: 300, style: {}, focus() {}, setPointerCapture() {}, hasPointerCapture: () => false,
  getBoundingClientRect: () => ({ left: 0, top: 0 }), addEventListener: (name: string, fn: any) => listeners.set(name, fn), removeEventListener: (name: string) => listeners.delete(name) }
const jsx = (type: any, props: any) => { if (props.ref) props.ref.current = target; return { type, props } }
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx, Fragment: 'fragment' }))
mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx, Fragment: 'fragment' }))
mock.module('lucide-react', () => Object.fromEntries(['ArrowLeft', 'ArrowRight', 'Check', 'Circle', 'Crosshair', 'Info', 'Minus', 'RotateCcw', 'X'].map(name => [name, () => null])))
for (const name of ['Tooltip', 'ValueSlider', 'Segmented', 'Select']) mock.module(ui + `components/ui/${name}.tsx`, () => ({ [name]: () => null }))
mock.module(ui + 'features/replay/ReplayDepthLadder.tsx', () => ({ ReplayDepthLadder: () => null }))
const offscreen: any[] = [], composites: any[] = [], blits: any[] = [], rects: any[] = [], arcs: any[] = [], strokes: any[] = []
let historyPaints = 0, orderPaints = 0
const context = new Proxy({ measureText: (text: string) => ({ width: [...text].reduce((sum, char) => sum + (char === '1' ? 5 : 7), 0) }),
  drawImage: (layer: any, ...bounds: number[]) => { composites.push(layer); blits.push(bounds) }, rect: (...args: any[]) => rects.push(args),
  arc: (...args: any[]) => arcs.push(args), stroke: () => strokes.push(context.strokeStyle) } as any, { get: (object, key) => object[key] ?? (() => {}) })
mock.module(ui + 'components/viz/chartCanvas.ts', () => ({ prepareCanvas: (canvas: any) => canvas === target ? context : new Proxy({
  fillRect: () => historyPaints++, stroke: () => orderPaints++,
} as any, { get: (object, key) => object[key] ?? (() => {}) }), canvasLine() {}, readCanvasTheme: () => ({ font: 'sans-serif', ink: 'gray' }) }))
const observers: (() => void)[] = []
const observer = class { constructor(callback: () => void) { observers.push(callback) } observe() {} disconnect() {} }
Object.assign(globalThis, { ResizeObserver: observer, MutationObserver: observer,
  getComputedStyle: () => ({ getPropertyValue: () => 'monospace' }),
  document: { documentElement: {}, createElement: () => { const layer = {}; offscreen.push(layer); return layer }, fonts: { addEventListener() {}, removeEventListener() {} } },
  window: { requestAnimationFrame: (fn: FrameRequestCallback) => { viewFrames.set(++viewFrameId, fn); return viewFrameId }, cancelAnimationFrame: (id: number) => viewFrames.delete(id), setTimeout: (fn: () => void) => { timers.set(++timerId, fn); return timerId }, clearTimeout: (id: number) => timers.delete(id) },
})
const heatMock = await import(ui + '../tests/replay-heat.mock.ts')
mock.module(ui + 'features/replay/useReplayHeat.ts', () => heatMock)
const { ReplayChart } = await import(ui + 'features/replay/ReplayChart.tsx')
const { projectReplay } = await import(ui + 'features/replay/model.ts')
const data: any = { recording_id: 'r', window_id: 'w', context: { schema_version: 1, instrument: { X: { price_tick: .01 } } }, coverage: [{ start_us: 0, end_us: 100 }], gaps: [], state_before_start: { time_us: 0, state_through_seq: 0 }, messages: [
  { schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 1, topic: '/quote/X', data: { symbol: 'X', book_valid: true, available: true, valid_until_us: 1000, bids: [{ price: 99, quantity: 3 }], asks: [{ price: 99.5, quantity: 4 }] } },
], complete: true, next_cursor: null }
const projection = projectReplay(data, { time_us: 49 })
const props: any = { window: data, range: data.coverage[0], projection, symbol: 'X', executionId: 'E', selectedOrder: null, selectedTrade: null, onSelectOrder() {}, onSelectTrade() {}, onInspect() {}, onSeek() {} }
function find(node: any, predicate: (n: any) => boolean): any {
  if (Array.isArray(node)) { for (const child of node) { const found = find(child, predicate); if (found) return found } }
  else if (node && typeof node === 'object') return predicate(node) ? node : find(node.props?.children, predicate)
}
function draw() { for (const [id, fn] of [...viewFrames]) { viewFrames.delete(id); fn(0) }
  let attempts = 0
  do { dirty = false; at = 0; tree = ReplayChart(props); for (const effect of effects.splice(0)) effect(); assert.ok(++attempts < 20, '布局应在有限次渲染内稳定') } while (dirty)
}
const canvas = () => find(tree, n => n.type === 'canvas')
const axisLeft = () => find(tree, n => n.props?.className === 'replay-price-tick num').props.style.left
const tick = () => find(tree, n => n.props?.className === 'replay-price-tick num').props.children
const event = (x: number, y: number) => ({ button: 0, pointerId: 1, clientX: x, clientY: y, currentTarget: target })
function dragEnd(name = 'onPointerUp') { canvas().props[name](event(100, 100)); draw() }
function reset() { find(tree, n => n.props?.['aria-label'] === '恢复视图').props.onClick(); draw() }
draw()
const initial = axisLeft()
const initialLabel = find(tree, n => n.props?.className === 'replay-price-tick num')
assert.equal(900 - 346 - initial, initialLabel.props.style.width, '主图到盘口间只有一个价格槽，不预留第二列')
canvas().props.onPointerDown(event(100, 100)); draw()
canvas().props.onPointerMove(event(100, 700)); draw()
assert.equal(axisLeft(), initial, '上下平移跨整数位期间锁住主图边界')
dragEnd()
const expanded = axisLeft()
assert.ok(expanded < initial, '松手后才为多出的整数位扩宽')
reset()
assert.equal(axisLeft(), initial, '恢复视图收回历史长读数的多余宽度')
const beforeMainDrag = historyPaints, beforeMainLayers = offscreen.length
canvas().props.onPointerDown(event(100, 100)); draw()
for (const [x, y] of [[120, 110], [140, 130], [160, 140]]) {
  canvas().props.onPointerMove(event(x, y)); draw()
  assert.equal(historyPaints, beforeMainDrag, '主图拖动不重新聚合或绘制历史热力图')
  assert.equal(offscreen.length, beforeMainLayers, '主图拖动复用历史和委托缓存')
}
dragEnd(); reset()
const labelBefore = tick(), beforePriceScale = historyPaints
canvas().props.onPointerDown(event(550, 100)); draw()
for (const y of [150, 200, 400, 700]) {
  canvas().props.onPointerMove(event(550, y)); draw()
  assert.equal(historyPaints, beforePriceScale, '连续价格轴缩放不重建历史热力图')
}
assert.ok(JSON.stringify(find(tree, n => n.props?.className === 'replay-heat-scale')).includes('历史缩放预览'), '新增视野明确标识为预览，不能误认为没有挂量')
const transformed = blits.at(-4), scale = Math.exp(-600 / 160)
assert.ok(Math.abs(transformed[1] - 100 * (1 - scale)) < 1e-6, '历史图像按鼠标锚点纵向缩放')
assert.ok(Math.abs(transformed[3] - 300 * scale) < 1e-6, '历史图像与实时价格轴共享缩放比例')
assert.equal(axisLeft(), initial, '价格轴缩放期间也锁宽')
assert.notEqual(tick(), labelBefore, '价格轴拖动确实缩放刻度')
dragEnd('onPointerCancel')
assert.ok(historyPaints > beforePriceScale, '手势取消后按最终价格范围精确补绘')
reset()
const afterScale = axisLeft()
canvas().props.onPointerDown(event(100, 100)); draw()
canvas().props.onPointerMove(event(100, 1000000)); draw()
assert.equal(axisLeft(), afterScale)
dragEnd('onLostPointerCapture')
assert.ok(axisLeft() < afterScale, '失去捕获也要结束锁宽并补足空间')
reset()
props.window = { ...data, recording_id: 'other-recording' }; draw()
assert.equal(axisLeft(), initial, '切换录制重新适配紧凑轴宽')
const beforeWheel = axisLeft(), beforeWheelPaints = historyPaints
for (let i = 0; i < 3; i++) {
  listeners.get('wheel')!({ clientX: 550, clientY: 100, deltaY: 400, deltaMode: 0, preventDefault() {} }); draw()
  assert.equal(axisLeft(), beforeWheel, '连续滚轮期间锁宽')
  assert.equal(historyPaints, beforeWheelPaints, '连续价格轴滚轮不重建历史热力图')
}
for (const [id, fn] of [...timers]) { timers.delete(id); fn() } draw()
assert.ok(historyPaints > beforeWheelPaints, '滚轮停止后精确补绘历史')
assert.ok(axisLeft() < beforeWheel, '滚轮停止后为更长读数扩宽')
props.window = { ...props.window, window_id: 'next-window' }; draw()
assert.equal(axisLeft(), initial, '翻窗恢复视图后不保留历史最大宽度')
props.window = { ...data, recording_id: 'third-recording' }; draw()
assert.equal(axisLeft(), initial, '切换录制重新适配紧凑轴宽')
props.window = { ...data, window_id: 'legacy', context: { schema_version: 1 } }
props.projection = { ...projection, quoteHistory: projection.quoteHistory.map((quote: any) => ({ ...quote, bids: [{ price: 1.25, quantity: 3 }], asks: [{ price: 1.2504, quantity: 4 }] })) }
draw()
assert.match(tick(), /^1\.\d{4}$/, '缺元数据的旧录制按同段已录价格统一补齐四位')
const legacyWidth = find(tree, n => n.props?.className === 'replay-price-tick num').props.style.width
canvas().props.onPointerMove(event(100, 100)); draw()
const hoverLabel = find(tree, n => n.props?.className === 'replay-crosshair-price num')
assert.match(hoverLabel.props.children[1].props.children, /^1\.\d{4}$/, '悬停与刻度使用同一精度')
assert.equal(hoverLabel.props.style.width, legacyWidth, '悬停不会另撑一列')
props.scene = props.projection
props.selectedOrder = null
draw()
const cachedLayers = offscreen.length, cachedPaints = historyPaints, cachedOrders = orderPaints
assert.ok(cachedPaints > 0, '首次生成彩色和灰色历史层')
props.projection = { ...props.projection, time_us: 60 }
rects.length = 0; composites.length = 0; draw()
assert.equal(offscreen.length, cachedLayers, '不可变游标快照不重新生成历史层')
assert.equal(historyPaints, cachedPaints)
assert.equal(orderPaints, cachedOrders)
assert.equal(composites.length, 4, '游标合成两色历史与委托层')
const left = rects[0][0], split = rects[0][2] + left
assert.ok(split > left && rects.some(rect => rect[0] === split), '两色层在当前游标位置动态裁剪')
canvas().props.onPointerMove(event(100, 100)); draw()
assert.equal(offscreen.length, cachedLayers, '悬停不重新生成历史层')
assert.equal(historyPaints, cachedPaints)
assert.equal(orderPaints, cachedOrders)
observers[1](); draw()
assert.ok(offscreen.length > cachedLayers, '主题变化重新生成历史层')
const themed = offscreen.length
props.scene = { ...props.scene, quoteHistory: [...props.scene.quoteHistory] }; draw()
assert.ok(offscreen.length > themed, '新历史快照重新生成历史层')
const updated = offscreen.length
canvas().props.onKeyDown({ key: '+', preventDefault() {} }); draw()
assert.ok(offscreen.length > updated, '视图缩放重新生成历史层')
reset()
const sampleOrder: any = { key: 'A', order_id: 'A', symbol: 'X', price: 1.25, time_us: 50, capture_seq: 10,
  direction: 'BUY', confirmed: true, terminal: false, remaining_volume: 1, volume: 1, status: 'working' }
props.scene = { ...props.scene, time_us: 99, capture_seq: 1000, orderHistory: [sampleOrder] }
props.projection = { ...props.projection, time_us: 50, capture_seq: 9 }
arcs.length = 0; strokes.length = 0; draw()
assert.ok(strokes.includes('gray'), '同刻采集序号尚未到达的委托保持灰色')
const beforeOrderHover = offscreen.length
const { replayChartLayout, replayTimeX, replayPriceY } = await import(ui + 'features/replay/chartGeometry.ts')
const { replayObservedPriceView } = await import(ui + 'features/replay/depthGeometry.ts')
const orderLayout = replayChartLayout(900, 300, 900 - 346 - axisLeft())
const orderView = replayObservedPriceView(props.scene.quoteHistory, props.scene.orderHistory, [], null)!
canvas().props.onPointerMove(event(replayTimeX(50, props.range, orderLayout), replayPriceY(1.25, orderView, orderLayout))); draw()
assert.ok(arcs.some(arc => arc[2] === 5), '悬停仍放大命中的委托标记')
assert.equal(offscreen.length, beforeOrderHover, '委托高亮只重画命中标记，不重新生成全量历史')
const beforeSelection = offscreen.length
props.selectedOrder = 'A'; draw()
assert.equal(offscreen.length, beforeSelection + 2, '选中委托仅重建委托两色层，不重绘盘口历史')
console.log('ok')
