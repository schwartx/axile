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
mock.module('lucide-react', () => Object.fromEntries(['ChevronLeft', 'ChevronRight', 'Pause', 'Play', 'RefreshCw', 'SkipBack', 'SkipForward'].map(name => [name, () => null])))
for (const [path, name] of [['components/ui/BackLink.tsx', 'BackLink'], ['components/ui/ErrorNotice.tsx', 'ErrorNotice'], ['components/ui/Segmented.tsx', 'Segmented'], ['features/replay/ReplayChart.tsx', 'ReplayChart']]) mock.module(ui + path, () => ({ [name]: () => null }))

const manifest = { account_id: 7, execution_id: 'E2', availability: 'partial', recordings: [{ recording_id: 'r', symbols: ['X'], symbols_complete: true, coverage: [{ start_us: 0, end_us: 40_000_000 }], context: { schema_version: 1, instrument: {} } }] }
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
  return { ...make(JSON.stringify(request), request.start_us), coverage: [{ start_us: request.start_us, end_us: request.end_us }] }
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
  else if (node && typeof node === 'object') return predicate(node) ? node : find(node.props?.children, predicate)
  return null
}
function text(node: any): string {
  if (node == null || typeof node === 'boolean') return ''
  if (Array.isArray(node)) return node.map(text).join(' ')
  return typeof node === 'object' ? text(node.props?.children) : String(node)
}
function button(label: string) { const node = find(tree, node => node.type === 'button' && text(node).includes(label)); if (!node) throw Error('missing button: ' + label); return node }
function slider() { return find(tree, node => node.type === 'input' && node.props?.id === 'replay-time') }
function playing() { return Boolean(find(tree, node => node.props?.['aria-label'] === '暂停回放')) }
function hide(hidden: boolean) { pageDocument.hidden = hidden; for (const listener of [...listeners]) listener(); flush() }
function frame(now: number) { wall = now; const entry = frames.entries().next().value; if (entry) { frames.delete(entry[0]); entry[1](now); flush() } }
flush(); pendingManifest!(); await settle()
if (scenario === 'continue') {
  button('播放').props.onClick(); flush(); loadingNext = true; frame(20_001)
  await finishWindows()
  console.log(JSON.stringify({ playing: playing(), cursor: slider().props['aria-valuetext'] }))
} else if (scenario.startsWith('hidden')) {
  button('播放').props.onClick(); flush(); loadingNext = true; frame(20_001)
  hide(true)
  if (scenario === 'hidden-return-before-load') hide(false)
  await finishWindows()
  const hiddenPlaying = Boolean(find(tree, node => node.props?.['aria-label'] === '暂停回放'))
  hide(false); frame(50_001)
  console.log(JSON.stringify({ hiddenPlaying, playing: Boolean(find(tree, node => node.props?.['aria-label'] === '暂停回放')),
    cursor: find(tree, node => node.type === 'input' && node.props?.id === 'replay-time').props['aria-valuetext'] }))
} else if (scenario === 'cached-seek') {
  find(tree, node => node.props?.onSeek && node.props?.projection).props.onSeek(10_000_000); flush()
  const initial = slider().props['aria-valuetext']
  loadingNext = true; button('下一窗口').props.onClick(); flush()
  slider().props.onChange({ target: { value: '0' } }); flush()
  slider().props.onPointerUp(); await settle()
  const cursor = slider().props['aria-valuetext']
  await finishWindows()
  const afterLateResponse = slider().props['aria-valuetext']
  find(tree, node => node.props?.onSeek && node.props?.projection).props.onSeek(5_000_000); flush()
  console.log(JSON.stringify({ initial, cursor, afterLateResponse, afterNextSeek: slider().props['aria-valuetext'] }))
} else if (scenario.startsWith('pause-')) {
  button('播放').props.onClick(); flush(); loadingNext = true; frame(20_001)
  if (scenario === 'pause-drag') slider().props.onChange({ target: { value: '25000000' } })
  else find(tree, node => node.type === 'select' && node.props?.['aria-label'] === '窗口时长').props.onChange({ target: { value: '2' } })
  flush()
  const pausedBeforeLoad = !playing()
  await finishWindows()
  console.log(JSON.stringify({ pausedBeforeLoad, playingAfterLoad: playing(), seekLabel: text(find(tree, node => node.type === 'label' && node.props?.htmlFor === 'replay-time')) }))
} else if (scenario.startsWith('missing')) {
  const failed = { seek: Boolean(find(tree, node => node.type === 'input' && node.props?.id === 'replay-time')),
    playDisabled: button('播放').props.disabled, nextDisabled: button('下一窗口').props.disabled }
  if (scenario === 'missing-seek') {
    find(tree, node => node.type === 'input' && node.props?.id === 'replay-time').props.onChange({ target: { value: '500000' } }); flush()
    find(tree, node => node.type === 'input' && node.props?.id === 'replay-time').props.onPointerUp(); await settle()
  } else { button('下一窗口').props.onClick(); await settle() }
  console.log(JSON.stringify({ failed, recovered: !find(tree, node => node.props?.title === '回放窗口读取失败')?.props.error,
    orders: Boolean(find(tree, node => typeof node.type === 'function' && node.type.name === 'OrdersAndTrades')),
    cursor: find(tree, node => node.type === 'input' && node.props?.id === 'replay-time').props['aria-valuetext'] }))
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
    const detail = find(section, node => node.type === 'div' && Array.isArray(node.props.children)
      && node.props.children.some((child: any) => child?.type === 'h3' && text(child).includes('委托详情')))
    rows[id] = { row: text(row).replace(/\s+/g, ' ').trim(), detail: text(detail).replace(/\s+/g, ' ').trim() }
  }
  console.log(JSON.stringify(rows))
}
