import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { SupplementControls } from '@/features/setup/SupplementControls'

function render(count: number, minutes: number) {
  return renderToStaticMarkup(createElement(SupplementControls, {
    supN: count, supM: minutes, onN: () => {}, onM: () => {},
  }))
}

test('不补时收起间隔，禁用交互并保留原间隔值', () => {
  const html = render(0, 3)
  expect(html).toContain('不补')
  expect(html).toContain('inert=""')
  expect(html).toContain('grid-cols-[0fr]')
  expect(html).toContain('aria-valuetext="3 分" aria-disabled="true"')
  expect(html).toContain('tabindex="-1"')
})

test('已有补发配置显示原次数与间隔', () => {
  const html = render(6, 10)
  expect(html).toContain('aria-valuenow="6"')
  expect(html).toContain('aria-valuetext="6 次"')
  expect(html).toContain('aria-valuenow="10"')
  expect(html).toContain('aria-valuetext="10 分"')
  expect(html).toContain('grid-cols-[1fr]')
  expect(html).not.toContain('inert=""')
})

test('补发可超过四次，最多十次', () => {
  for (const count of [4, 9, 10]) {
    const html = render(count, 1)
    expect(html).toContain('aria-valuemax="10"')
    const nextButton = html.match(/<button[^>]*aria-label="下一档补发次数"[^>]*>/)?.[0]
    expect(nextButton).toBeDefined()
    expect(nextButton!.includes('disabled=""')).toBe(count === 10)
  }
})
