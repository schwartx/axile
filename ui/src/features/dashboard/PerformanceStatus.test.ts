import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { PerformanceStatus } from '@/features/dashboard/PerformanceStatus'
import type { PerformanceSummary } from '@/types/api'

const render = (status?: PerformanceSummary['status']) => renderToStaticMarkup(createElement(PerformanceStatus, { status }))

test('仅等待更新时旋转，并保留减少动态效果和键盘可读提示', () => {
  const html = render('pending')
  expect(html).toContain('animate-spin motion-reduce:animate-none')
  expect(html).toContain('aria-label="历史绩效等待更新"')
  expect(html).toContain('tabindex="0"')
  expect(html).not.toContain('<button')
})

test('过期和失败不伪装为计算中，失败使用独立警示形状', () => {
  expect(render('stale')).toContain('aria-label="历史绩效待更新"')
  expect(render('failed')).toContain('aria-label="历史绩效更新失败"')
  expect(render('failed')).toContain('lucide-triangle-alert')
  for (const status of ['stale', 'failed', 'empty', 'ready'] as const) {
    expect(render(status)).not.toContain('animate-spin')
  }
})

test('正常状态不留图标或焦点，但各状态保留固定槽位', () => {
  expect(render('ready')).not.toContain('<svg')
  expect(render('ready')).not.toContain('tabindex')
  expect(render()).toContain('aria-label="暂无历史绩效"')
  for (const status of ['pending', 'stale', 'failed', 'empty', 'ready'] as const) {
    expect(render(status)).toContain('inline-grid h-4 w-4 shrink-0')
  }
})
