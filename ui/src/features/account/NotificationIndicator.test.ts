import { expect, test } from 'bun:test'
import { notificationIndicator } from './notificationIndicatorModel'
import type { Account } from '@/types/api'

function status(mode: Account['execution_notification_status'], ok: boolean | null, executionId = 'latest') {
  return notificationIndicator({
    account: {
      id: 1,
      execution_notification_status: mode,
      notification_state: {
        last_success_at: 'earlier', execution_id: 'earlier',
        last_attempt_execution_id: executionId, last_attempt_ok: ok,
        last_attempt_at: 'now', last_attempt_error: ok === false ? 'timeout' : null,
      },
    },
    executionId: 'latest',
  })
}

test('notification labels distinguish mode and actual latest result', () => {
  expect(status('none', null).label).toBe('通知未设置')
  expect(status('default', true).label).toBe('')
  expect(status('default', true).result).toBe(true)
  expect(status('function', true).label).toBe('自定义')
  expect(status('default', false).label).toBe('飞书通知发送失败')
  expect(status('function', false).label).toBe('通知函数执行失败')
  expect(status('function', false).detail).toContain('timeout')
})

test('previous failures or successes do not describe a newer execution', () => {
  expect(status('default', true, 'earlier').result).toBeNull()
  expect(status('function', false, 'earlier').result).toBeNull()
  expect(status('default', null).result).toBeNull()
})

test('indicator links to notification editor with accessible icon and no underline', async () => {
  const { createElement } = await import('react')
  const { renderToStaticMarkup } = await import('react-dom/server')
  const { MemoryRouter } = await import('react-router')
  const { NotificationIndicator } = await import('./NotificationIndicator')
  const render = (mode: Account['execution_notification_status'], ok: boolean) => renderToStaticMarkup(
    createElement(MemoryRouter, {}, createElement(NotificationIndicator, {
      account: {
        id: 7, execution_notification_status: mode,
        notification_state: {
          execution_id: 'latest', last_success_at: 'today', last_attempt_execution_id: 'latest',
          last_attempt_ok: ok, last_attempt_error: ok ? null : 'timeout',
        },
      }, executionId: 'latest',
    })),
  )
  const unset = render('none', false)
  expect(unset).toContain('通知未设置')
  expect(unset).not.toContain('<svg')
  expect(unset).toContain('/accounts/7/edit/notification')
  expect(unset).toContain('no-underline')
  const success = render('default', true)
  expect(success).toContain('lucide-bell')
  expect(success).toContain('lucide-check')
  expect(success).toContain('aria-label=')
  expect(render('function', true)).toContain('自定义')
  const failed = render('default', false)
  expect(failed).toContain('lucide-x')
  expect(failed).toContain('飞书通知发送失败')
  expect(failed).toContain('text-warn')
})

test('调仓信号铃铛无配置也可进入独立页面，只显示信号推送结果', async () => {
  const { createElement } = await import('react')
  const { renderToStaticMarkup } = await import('react-dom/server')
  const { MemoryRouter } = await import('react-router')
  const { NotificationIndicator } = await import('./NotificationIndicator')
  const account = {
    id: 7, trade_channel: 'rebalance_signal', execution_notification_status: 'default' as const,
    notification_state: {
      execution_id: null, last_success_at: null, last_attempt_event_type: 'signal.calculated',
      last_attempt_ok: false, last_attempt_at: 'now', last_attempt_error: 'timeout',
    },
  }
  const render = (mode: Account['execution_notification_status']) => renderToStaticMarkup(
    createElement(MemoryRouter, {}, createElement(NotificationIndicator, { account: { ...account, execution_notification_status: mode } })),
  )
  expect(notificationIndicator({ account })).toMatchObject({ result: false })
  expect(notificationIndicator({ account: { ...account, notification_state: {
    ...account.notification_state, last_attempt_event_type: 'execution.finished',
  } } })).toMatchObject({ result: null })
  expect(render('default')).toContain('lucide-x')
  const unset = render('none')
  expect(unset).toContain('lucide-bell')
  expect(unset).toContain('/accounts/7/edit/notification')
  expect(unset).not.toContain('>通知未设置<')
})
