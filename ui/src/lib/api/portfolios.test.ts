import { afterEach, expect, test } from 'bun:test'

import type { Portfolio } from '@/types/api'
import { createPortfolioCopy } from './portfolios'

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })

test('复制前读取最新定义，只提交配置字段，不继承身份或绑定', async () => {
  const source: Portfolio = {
    id: 7,
    name: '原组合',
    market: '期货',
    description: '最新说明',
    custom_calc_py_code: 'def calculate_portfolio(context): return {"rb2610": 0.5}',
    status: 'active',
    tag: '趋势',
    account_id: 3,
    created_at: '2026-01-01T00:00:00',
    updated_at: '2026-11-09T12:00:00',
  }
  const created = { ...source, id: 8, name: '新组合', account_id: null }
  const requests: { url: string; init?: RequestInit }[] = []
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    requests.push({ url, init })
    return Promise.resolve(new Response(JSON.stringify(requests.length === 1 ? source : created), {
      status: requests.length === 1 ? 200 : 201,
    }))
  }) as typeof fetch

  expect(await createPortfolioCopy(7, '新组合')).toEqual(created)
  expect(requests.map(({ url, init }) => [url, init?.method ?? 'GET'])).toEqual([
    ['/api/v1/portfolio/7', 'GET'],
    ['/api/v1/portfolio/', 'POST'],
  ])
  expect(JSON.parse(requests[1]!.init!.body as string)).toEqual({
    name: '新组合',
    market: source.market,
    description: source.description,
    custom_calc_py_code: source.custom_calc_py_code,
    status: source.status,
    tag: source.tag,
  })
})

test('原组合读取失败时不创建副本，也不退回旧定义', async () => {
  const urls: string[] = []
  globalThis.fetch = ((url: string) => {
    urls.push(url)
    return Promise.resolve(new Response(JSON.stringify({ detail: '组合不存在' }), { status: 404 }))
  }) as typeof fetch

  await expect(createPortfolioCopy(7, '新组合')).rejects.toMatchObject({ status: 404, message: '组合不存在' })
  expect(urls).toEqual(['/api/v1/portfolio/7'])
})

test('创建失败向弹窗传播错误，不报告复制成功', async () => {
  globalThis.fetch = ((url: string) => Promise.resolve(
    url.endsWith('/7')
      ? new Response(JSON.stringify({ market: '期货', custom_calc_py_code: 'def calculate_portfolio(context): return {}' }))
      : new Response(JSON.stringify({ detail: '保存失败' }), { status: 500 }),
  )) as typeof fetch

  await expect(createPortfolioCopy(7, '新组合')).rejects.toMatchObject({ status: 500, message: '保存失败' })
})
