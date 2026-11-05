/// <reference types="bun" />
import { mock } from 'bun:test'

declare const ui: string

let slots: any[] = [], at = 0
mock.module('react', () => ({
  useRef: (value: any) => { const index = at++; slots[index] ??= { current: value }; return slots[index] },
  useState: (value: any) => { const index = at++; slots[index] ??= { value }; return [slots[index].value, (next: any) => { slots[index].value = next }] },
}))
const jsx = (type: any, props: any) => ({ type, props })
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx }))
mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx }))
const { ValueSlider } = await import(ui + 'components/ui/ValueSlider.tsx')

function setup(extra: any = {}) {
  slots = []; at = 0
  const changes: number[] = [], commits: number[] = []
  const tree = ValueSlider({ value: 10, min: 0, max: 100, step: 1, label: '时间', onChange: (value: number) => changes.push(value),
    onChangeEnd: (value: number) => commits.push(value), ...extra })
  const slider = (tree.props.children[0] as any).props
  slider.children.props.ref.current = { getBoundingClientRect: () => ({ left: 10, width: 100 }) }
  const target = { focus() {}, setPointerCapture() {}, releasePointerCapture() { slider.onLostPointerCapture() } }
  const pointer = (clientX: number) => ({ currentTarget: target, pointerId: 1, isPrimary: true, button: 0, clientX })
  const key = (name: string) => ({ key: name, shiftKey: false, preventDefault() {} })
  return { tree, slider, changes, commits, pointer, key }
}
function text(node: any): string {
  if (Array.isArray(node)) return node.map(text).join(' ')
  return node && typeof node === 'object' ? text(node.props?.children) : String(node ?? '')
}
const drag = setup()
drag.slider.onPointerDown(drag.pointer(25)); drag.slider.onPointerMove(drag.pointer(80))
const beforeRelease = [...drag.commits]
drag.slider.onPointerUp(drag.pointer(95)); drag.slider.onBlur(); drag.slider.onLostPointerCapture()

const canceled = setup()
canceled.slider.onPointerDown(canceled.pointer(40)); canceled.slider.onPointerMove(canceled.pointer(65))
canceled.slider.onPointerCancel(); canceled.slider.onLostPointerCapture(); canceled.slider.onBlur()
canceled.slider.onPointerMove(canceled.pointer(90))

const lost = setup()
lost.slider.onPointerDown(lost.pointer(70)); lost.slider.onLostPointerCapture(); lost.slider.onBlur()

const keyboard = setup()
keyboard.slider.onKeyDown(keyboard.key('ArrowRight')); keyboard.slider.onKeyDown(keyboard.key('ArrowRight'))
const keyboardBefore = [...keyboard.commits]
keyboard.slider.onKeyUp(keyboard.key('ArrowRight')); keyboard.slider.onBlur()

const blurred = setup()
blurred.slider.onKeyDown(blurred.key('End')); blurred.slider.onBlur(); blurred.slider.onKeyUp(blurred.key('End'))

const formatted = setup({ formatValue: (value: number) => `time:${value}` })
const original = setup({ unit: 'ms', onChangeEnd: undefined })
original.slider.onKeyDown(original.key('End'))
const axis = setup({ variant: 'axis', formatValue: (value: number) => `time:${value}` })
axis.slider.onPointerDown(axis.pointer(35)); axis.slider.onPointerMove(axis.pointer(85)); axis.slider.onPointerUp(axis.pointer(110))
axis.slider.onKeyDown(axis.key('Home')); axis.slider.onKeyUp(axis.key('Home'))
axis.slider.onKeyDown(axis.key('End')); axis.slider.onKeyUp(axis.key('End'))
const fixed = setup({ variant: 'axis', min: 0, max: 0, value: 0, disabled: true })
fixed.slider.onPointerDown(fixed.pointer(70)); fixed.slider.onKeyDown(fixed.key('End'))
console.log(JSON.stringify({
  drag: { beforeRelease, changes: drag.changes, commits: drag.commits },
  canceled: { changes: canceled.changes, commits: canceled.commits },
  lost: lost.commits,
  keyboard: { beforeRelease: keyboardBefore, changes: keyboard.changes, commits: keyboard.commits },
  blurred: blurred.commits,
  formatted: { aria: formatted.slider['aria-valuetext'], marks: text(formatted.tree.props.children[1]) },
  original: { aria: original.slider['aria-valuetext'], changes: original.changes },
  axis: { aria: axis.slider['aria-valuetext'], changes: axis.changes, commits: axis.commits, touchNone: axis.slider.className.includes('touch-none'), marks: text(axis.tree.props.children[1]) },
  fixed: { changes: fixed.changes, tabIndex: fixed.slider.tabIndex, thumbLeft: fixed.slider.children.props.children[2].props.style.left },
}))
