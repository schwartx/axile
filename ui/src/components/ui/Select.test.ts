import { expect, test } from 'bun:test'

test('分组说明只显示一次，搜索后保留，且不占用键盘选项索引', () => {
  const result = Bun.spawnSync([process.execPath, '--eval', `
    import { mock, expect } from 'bun:test'
    import * as React from 'react'
    import { renderToStaticMarkup } from 'react-dom/server'

    const useState = React.useState
    let hookIndex = 0
    let query = ''
    mock.module('react', () => ({ ...React, useState(initial) {
      const index = hookIndex++
      return useState(index < 2 ? true : index === 2
        ? { left: 0, minWidth: 300, maxHeight: 320, placement: 'down', anchorBottom: 0 }
        : index === 3 ? query : initial)
    } }))
    mock.module('react-dom', () => ({ createPortal: (children) => children }))
    globalThis.document = { body: {} }
    const { Select } = await import(${JSON.stringify(`${import.meta.dir}/Select.tsx`)})
    const groupLabel = '真实持仓与行情 · 主动交易会产生真实委托'
    const options = [
      { value: null, label: '样例数据', description: '不连接真实渠道' },
      { value: 1, label: 'alpha', groupLabel },
      { value: 2, label: 'beta', groupLabel },
    ]
    function render(items = options) {
      hookIndex = 0
      return renderToStaticMarkup(React.createElement(Select, {
        value: null, options: items, onChange() {}, searchable: true,
      }))
    }
    const html = render()
    expect(html.match(/role="presentation"/g)).toHaveLength(1)
    expect(html.match(/role="option"/g)).toHaveLength(3)
    expect(html).toContain('aria-description="' + groupLabel + '"')
    expect(html).toContain('-opt-2')
    query = 'beta'
    const filtered = render()
    expect(filtered.match(/role="presentation"/g)).toHaveLength(1)
    expect(filtered.match(/role="option"/g)).toHaveLength(1)
    expect(filtered).toContain('-opt-0')
    query = ''
    expect(render([{ value: 1, label: '未分组' }])).not.toContain('role="presentation"')
  `], { cwd: process.cwd(), stdout: 'pipe', stderr: 'pipe' })
  expect(result.exitCode, new TextDecoder().decode(result.stderr)).toBe(0)
})
