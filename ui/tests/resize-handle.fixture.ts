import { mock } from 'bun:test'

declare const ui: string
let slots: any[] = [], at = 0, effects: (() => void)[] = []
mock.module('react', () => ({
  useEffect: (fn: () => void) => { const index = at++; if (!slots[index]) { slots[index] = true; effects.push(fn) } },
  useRef: (value: any) => { const index = at++; slots[index] ??= { current: value }; return slots[index] },
  useState: (value: any) => { const index = at++; slots[index] ??= { value: typeof value === 'function' ? value() : value }; return [slots[index].value, (next: any) => { slots[index].value = next }] },
}))
const jsx = (type: any, props: any) => ({ type, props })
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx }))
mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx }))
const { ResizeHandle } = await import(ui + 'components/ui/ResizeHandle.tsx')
function setup(extra: any = {}) {
  slots = []; at = 0
  const changes: number[] = [], commits: number[] = [], resizing: boolean[] = []
  let resets = 0, captured = false
  const props = ResizeHandle({ orientation: 'horizontal', value: 50, min: 20, max: 80, step: 5,
    valueAt: (y: number) => Math.min(80, Math.max(20, y)), onChange: (v: number) => changes.push(v),
    onCommit: (v: number) => commits.push(v), onResizeChange: (v: boolean) => resizing.push(v),
    onReset: () => resets++, ...extra }).props
  const target = { focus() {}, setPointerCapture() { captured = true }, hasPointerCapture() { return captured },
    releasePointerCapture() { captured = false; props.onLostPointerCapture() } }
  const pointer = (y: number, extra: any = {}) => ({ clientX: 30, clientY: y, pointerId: 1, isPrimary: true, button: 0, preventDefault() {}, currentTarget: target, ...extra })
  const key = (key: string) => props.onKeyDown({ key, preventDefault() {} })
  return { props, changes, commits, resizing, pointer, key, resets: () => resets }
}
const drag = setup()
drag.props.onPointerUp(drag.pointer(70))
drag.props.onPointerDown(drag.pointer(40)); drag.props.onPointerMove(drag.pointer(60))
drag.props.onPointerUp(drag.pointer(75)); drag.props.onPointerUp(drag.pointer(80))
const cancel = setup()
cancel.props.onPointerDown(cancel.pointer(40)); cancel.props.onPointerCancel(cancel.pointer(50))
cancel.props.onPointerMove(cancel.pointer(70)); cancel.props.onPointerUp(cancel.pointer(80))
const lost = setup()
lost.props.onPointerDown(lost.pointer(40)); lost.props.onLostPointerCapture(); lost.props.onPointerUp(lost.pointer(80))
const keys = setup({ step: -5 })
for (const key of ['ArrowUp', 'ArrowDown', 'Home', 'End', 'ArrowLeft']) keys.key(key)
keys.props.onDoubleClick()
const width = setup({ orientation: 'vertical', valueAt: (x: number) => x })
width.props.onPointerDown(width.pointer(60)); width.props.onPointerUp(width.pointer(70)); width.key('ArrowRight')
const disabled = setup({ disabled: true })
disabled.props.onPointerDown(disabled.pointer(60)); disabled.key('End'); disabled.props.onDoubleClick()
const secondary = setup()
secondary.props.onPointerDown(secondary.pointer(60, { button: 2 })); secondary.props.onPointerDown(secondary.pointer(60, { isPrimary: false }))
const { ReplayStage } = await import(ui + 'features/replay/ReplayStage.tsx')
const storage = new Map<string, string>()
;(globalThis as any).window = { localStorage: { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) } }
let measure = () => {}
;(globalThis as any).ResizeObserver = class { constructor(fn: () => void) { measure = fn } observe() {} disconnect() {} }
slots = []; at = 0; effects = []
let stageHeight = 900
function renderStage() { at = 0; return ReplayStage({ children: null }).props }
let stage = renderStage()
stage.ref.current = { getBoundingClientRect: () => ({ bottom: stageHeight, height: stageHeight }) }
effects.forEach(fn => fn()); stage = renderStage()
const handle = () => stage.children[1].props
const initialHeight = handle().value
handle().onCommit(700); stage = renderStage()
const limitedHeight = handle().value
const savedHeight = storage.get('axon.replay.dockHeight')
stageHeight = 200; measure(); stage = renderStage()
const smallHeight = handle().value
stageHeight = 900; measure(); stage = renderStage()
const restoredHeight = handle().value
handle().onReset(); stage = renderStage()
const resetHeight = handle().value
console.log(JSON.stringify({
  drag: { changes: drag.changes, commits: drag.commits, resizing: drag.resizing },
  cancel: { changes: cancel.changes, commits: cancel.commits, resizing: cancel.resizing },
  lost: { commits: lost.commits, resizing: lost.resizing },
  keys: keys.commits, resets: keys.resets(), width: width.commits,
  disabled: { changes: disabled.changes, commits: disabled.commits, resets: disabled.resets(), tabIndex: disabled.props.tabIndex },
  secondary: secondary.changes,
  stage: { initialHeight, limitedHeight, savedHeight, smallHeight, restoredHeight, resetHeight },
}))
