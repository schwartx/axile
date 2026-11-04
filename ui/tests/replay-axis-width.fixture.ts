/// <reference types="bun" />
import { mock } from 'bun:test'
import assert from 'node:assert/strict'
declare const ui: string
let at = 0, dirty = false, tree: any
const slots: any[] = [], effects: (() => void)[] = [], timers = new Map<number, () => void>()
let timerId = 0
mock.module('react', () => ({
  useRef: (initial: any) => { const i = at++; return slots[i] ??= { current: initial } },
  useState: (initial: any) => {
    const i = at++; slots[i] ??= { value: initial }
    return [slots[i].value, (next: any) => { const value = typeof next === 'function' ? next(slots[i].value) : next; if (!Object.is(value, slots[i].value)) { slots[i].value = value; dirty = true } }]
  },
  useMemo: (fn: any) => fn(), useCallback: (fn: any) => fn,
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
const context = new Proxy({ measureText: (text: string) => ({ width: [...text].reduce((sum, char) => sum + (char === '1' ? 5 : 7), 0) }) } as any, { get: (object, key) => object[key] ?? (() => {}) })
mock.module(ui + 'components/viz/chartCanvas.ts', () => ({ prepareCanvas: () => context, canvasLine() {}, readCanvasTheme: () => ({ font: 'sans-serif' }) }))
const observer = class { observe() {} disconnect() {} }
Object.assign(globalThis, { ResizeObserver: observer, MutationObserver: observer,
  getComputedStyle: () => ({ getPropertyValue: () => 'monospace' }),
  document: { documentElement: {}, fonts: { addEventListener() {}, removeEventListener() {} } },
  window: { setTimeout: (fn: () => void) => { timers.set(++timerId, fn); return timerId }, clearTimeout: (id: number) => timers.delete(id) },
})
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
function draw() {
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
assert.equal(900 - 346 - (initial - 2), initialLabel.props.style.width + 4, '主图到盘口间只有一个价格槽，不预留第二列')
canvas().props.onPointerDown(event(100, 100)); draw()
canvas().props.onPointerMove(event(100, 700)); draw()
assert.equal(axisLeft(), initial, '上下平移跨整数位期间锁住主图边界')
dragEnd()
const expanded = axisLeft()
assert.ok(expanded < initial, '松手后才为多出的整数位扩宽')
reset()
assert.equal(axisLeft(), initial, '恢复视图收回历史长读数的多余宽度')
const labelBefore = tick()
canvas().props.onPointerDown(event(550, 100)); draw()
canvas().props.onPointerMove(event(550, 700)); draw()
assert.equal(axisLeft(), initial, '价格轴缩放期间也锁宽')
assert.notEqual(tick(), labelBefore, '价格轴拖动确实缩放刻度')
dragEnd('onPointerCancel')
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
const beforeWheel = axisLeft()
for (let i = 0; i < 3; i++) {
  listeners.get('wheel')!({ clientX: 550, clientY: 100, deltaY: 400, deltaMode: 0, preventDefault() {} }); draw()
  assert.equal(axisLeft(), beforeWheel, '连续滚轮期间锁宽')
}
for (const [id, fn] of [...timers]) { timers.delete(id); fn() } draw()
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
console.log('ok')
