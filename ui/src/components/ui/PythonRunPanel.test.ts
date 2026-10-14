import { describe, expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { PythonRunPanel } from './PythonRunPanel'

describe('PythonRunPanel', () => {
  it('keeps a failed run out of the business result panel', () => {
    const html = renderToStaticMarkup(
      createElement(PythonRunPanel, {
        kind: 'result',
        open: true,
        onToggle: () => {},
        running: false,
        result: { valid: false, errorLine: 12, errorType: 'RuntimeError', errorMessage: 'boom' },
        stale: false,
      }),
    )

    expect(html).toContain('无返回值')
    expect(html).toContain('请查看问题面板')
    expect(html).not.toContain('RuntimeError')
  })

  it('renders execution diagnostics in the problems panel', () => {
    const html = renderToStaticMarkup(
      createElement(PythonRunPanel, {
        kind: 'problems',
        open: true,
        onToggle: () => {},
        running: false,
        result: {
          valid: false,
          errorLine: 12,
          errorType: 'RuntimeError',
          errorMessage: 'boom',
          traceback: 'trace detail',
        },
        stale: false,
      }),
    )

    expect(html).toContain('1 个问题')
    expect(html).toContain('RuntimeError: boom')
    expect(html).toContain('第 12 行')
    expect(html).toContain('查看错误详情')
    expect(html).toContain('text-warn')
    expect(html).not.toContain('text-bad')
  })
})
