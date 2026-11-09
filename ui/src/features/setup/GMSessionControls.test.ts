import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { defaultGMSessions } from './cron'
import { GMSessionControls } from './GMSessionControls'

test('仅 GM 展示四个阶段，盘后禁用，默认关闭', () => {
  for (const channel of ['ctp', 'tq']) {
    expect(renderToStaticMarkup(createElement(GMSessionControls, { channel, onChange: () => {} }))).toBe('')
  }
  const html = renderToStaticMarkup(createElement(GMSessionControls, { channel: 'gm', onChange: () => {} }))
  expect(html).toContain('早盘集合竞价 · 可撤单阶段')
  expect(html).toContain('盘后定价交易')
  expect(html).toContain('disabled=')
  expect(html).toContain('09:25–09:30')
  expect(html).not.toContain('SINGLE-MAKER')
  expect(html).toContain('撤单受限时保留原委托')
  expect(html).toContain('09:24')
  expect(html).toContain('早盘不可撤单阶段触发时间')
  expect(html).not.toContain('<select')
  expect(html).toContain('role="group" aria-label="早盘不可撤单阶段触发时间"')
  for (const time of ['09:15', '14:57']) expect(html).toContain(`固定 ${time} 触发`)
  expect(html.match(/aria-checked="false"/g)).toHaveLength(4)
  const selected = renderToStaticMarkup(createElement(GMSessionControls, { channel: 'gm', value: { ...defaultGMSessions(), opening_cancelable: { enabled: true, time: '09:15' } }, onChange: () => {} }))
  expect(selected.match(/aria-checked="true"/g)).toHaveLength(1)
})
