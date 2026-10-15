import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { BackLink } from '@/components/ui/BackLink'
import { AccountPageTitle } from '@/features/account/pageHead'

function renderTitle(path: string, props: Parameters<typeof AccountPageTitle>[0]) {
  const router = createMemoryRouter([
    { path: '*', element: createElement('header', null, createElement(AccountPageTitle, props)) },
  ], { initialEntries: [path] })
  try {
    return renderToStaticMarkup(createElement(RouterProvider, { router }))
  } finally {
    router.dispose()
  }
}

describe('账户页返回导航', () => {
  test('直接打开账户子页仍有独立的账户概览入口', () => {
    const html = renderTitle('/accounts/7/edit/algorithm', { accountId: 7, page: '子页面', name: '24/7' })
    expect(html.match(/<a [^>]+>/)?.[0]).toContain('href="/accounts/7"')
    expect(html.match(/<a [^>]+>/)?.[0]).toContain('aria-label="返回账户概览"')
    expect(html).toContain('子页面 · ')
    expect(html).toContain('24/7')
    // 返回按钮与账户名各有自己的可访问链接，图标不混入标题语义。
    expect(html.match(/<a /g)).toHaveLength(2)
    expect(html.indexOf('aria-label="返回账户概览"')).toBeLessThan(html.indexOf('<h1'))
  })

  test('账户名尚未加载时也能返回概览', () => {
    const html = renderTitle('/accounts/7/holdings', { accountId: 7, page: '持仓明细' })
    expect(html).toContain('aria-label="返回账户概览"')
    expect(html).toContain('账户 #7')
  })

  test('执行详情返回所属记录列表并保留筛选参数', () => {
    const html = renderTitle('/accounts/7/executions/exec-1', {
      accountId: 7, page: '执行详情', flip: false,
      back: { to: '/accounts/7/executions?status=failed&skip=20', label: '返回执行记录' },
    })
    expect(html.match(/<a [^>]+>/)?.[0]).toContain('href="/accounts/7/executions?status=failed&amp;skip=20"')
    expect(html.match(/<a [^>]+>/)?.[0]).toContain('aria-label="返回执行记录"')
  })

  test('从绩效进入的执行详情可以返回绩效页', () => {
    const html = renderTitle('/accounts/7/executions/exec-1', {
      accountId: 7, page: '执行详情', flip: false,
      back: { to: '/accounts/7/history', label: '返回实盘绩效' },
    })
    expect(html.match(/<a [^>]+>/)?.[0]).toContain('href="/accounts/7/history"')
    expect(html.match(/<a [^>]+>/)?.[0]).toContain('aria-label="返回实盘绩效"')
  })

  test('账户概览返回所有账户，直达时不依赖浏览历史', () => {
    const router = createMemoryRouter([
      { path: '*', element: createElement(BackLink, { to: '/', label: '返回所有账户' }) },
    ], { initialEntries: ['/accounts/7'] })
    try {
      const html = renderToStaticMarkup(createElement(RouterProvider, { router }))
      expect(html.match(/<a [^>]+>/)?.[0]).toContain('href="/"')
      expect(html.match(/<a [^>]+>/)?.[0]).toContain('aria-label="返回所有账户"')
    } finally {
      router.dispose()
    }
  })
})
