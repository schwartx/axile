import { GlobalErrorContext } from '@/lib/errorPresentation'
import { useDomainStore } from '@/stores/domain'
import { describe, expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { ErrorNotice } from './ErrorNotice'
import { AccountConnectionContext } from '@/lib/accountConnection'
import { ApiError } from '@/lib/api/client'

describe('ErrorNotice', () => {
  it('renders a stable open error slot with a retry action', () => {
    const html = renderToStaticMarkup(createElement(ErrorNotice, {
      intent: 'read', title: '自动执行计划加载失败', error: new Error('CTP 交易前置断线'),
      variant: 'section', onRetry: () => {},
    }))
    expect(html).toContain('grid-rows-[1fr]')
    expect(html).toContain('CTP 交易前置断线')
    expect(html).toContain('重试：自动执行计划加载失败')
    expect(html).toContain('text-warn')
    expect(html).not.toContain('text-bad')
  })

  it('keeps a collapsed inert slot when there is no error', () => {
    const html = renderToStaticMarkup(createElement(ErrorNotice, { intent: 'read', title: '加载失败', error: null }))
    expect(html).toContain('grid-rows-[0fr]')
    expect(html).toContain('inert=""')
  })

  it('账户统一入口收起同源错误，独立渲染仍保留详情和重试', () => {
    const error = new ApiError(503, '渠道断线', { body: { error_scope: 'account_connection' } })
    const notice = createElement(ErrorNotice, { accountId: 7, intent: 'read', title: '目标读取失败', error, onRetry: () => {} })
    expect(renderToStaticMarkup(notice)).toContain('重试：目标读取失败')
    const handled = renderToStaticMarkup(createElement(AccountConnectionContext.Provider, { value: { accountId: 7, handled: true } }, notice))
    expect(handled).toContain('暂不可用')
    expect(handled).not.toContain('text-warn')
    expect(handled).not.toContain('渠道断线')
    expect(handled).not.toContain('查看错误详情')
    expect(handled).not.toContain('重试：')
  })

  it('新鲜度提示保持中性，主动操作始终保留反馈', () => {
    const error = new ApiError(503, '渠道断线', { body: { error_scope: 'account_connection' } })
    const render = (intent: 'read' | 'mutation') => renderToStaticMarkup(createElement(AccountConnectionContext.Provider, { value: { accountId: 7, handled: true } },
      createElement(ErrorNotice, { accountId: 7, intent, title: '保存失败', hasData: true, error, onRetry: () => {} })))
    expect(render('read')).toContain('当前显示上次数据')
    expect(render('read')).not.toContain('text-warn')
    expect(render('mutation')).toContain('保存失败')
    expect(render('mutation')).toContain('role="alert"')
    expect(render('mutation')).toContain('重试：保存失败')
  })
})

it('全局承接后不复活同一次网络错误，独立布局与新操作继续反馈', () => {
  const error = new TypeError('Failed to fetch')
  const render = (global: boolean, value = error) => renderToStaticMarkup(createElement(GlobalErrorContext.Provider, { value: global },
    createElement(ErrorNotice, { intent: 'mutation', networkFeedback: 'global-only', title: '连接失败', error: value })))
  const initial = useDomainStore.getInitialState()
  const previous = initial.accountsError
  try {
    initial.accountsError = error
    expect(render(false)).toContain('查看错误详情')
    expect(render(true)).toContain('grid-rows-[0fr]')
    expect(render(true)).not.toContain('暂不可用')
    initial.accountsError = null
    expect(render(true)).toContain('grid-rows-[0fr]')
    expect(render(false)).toContain('查看错误详情')
    expect(render(true, new TypeError('Failed to fetch'))).toContain('查看错误详情')
  } finally { initial.accountsError = previous }
})
