import { expect, test } from 'bun:test'

import type { PortfolioLite } from '@/types/api'
import { createPortfolioCopy } from './portfolios'

test('复制组合只提交已保存的定义，不继承身份或运行状态', async () => {
  const source: PortfolioLite = {
    id: 7,
    name: '原组合',
    market: '期货',
    description: '原说明',
    custom_calc_py_code: 'def calculate_portfolio(context): return {}',
    status: 'active',
    tag: '趋势',
    created_at: '2026-01-01T00:00:00',
    updated_at: '2026-02-01T00:00:00',
  }
  const originalFetch = globalThis.fetch
  const request: { url: string; init: RequestInit } = { url: '', init: {} }
  globalThis.fetch = ((url: string, init: RequestInit) => {
    request.url = url
    request.init = init
    return Promise.resolve(new Response(JSON.stringify({ id: 8, account_id: null }), { status: 201 }))
  }) as typeof fetch

  try {
    await createPortfolioCopy(source, '新组合')
  } finally {
    globalThis.fetch = originalFetch
  }

  expect(request.url).toBe('/api/v1/portfolio/')
  expect(request.init.method).toBe('POST')
  expect(JSON.parse(request.init.body as string)).toEqual({
    name: '新组合',
    market: '期货',
    description: '原说明',
    custom_calc_py_code: source.custom_calc_py_code,
    status: 'active',
    tag: '趋势',
  })
})
