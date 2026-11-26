import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { AppSidebar } from './AppSidebar'
import { SIGNAL_CHANNEL } from '@/features/signal/signalData'
import { useDomainStore } from '@/stores/domain'
import type { AccountDashboardItem } from '@/types/api'

test('桌面侧栏收起后通过图标和浮层保留导航，展开后显示分割线', () => {
  const domain = useDomainStore.getInitialState()
  const previous = domain.accounts
  domain.accounts = [{ account_id: 7, name: '信号账户', trade_channel: SIGNAL_CHANNEL, market: 'A股' } as AccountDashboardItem]
  try {
    const render = (desktopCollapsed: boolean) => renderToStaticMarkup(createElement(MemoryRouter, {
      initialEntries: ['/accounts/7/signals'],
    }, createElement(AppSidebar, { desktopCollapsed })))
    const collapsed = render(true)
    expect(collapsed).toContain('--sidebar-width:56px')
    expect(collapsed).toContain('aria-label="所有账户"')
    expect(collapsed).toMatch(/popovertarget="sidebar-account-navigation"/i)
    expect(collapsed).toMatch(/id="sidebar-account-navigation"[^>]*popover="auto"[^>]*>.*?href="\/accounts\/7\/signals"/)
    expect(collapsed).not.toContain('aria-label="调整侧栏宽度"')

    const expanded = render(false)
    expect(expanded).toContain('--sidebar-width:184px')
    expect(expanded).toContain('aria-label="调整侧栏宽度"')
    expect(expanded).not.toContain('popover="auto"')
  } finally {
    domain.accounts = previous
  }
})
