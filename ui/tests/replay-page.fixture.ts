import { mock } from 'bun:test'

declare const ui: string
declare const scenario: string

// The page runs unchanged; only React scheduling, I/O and the browser clock are controlled.
let slots: any[] = [], at = 0, effects: any[] = [], dirty = false, tree: any, wall = 0, rafId = 0
let loadingNext = false, pendingManifest: (() => void) | null = null
const pendingWindows: (() => void)[] = []
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
mock.module('react', () => ({ useState, useRef, useMemo: memoValue, useEffect, useCallback: (fn: any, deps: any[]) => memoValue(() => fn, deps) }))
const jsx = (type: any, props: any, key: any) => ({ type, props, key })
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx, Fragment: 'fragment' }))
mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx, Fragment: 'fragment' }))
mock.module('react-router', () => ({ useParams: () => ({ id: '7', executionId: 'E2' }) }))
mock.module('lucide-react', () => Object.fromEntries(['ChevronLeft', 'ChevronRight', 'Info', 'Pause', 'Play', 'RefreshCw', 'SkipBack', 'SkipForward', 'X'].map(name => [name, () => null])))
for (const [path, name] of [['features/account/pageHead.tsx', 'AccountPageTitle'], ['components/ui/Select.tsx', 'Select'], ['components/ui/ValueSlider.tsx', 'ValueSlider'], ['components/ui/BackLink.tsx', 'BackLink'], ['components/ui/ErrorNotice.tsx', 'ErrorNotice'], ['components/ui/Segmented.tsx', 'Segmented'], ['components/ui/OverflowText.tsx', 'OverflowText'], ['components/ui/InkRewrite.tsx', 'InkRewrite'], ['features/replay/ReplayChart.tsx', 'ReplayChart']]) mock.module(ui + path, () => ({ [name]: () => null }))
mock.module(ui + 'stores/domain.ts', () => ({ useDomainStore: (select: any) => select({ accounts: [{ account_id: 7, name: 'demo', trade_channel: 'binance', market: 'crypto' }] }) }))
mock.module(ui + 'lib/viewTransition.ts', () => ({ withViewTransition: (update: () => void) => update() }))
mock.module(ui + 'components/ui/Tooltip.tsx', () => ({ Tooltip: () => null }))

const manifest = { account_id: 7, execution_id: 'E2', availability: 'partial', recordings: [{ recording_id: 'r', symbols: scenario === 'controls' ? ['X', 'Y'] : ['X'], symbols_complete: true, coverage: [{ start_us: 0, end_us: 40_000_000 }], context: { schema_version: 1, instrument: {} } }] }
if (scenario === 'whole-after-window') manifest.recordings[0].coverage.push({ start_us: 60_000_000, end_us: 100_000_000 })
mock.module(ui + 'lib/api/replay.ts', () => ({
  getReplayManifest: () => new Promise(resolve => { pendingManifest = () => resolve(manifest) }),
  readReplayWindow: async (_: string, request: any) => {
    if (scenario.startsWith('missing') && request.start_us === 0) throw Error('REPLAY_IO')
    if (request.start_us > 0 && loadingNext) return new Promise(resolve => pendingWindows.push(() => resolve(windowAt(request))))
    return windowAt(request)
  },
}))
const make = (id: string, start: number) => ({ recording_id: 'r', window_id: id, context: { schema_version: 1, instrument: {} },
  state_before_start: { state_through_seq: 2, time_us: start, orders: {
    old: { symbol: 'X', order_id: 'O1', execution_id: 'E1', origin: 'execution', status: 'working', confirmed_book: true, sync_generation: 1, quantity: 1, remaining_quantity: 1, price: 100 },
    current: { symbol: 'X', order_id: 'O2', execution_id: 'E2', origin: 'execution', status: 'working', confirmed_book: true, sync_generation: 1, quantity: 1, remaining_quantity: 1, price: 100 },
    manual: { symbol: 'X', order_id: 'M', origin: 'manual', status: 'working', confirmed_book: true, sync_generation: 1, quantity: 1, remaining_quantity: 1, price: 100 },
    unknown: { symbol: 'X', order_id: 'U', status: 'working', confirmed_book: true, sync_generation: 1, quantity: 1, remaining_quantity: 1, price: 100 },
    conflict: { symbol: 'X', order_id: 'C', execution_id: 'E2', origin: 'external', status: 'working', confirmed_book: true, sync_generation: 1, quantity: 1, remaining_quantity: 1, price: 100 },
  } }, messages: [], coverage: [{ start_us: start, end_us: start + 20_000_000 }], gaps: [], next_cursor: null, complete: true })
