import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MacTimePicker } from '@/components/ui/MacTimePicker'

test('时间范围钳住回显并固定小时，默认仍保留完整时分选择', () => {
  const render = (value: string, bounded = true) => renderToStaticMarkup(createElement(MacTimePicker, {
    value, onChange: () => {}, ...(bounded ? { min: '09:20', max: '09:24' } : {}),
  }))
  for (const [value, minute] of [['08:00', '20'], ['09:22', '22'], ['10:00', '24'], ['', '––']]) {
    const html = render(value)
    expect(html).toMatch(/aria-label="时" disabled=""/)
    expect(html).toContain(`>${minute}</button>`)
  }
  const full = render('23:59', false)
  expect(full).not.toContain('disabled=')
  expect(full).toContain('>23</button>')
  expect(full).toContain('>59</button>')
})
