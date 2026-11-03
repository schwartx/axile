import { mock } from 'bun:test'

declare const ui: string
declare const scenario: string

let slots: any[] = [], at = 0, effects: any[] = [], dirty = false, tree: any, wall = 0, rafId = 0
let pendingManifest: (() => void) | null = null
let requests: any[] = [], readCount = 0
const listeners = new Set<() => void>(), frames = new Map<number, (now: number) => void>()
const equal = (a: any[], b: any[]) => a?.length === b?.length && a.every((value, index) => Object.is(value, b[index]))
function useState(initial: any) {
  const index = at++
  slots[index] ??= { value: typeof initial === 'function' ? initial() : initial }
  return [slots[index].value, (next: any) => {
    const value = typeof next === 'function' ? next(slots[index].value) : next
    if (!Object.is(value, slots[index].value)) { slots[index].value = value; dirty = true }
  }]
}
function useRef(initial: any) { const index = at++; slots[index] ??= { current: initial }; return slots[index] }
function memoValue(fn: any, deps: any[]) {
  const index = at++
  if (!(index in slots) || !equal(slots[index].deps, deps)) slots[index] = { deps, value: fn() }
  return slots[index].value
}
function useEffect(fn: any, deps: any[]) {
  const index = at++
  if (!(index in slots) || !equal(slots[index].deps, deps)) {
    const cleanup = slots[index]?.cleanup
    slots[index] = { deps, cleanup }; effects.push({ index, fn, cleanup })
  }
}
mock.module('react', () => ({ Fragment: 'fragment', useState, useRef, useMemo: memoValue, useEffect, useLayoutEffect: useEffect, useCallback: (fn: any, deps: any[]) => memoValue(() => fn, deps) }))
const jsx = (type: any, props: any, key: any) => ({ type, props, key })
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx, Fragment: 'fragment' }))
mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx, Fragment: 'fragment' }))
mock.module('react-router', () => ({ useParams: () => ({ id: '7', executionId: 'E2' }) }))
mock.module('lucide-react', () => Object.fromEntries(['Circle', 'Crosshair', 'Minus', 'RotateCcw', 'Info', 'Pause', 'Play', 'RefreshCw', 'SkipBack', 'SkipForward'].map(name => [name, () => null])))
for (const [path, name] of [['features/account/pageHead.tsx', 'AccountPageTitle'], ['components/ui/Select.tsx', 'Select'], ['components/ui/ErrorNotice.tsx', 'ErrorNotice'], ['components/ui/Segmented.tsx', 'Segmented'], ['components/ui/OverflowText.tsx', 'OverflowText'], ['components/ui/InkRewrite.tsx', 'InkRewrite'], ['features/replay/ReplayChart.tsx', 'ReplayChart']]) mock.module(ui + path, () => ({ [name]: () => null }))
mock.module(ui + 'stores/domain.ts', () => ({ useDomainStore: (select: any) => select({ accounts: [{ account_id: 7, name: 'demo', trade_channel: 'binance', market: 'crypto' }] }) }))
mock.module(ui + 'lib/viewTransition.ts', () => ({ withViewTransition: (update: () => void) => update() }))
mock.module(ui + 'components/ui/Tooltip.tsx', () => ({ Tooltip: () => null }))

const recording = (recording_id: string, start_us: number, end_us: number) => ({ recording_id, symbols: scenario === 'controls' ? ['X', 'Y'] : ['X'], symbols_complete: true, coverage: [{ start_us, end_us }], context: { schema_version: 1, instrument: {} } })
const manifest = { account_id: 7, execution_id: 'E2', availability: 'partial', recordings: scenario === 'full-execution'
  ? [recording('r0', 0, 10_000_000), recording('r1', 20_000_000, 40_000_000)] : [recording('r', 0, 40_000_000)] }