function windowAt(request: any) {
  return { ...make(JSON.stringify(request), request.start_us), coverage: [{ start_us: request.start_us, end_us: request.end_us }],
    ...(scenario === 'inspect' ? { messages: [{ schema_version: 1, recording_id: 'r', capture_seq: 3, time_us: 1_000_000, topic: '/trade', data: { symbol: 'X', trade_key: 'T', order_id: 'O1', execution_id: 'E1', quantity: 1, price: 100, direction: 'BUY', identity_quality: 'stable' } }, { schema_version: 1, recording_id: 'r', capture_seq: 4, time_us: 1_000_000, topic: '/noop', data: {} }] } : {}),
    ...(scenario === 'overview' ? { messages: [
      { schema_version: 1, recording_id: 'r', capture_seq: 3, time_us: 1_000_000, topic: '/quote/X', data: { symbol: 'X', book_valid: true, bids: [{ price: 100, quantity: 1 }], asks: [{ price: 101, quantity: 2 }], valid_until_us: 40_000_000 } },
      { schema_version: 1, recording_id: 'r', capture_seq: 4, time_us: 30_000_000, topic: '/order', data: { symbol: 'X', order_id: 'F', price: 100, quantity: 1, remaining_quantity: 1, status: 'working', confirmed_book: true } },
      { schema_version: 1, recording_id: 'r', capture_seq: 5, time_us: 35_000_000, topic: '/trade', data: { symbol: 'X', order_id: 'F', trade_key: 'future', identity_quality: 'stable', price: 100, quantity: 1 } },
      { schema_version: 1, recording_id: 'r', capture_seq: 6, time_us: 36_000_000, topic: '/order', data: { symbol: 'X', order_id: 'O2', price: 100, quantity: 1, remaining_quantity: 0, filled_quantity: 1, status: 'filled', terminal: true, sync_generation: 1 } },
      { schema_version: 1, recording_id: 'r', capture_seq: 7, time_us: 38_000_000, topic: '/quote/X', data: { symbol: 'X', book_valid: true, bids: [{ price: 100, quantity: 9 }], asks: [{ price: 101, quantity: 8 }], valid_until_us: 40_000_000 } },
    ] } : {}),
    ...(scenario === 'first-quote' ? { messages: [{ schema_version: 1, recording_id: 'r', capture_seq: 3, time_us: 1_000_000,
      topic: '/quote/X', data: { symbol: 'X', book_valid: true, bids: [{ price: 100, quantity: 1 }], asks: [{ price: 101, quantity: 2 }], valid_until_us: 2_000_000 } }] } : {}) }
}
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
async function settle() { for (let index = 0; index < 4; index++) { await Promise.resolve(); flush() } }
async function finishWindows() { loadingNext = false; for (const resolve of pendingWindows.splice(0)) resolve(); await settle() }
function find(node: any, predicate: (node: any) => boolean): any {
  if (Array.isArray(node)) { for (const child of node) { const match = find(child, predicate); if (match) return match } }
  else if (node && typeof node === 'object') return predicate(node) ? node : find([node.props?.children, node.props?.depthStatus], predicate)
  return null
}
function text(node: any): string {
  if (node == null || typeof node === 'boolean') return ''
  if (node?.props?.text != null) return String(node.props.text)
  if (Array.isArray(node)) return node.map(text).join(' ')
  return typeof node === 'object' ? text(node.props?.children) : String(node)
}
function button(label: string) { const node = find(tree, node => node.type === 'button' && text(node).includes(label)); if (!node) throw Error('missing button: ' + label); return node }
function slider() {
  const fallback = find(tree, node => node.props?.label === '录制定位')
  if (fallback) return fallback
  // The chart owns the axis; this page harness exercises its seek contract.
  const chart = find(tree, node => node.props?.symbolControl && node.props?.projection)
  return { props: { value: chart.props.projection.time_us - chart.props.range.start_us,
    formatValue: (offset: number) => String(offset),
    onChange: (offset: number) => chart.props.onSeek(chart.props.range.start_us + offset),
    onChangeEnd: (offset: number) => chart.props.onSeek(chart.props.range.start_us + offset) } }
}
function seekText() { return text(find(tree, node => node.props?.className?.includes('replay-cursor-time'))) }
function playing() { return Boolean(find(tree, node => node.props?.['aria-label'] === '暂停回放')) }
function hide(hidden: boolean) { pageDocument.hidden = hidden; for (const listener of [...listeners]) listener(); flush() }
function frame(now: number) { wall = now; const entry = frames.entries().next().value; if (entry) { frames.delete(entry[0]); entry[1](now); flush() } }
flush(); pendingManifest!(); await settle()
if (scenario !== 'overview') {
  find(tree, node => node.props?.ariaLabel === '窗口时长').props.onChange('20'); await settle()
}
if (scenario === 'overview') {
  const chart = () => find(tree, node => node.props?.symbolControl && node.props?.projection)
  const details = () => find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades')
  const initial = { range: chart().props.range, sceneTrades: chart().props.scene.ownTrades.length, currentTrades: chart().props.projection.ownTrades.length,
    sceneStatus: chart().props.scene.orders[JSON.stringify(['X','O2'])].status, currentStatus: chart().props.projection.orders[JSON.stringify(['X','O2'])].status }
  chart().props.onSeek(2_000_000); flush()
  const earlyBook = chart().props.projection.quotes.X.bids[0].quantity
  chart().props.onSelectOrder(JSON.stringify(['X','F'])); flush()
  const pending = details().props.selectionPending
  chart().props.onSelectTrade('5:future'); flush()
  const future = { selected: details().props.selectedTradeKey, currentTrades: chart().props.projection.ownTrades.length }
  chart().props.onSeek(39_000_000); flush()
  const lateBook = chart().props.projection.quotes.X.bids[0].quantity
  chart().props.onSeek(2_000_000); flush()
  console.log(JSON.stringify({ initial, earlyBook, lateBook, restoredBook: chart().props.projection.quotes.X.bids[0].quantity, pending, future }))
} else if (scenario === 'whole-after-window') {
  const ranges = []
  for (const start of [0, 60_000_000]) {
    find(tree, node => node.props?.ariaLabel === '选择可用覆盖时段').props.onChange(start); await settle()
    find(tree, node => node.props?.ariaLabel === '窗口时长').props.onChange('2'); await settle()
    find(tree, node => node.props?.['aria-label'] === '下一窗口').props.onClick(); await settle()
    const later = find(tree, node => node.props?.symbolControl && node.props?.projection).props.range
    find(tree, node => node.props?.ariaLabel === '窗口时长').props.onChange('all'); await settle()
    ranges.push({ later, whole: find(tree, node => node.props?.symbolControl && node.props?.projection).props.range })
  }
  console.log(JSON.stringify(ranges))
} else if (scenario === 'controls') {
  const title = find(tree, node => node.props?.page === '执行回放').props
  const chart = () => find(tree, node => node.props?.projection && node.props?.symbolControl)
  const picker = chart().props.symbolControl
  picker.props.onChange('Y'); await settle()
  console.log(JSON.stringify({ title, searchable: picker.props.searchable, symbol: chart().props.symbol,
    nativeControl: Boolean(find(tree, node => node.type === 'select' || node.type === 'input' && node.props?.type === 'range')) }))
} else if (scenario === 'draft-commit') {
  slider().props.onChange(5_000_000); flush()
  const preview = seekText()
  const chartTimeBeforeCommit = find(tree, node => node.props?.projection)?.props.projection.time_us
  slider().props.onChangeEnd(7_000_000); await settle()
  console.log(JSON.stringify({ preview, chartTimeBeforeCommit, cursor: seekText(), playing: playing() }))
} else if (scenario === 'selection') {
  const details = () => find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades')
  const initial = details().props.selected
  details().props.onSelect(JSON.stringify(['X', 'M'])); flush()
  console.log(JSON.stringify({ initial, selected: details().props.selected }))
} else if (scenario === 'inspect') {
  const chart = () => find(tree, node => node.props?.symbolControl && node.props?.projection)
  const details = () => find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades')
  chart().props.onSeek(2_000_000); flush()
  button('播放').props.onClick(); flush()
  const before = JSON.stringify(chart().props.projection)
  const trade = chart().props.projection.ownTrades[0]
  chart().props.onSelectTrade(`${trade.capture_seq}:${trade.key}`); flush()
  const linked = { order: details().props.selected, trade: details().props.selectedTradeKey, chartTrade: chart().props.selectedTrade, tab: details().props.tab }
  const unchanged = before === JSON.stringify(chart().props.projection)
  chart().props.onSelectOrder(JSON.stringify(['X', 'M'])); flush()
  console.log(JSON.stringify({ linked, unchanged, playing: playing(), selectedOrder: details().props.selected, selectedTrade: chart().props.selectedTrade, tab: details().props.tab }))
} else if (scenario === 'trade-seek') {
  button('下一窗口').props.onClick(); await settle()
  find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades').props.onSeek(5_000_000)
  await settle()
  console.log(JSON.stringify({ cursor: seekText(), playing: playing() }))
} else if (scenario === 'first-quote') {
  const before = Boolean(find(tree, node => node.props?.projection)?.props.projection.quotes.X)
  button('首个盘口').props.onClick(); flush()
  console.log(JSON.stringify({ before, after: Boolean(find(tree, node => node.props?.projection)?.props.projection.quotes.X),
    cursor: seekText(), playing: playing() }))
} else if (scenario === 'restart') {
  button('下一窗口').props.onClick(); await settle()
  button('播放').props.onClick(); flush(); frame(20_001)
  const stopped = seekText()
  button('播放').props.onClick(); flush(); frame(20_501)
  console.log(JSON.stringify({ stopped, cursor: seekText(), playing: playing() }))
} else if (scenario === 'continue') {
  button('播放').props.onClick(); flush(); loadingNext = true; frame(20_001)
  await finishWindows()
  console.log(JSON.stringify({ playing: playing(), cursor: seekText() }))
} else if (scenario.startsWith('hidden')) {
  button('播放').props.onClick(); flush(); loadingNext = true; frame(20_001)
  hide(true)
  if (scenario === 'hidden-return-before-load') hide(false)
  await finishWindows()
  const hiddenPlaying = Boolean(find(tree, node => node.props?.['aria-label'] === '暂停回放'))
  hide(false); frame(50_001)
  console.log(JSON.stringify({ hiddenPlaying, playing: Boolean(find(tree, node => node.props?.['aria-label'] === '暂停回放')),
    cursor: seekText() }))
} else if (scenario === 'cached-seek') {
  find(tree, node => node.props?.onSeek && node.props?.projection).props.onSeek(10_000_000); flush()
  const initial = seekText()
  loadingNext = true; button('下一窗口').props.onClick(); flush()
  slider().props.onChange(0); flush()
  slider().props.onChangeEnd(slider().props.value); await settle()
  const cursor = seekText()
  await finishWindows()
  const afterLateResponse = seekText()
  find(tree, node => node.props?.onSeek && node.props?.projection).props.onSeek(5_000_000); flush()
  console.log(JSON.stringify({ initial, cursor, afterLateResponse, afterNextSeek: seekText() }))
} else if (scenario.startsWith('pause-')) {
  button('播放').props.onClick(); flush(); loadingNext = true; frame(20_001)
  if (scenario === 'pause-drag') { slider().props.onChange(25000000); slider().props.onChangeEnd(25000000) }
  else find(tree, node => node.props?.ariaLabel === '窗口时长').props.onChange('2')
  flush()
  const pausedBeforeLoad = !playing()
  await finishWindows()
  console.log(JSON.stringify({ pausedBeforeLoad, playingAfterLoad: playing(), seekLabel: seekText() }))
} else if (scenario.startsWith('missing')) {
  const failed = { seek: Boolean(slider()),
    playDisabled: button('播放').props.disabled, nextDisabled: button('下一窗口').props.disabled }
  if (scenario === 'missing-seek') {
    slider().props.onChange(500000); flush()
    slider().props.onChangeEnd(slider().props.value); await settle()
  } else { button('下一窗口').props.onClick(); await settle() }
  console.log(JSON.stringify({ failed, recovered: !find(tree, node => node.props?.title === '回放窗口读取失败')?.props.error,
    orders: Boolean(find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades')),
    cursor: seekText() }))
} else if (scenario === 'ownership') {
  function expand(node: any): any {
    if (Array.isArray(node)) return node.map(expand)
    if (node && typeof node === 'object') {
      if (typeof node.type === 'function' && ['OrdersAndTrades', 'RecordedFields'].includes(node.type.name)) return expand(node.type(node.props))
      return { ...node, props: { ...node.props, children: expand(node.props?.children) } }
    }
    return node
  }
  const rows: Record<string, { row: string; detail: string }> = {}
  for (const id of ['O1', 'O2', 'M', 'U', 'C']) {
    find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades').props.onSelect(JSON.stringify(['X', id])); flush()
    const section = find(expand(tree), node => node.type === 'section' && node.props?.['aria-label'] === '游标处自有委托')
    const row = find(section, node => node.type === 'tr' && Boolean(find(node.props.children, child => child.type === 'button' && text(child) === id)))
    const detail = find(expand(tree), node => node.type === 'div' && Array.isArray(node.props.children)
      && node.props.children.some((child: any) => child?.type === 'h3' && text(child).includes('委托详情')))
    rows[id] = { row: text(row).replace(/\s+/g, ' ').trim(), detail: text(detail).replace(/\s+/g, ' ').trim() }
  }
  console.log(JSON.stringify(rows))
}
