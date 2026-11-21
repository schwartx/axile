import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ChartIntervalHighlight } from '@/components/viz/ChartIntervalHighlight'
import { createTimeScale } from '@/features/history/timeScale'

const plot = { left: 12, right: 212, top: 16, bottom: 160 }
const viewport = { start: 100, end: 300 }
const scale = createTimeScale('natural', [100, 300])

test('区间只改变高亮层的水平边界，并支持关闭动效', () => {
  const props = { plot, viewport, scale, start: 100, end: 300 }
  const unset = renderToStaticMarkup(createElement(ChartIntervalHighlight, { ...props, selection: null }))
  expect(unset).toContain('opacity-0')
  expect(unset).toContain('left:0;right:0')

  const selection = { kind: 'interval', start: 140, end: 240 } as const
  const chosen = renderToStaticMarkup(createElement(ChartIntervalHighlight, { ...props, selection, animate: true }))
  expect(chosen).toContain('left:40px;right:60px')
  expect(chosen).toContain('transition-[left,right,opacity]')
  expect(chosen).toContain('motion-reduce:transition-none')
})