if (scenario === 'selected-execution') Object.assign(manifest, { execution_start_us: 25_000_123 })
const order = (id: string, extra: any) => ({ symbol: 'X', order_id: id, status: 'working', confirmed_book: true, sync_generation: 1, quantity: 1, remaining_quantity: 1, price: 100, ...extra })
function windowAt(segment: any) {
  const start = segment.range.start_us, end = segment.range.end_us, recording_id = segment.recording.recording_id
  const quote = (seq: number, time: number, quantity: number) => ({ schema_version: 1, recording_id, capture_seq: seq, time_us: time, topic: '/quote/X', data: { symbol: 'X', book_valid: true, available: true, bids: [{ price: 100, quantity }], asks: [{ price: 101, quantity: 2 }], valid_until_us: end } })
  const trade = (seq: number, time: number, key: string, order_id = 'O1') => ({ schema_version: 1, recording_id, capture_seq: seq, time_us: time, topic: '/trade', data: { symbol: 'X', trade_key: key, order_id, execution_id: 'E1', quantity: 1, price: 100, direction: 'BUY', identity_quality: 'stable' } })
  let messages: any[] = []
  if (scenario === 'inspect') messages = [trade(3, 1_000_000, 'T'), { schema_version: 1, recording_id, capture_seq: 4, time_us: 1_000_000, topic: '/noop', data: {} }]
  if (scenario === 'first-quote') messages = [quote(3, 1_000_000, 1)]
  if (scenario === 'overview') messages = [
    quote(3, 1_000_000, 1),
    { schema_version: 1, recording_id, capture_seq: 4, time_us: 3_000_000, topic: '/order', data: { symbol: 'X', order_id: 'F', price: 100, quantity: 1, remaining_quantity: 1, status: 'working', confirmed_book: true } },
    trade(5, 5_000_000, 'future', 'F'),
    { schema_version: 1, recording_id, capture_seq: 6, time_us: 6_000_000, topic: '/order', data: { symbol: 'X', order_id: 'O2', price: 100, quantity: 1, remaining_quantity: 0, filled_quantity: 1, status: 'filled', terminal: true, sync_generation: 1 } },
    quote(7, 8_000_000, 9),
  ]
  if (scenario === 'full-execution') messages = [quote(3, start + 1_000_000, recording_id === 'r0' ? 1 : 9), trade(4, start + 3_000_000, 'T')]
  return { recording_id, window_id: recording_id + ':' + start, context: segment.recording.context,
    state_before_start: { recording_id, state_through_seq: 2, time_us: start, orders: {
      old: order('O1', { execution_id: 'E1', origin: 'execution' }),
      current: order('O2', { execution_id: 'E2', origin: 'execution' }),
      manual: order('M', { origin: 'manual' }),
      unknown: order('U', {}),
      conflict: order('C', { execution_id: 'E2', origin: 'external' }),
    } }, messages: messages.filter(message => message.time_us >= start && message.time_us < end), coverage: [segment.range], gaps: [], next_cursor: null, complete: true }
}
mock.module(ui + 'lib/api/replay.ts', () => ({
  getReplayManifest: () => new Promise(resolve => { pendingManifest = () => resolve(manifest) }),
  readReplayExecution: async (_: string, segments: any[], symbol: string) => {
    readCount++
    requests.push({ symbol, ranges: segments.map(segment => ({ recording: segment.recording.recording_id, ...segment.range })) })
    if (scenario === 'retry' && readCount === 1) throw Error('REPLAY_IO')
    return segments.map(segment => ({ range: segment.range, window: windowAt(segment) }))
  },
}))
const pageDocument = { hidden: false, addEventListener: (_: string, listener: () => void) => listeners.add(listener), removeEventListener: (_: string, listener: () => void) => listeners.delete(listener) }
Object.assign(globalThis, { window: { setTimeout, clearTimeout }, document: pageDocument,
  requestAnimationFrame: (fn: (now: number) => void) => { frames.set(++rafId, fn); return rafId }, cancelAnimationFrame: (id: number) => frames.delete(id) })
Object.defineProperty(globalThis, 'performance', { value: { now: () => wall }, configurable: true })
const { ExecutionReplayPage } = await import(ui + 'pages/ExecutionReplayPage.tsx')
function flush() {
  let count = 0
  do {
    dirty = false; at = 0; effects = []; tree = ExecutionReplayPage()
    for (const effect of effects) effect.cleanup?.()
    for (const effect of effects) slots[effect.index].cleanup = effect.fn()
    if (++count > 20) throw Error('page did not settle')
  } while (dirty)
}
async function settle() { for (let index = 0; index < 8; index++) { await Promise.resolve(); flush() } }
function find(node: any, predicate: (node: any) => boolean): any {
  if (Array.isArray(node)) { for (const child of node) { const match = find(child, predicate); if (match) return match } }
  else if (node && typeof node === 'object') return predicate(node) ? node : find(node.props?.children, predicate) ?? find(node.props?.playControls, predicate)
  return null
}
function text(node: any): string {
  if (node == null || typeof node === 'boolean') return ''
  if (node?.props?.text != null) return String(node.props.text)
  if (Array.isArray(node)) return node.map(text).join(' ')
  return typeof node === 'object' ? text(node.props?.children) : String(node)
}
function chart() { return find(tree, node => node.props?.symbolControl && node.props?.projection) }
function details() { return find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades') }
function button(label: string) { const node = find(tree, node => node.props?.['aria-label'] === label); if (!node) throw Error('missing button: ' + label); return node }
function seekText() { return text(find(tree, node => node.props?.className?.includes('replay-cursor-time'))) }
function playing() { return Boolean(find(tree, node => node.props?.['aria-label'] === '暂停回放')) }
function hide(hidden: boolean) { pageDocument.hidden = hidden; for (const listener of [...listeners]) listener(); flush() }
function frame(now: number) { wall = now; const entry = frames.entries().next().value; if (entry) { frames.delete(entry[0]); entry[1](now); flush() } }
function expand(node: any): any {
  if (Array.isArray(node)) return node.map(expand)
  if (node && typeof node === 'object') {
    if (typeof node.type === 'function' && ['OrdersAndTrades', 'RecordedFields'].includes(node.type.name)) return expand(node.type(node.props))
    return { ...node, props: { ...node.props, children: expand(node.props?.children) } }
  }
  return node
}
flush(); pendingManifest!(); await settle()
if (scenario === 'controls') {
  const title = find(tree, node => node.props?.page === '执行回放').props
  const picker = chart().props.symbolControl
  picker.props.onChange('Y'); await settle()
  console.log(JSON.stringify({ title, searchable: picker.props.searchable, symbol: chart().props.symbol, nativeControl: Boolean(find(tree, node => node.type === 'select' || node.type === 'input' && node.props?.type === 'range')) }))
} else if (scenario === 'seek') {
  chart().props.onSeek(5_000_000); flush()
  const preview = seekText(), chartTime = chart().props.projection.time_us
  chart().props.onSeek(7_000_000); flush()
  console.log(JSON.stringify({ preview, chartTime, cursor: seekText(), playing: playing() }))
} else if (scenario === 'speed') {
  const picker = () => find(tree, node => node.props?.['aria-label'] === '播放速度').props.children
  const options = picker().props.options, initial = picker().props.value
  chart().props.onSeek(5_000_000); flush(); picker().props.onChange('0.1'); flush()
  const paused = { cursor: seekText(), playing: playing(), speed: picker().props.value }
  button('播放回放').props.onClick(); flush(); frame(1000)
  const slow = seekText()
  picker().props.onChange('2'); flush()
  const switched = { cursor: seekText(), playing: playing() }
  frame(2000)
  console.log(JSON.stringify({ options, initial, paused, slow, switched, fast: seekText() }))
} else if (scenario === 'selection') {
  const initial = details().props.selected
  details().props.onSelect(JSON.stringify(['X', 'M'])); flush()
  console.log(JSON.stringify({ initial, selected: details().props.selected }))
} else if (scenario === 'first-quote') {
  const before = Boolean(chart().props.projection.quotes.X), entry = chart().props.firstQuoteControl
  entry.props.onClick(); flush()
  console.log(JSON.stringify({ before, prominent: !entry.props.disabled, after: Boolean(chart().props.projection.quotes.X), cursor: seekText(), playing: playing() }))
} else if (scenario === 'inspect') {
  chart().props.onSeek(2_000_000); flush(); button('播放回放').props.onClick(); flush()
  const before = JSON.stringify(chart().props.projection), trade = chart().props.projection.ownTrades[0]
  chart().props.onSelectTrade(trade.recording_id + ':' + trade.capture_seq + ':' + trade.key); flush()
  const linked = { order: details().props.selected, trade: details().props.selectedTradeKey, chartTrade: chart().props.selectedTrade, tab: details().props.tab }
  const unchanged = before === JSON.stringify(chart().props.projection)
  chart().props.onSelectOrder(JSON.stringify(['X', 'M'])); flush()
  console.log(JSON.stringify({ linked, unchanged, playing: playing(), selectedOrder: details().props.selected, selectedTrade: chart().props.selectedTrade, tab: details().props.tab }))
} else if (scenario === 'overview') {
  const initial = { range: chart().props.range, sceneTrades: chart().props.scene.ownTrades.length, currentTrades: chart().props.projection.ownTrades.length,
    sceneStatus: chart().props.scene.orders[JSON.stringify(['X','O2'])].status, currentStatus: chart().props.projection.orders[JSON.stringify(['X','O2'])].status }
  chart().props.onSeek(2_000_000); flush()
  const earlyBook = chart().props.projection.quotes.X.bids[0].quantity
  chart().props.onSelectOrder(JSON.stringify(['X','F'])); flush()
  const futureOrder = find(expand(details()), node => node.type === 'tr' && node.props?.['data-future'] === true)
  const preview = text(futureOrder)
  chart().props.onSelectTrade('r:5:future'); flush()
  const future = { selected: details().props.selectedTradeKey, currentTrades: chart().props.projection.ownTrades.length }
  chart().props.onSeek(9_000_000); flush()
  const lateBook = chart().props.projection.quotes.X.bids[0].quantity
  chart().props.onSeek(2_000_000); flush()
  console.log(JSON.stringify({ initial, earlyBook, lateBook, restoredBook: chart().props.projection.quotes.X.bids[0].quantity, preview, future }))
} else if (scenario === 'full-execution') {
  const initial = { range: chart().props.range, slices: chart().props.slices.map((slice: any) => [slice.window.recording_id, slice.range]), quotes: chart().props.scene.quoteHistory.map((quote: any) => [quote.time_us, quote.valid_until_us]), cursor: chart().props.projection.time_us }
  const hasRecordingDialog = Boolean(find(tree, node => node.props?.id === 'replay-information'))
  chart().props.onSeek(15_000_000); flush()
  const gap = { quotes: Object.keys(chart().props.projection.quotes), orders: Object.keys(chart().props.projection.orders), status: text(tree).includes('当前时刻未录制'), rows: text(expand(details())) }
  chart().props.onSeek(25_000_000); flush()
  const later = { quantity: chart().props.projection.quotes.X.bids[0].quantity, trades: chart().props.projection.ownTrades.map((trade: any) => trade.recording_id + ':' + trade.capture_seq + ':' + trade.key) }
  chart().props.onSeek(0); flush(); button('播放回放').props.onClick(); flush(); frame(25_000)
  console.log(JSON.stringify({ initial, hasRecordingDialog, requests, gap, later, played: chart().props.projection.time_us }))
} else if (scenario === 'selected-execution') {
  console.log(JSON.stringify({ range: chart().props.range, cursor: chart().props.projection.time_us, playing: playing(), requests }))
} else if (scenario === 'hidden') {
  button('播放回放').props.onClick(); flush(); frame(1000); hide(true)
  const paused = !playing(), atHide = seekText()
  hide(false); frame(5000)
  console.log(JSON.stringify({ paused, atHide, after: seekText(), playing: playing() }))
} else if (scenario === 'restart') {
  button('播放回放').props.onClick(); flush(); frame(40_001)
  const stopped = seekText()
  button('播放回放').props.onClick(); flush(); frame(40_501)
  console.log(JSON.stringify({ stopped, cursor: seekText(), playing: playing() }))
} else if (scenario === 'retry') {
  const failure = find(tree, node => node.props?.title === '执行回放读取失败')
  const failed = Boolean(failure?.props.error)
  failure.props.onRetry(); flush(); pendingManifest!(); await settle()
  console.log(JSON.stringify({ failed, recovered: Boolean(chart()), readCount }))
}
