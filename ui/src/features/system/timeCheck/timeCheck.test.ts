import { afterEach, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { useTimeCheck } from '@/stores/timeCheck'
import type { TimeCheckSnapshot } from '@/lib/api/timeCheck'
import { ScheduleFields } from '@/features/setup/ScheduleFields'
import { cronToExpr, parseSavedTimer, resolveCronList } from '@/features/setup/cron'
import { offsetText } from './display'

const base: TimeCheckSnapshot = {
  ready: true, status: 'disabled', next_run_at: null, state: {},
  settings: { version: 0, source: 'aliyun', custom_host: '', custom_port: 123, threshold_ms: 500, enabled: false, cron_expr: '0 * * * *' },
}
afterEach(() => useTimeCheck.setState({ snapshot: null, cursor: null, connected: false, error: null }))

test('时间检查共享流拒绝延迟更新，重连全量恢复', () => {
  const store = useTimeCheck.getState()
  store.apply({ stream_id: 'a', version: 2, data: base }, true)
  const warning = { ...base, status: 'warning' as const }
  store.apply({ stream_id: 'a', version: 3, data: warning })
  store.apply({ stream_id: 'a', version: 2, data: base })
  expect(useTimeCheck.getState().snapshot?.status).toBe('warning')
  store.disconnect()
  store.apply({ stream_id: 'b', version: 1, data: base })
  expect(useTimeCheck.getState().snapshot?.status).toBe('warning')
  store.apply({ stream_id: 'b', version: 1, data: base }, true)
  expect(useTimeCheck.getState().snapshot?.status).toBe('disabled')
})

test('时间偏差文案明确快慢及单位', () => {
  expect(offsetText(32)).toBe('本机比参考时间快 32.00 ms')
  expect(offsetText(-32)).toBe('本机比参考时间慢 32.00 ms')
})

test('复用排程字段不暴露交易、夜盘或补发功能', () => {
  const value = parseSavedTimer('continuous', '0 * * * *', null)
  expect(cronToExpr(resolveCronList('continuous', { ...value, supN: 0 }))).toBe('0 * * * *')
  const html = renderToStaticMarkup(createElement(ScheduleFields, { value, patch: () => {}, scheduleKind: 'continuous', rawErr: null }))
  expect(html).toContain('快捷')
  expect(html).toContain('高级')
  expect(html).toContain('自定义')
  expect(html).not.toContain('补发')
  expect(html).not.toContain('交易日历')
  expect(html).not.toContain('自动调仓')
})

test('REST 响应不能覆盖先到达的新 SSE 状态', () => {
  const store = useTimeCheck.getState()
  store.apply({ stream_id: 'a', version: 3, data: { ...base, status: 'warning' } }, true)
  store.accept({ ...base, stream_id: 'a', version: 2 })
  expect(useTimeCheck.getState().snapshot?.status).toBe('warning')
})

test('检查设置复用项目控件，移除原生下拉、数字框和复选框', async () => {
  const { TimeCheckSettingsPanel } = await import('./TimeCheckSettingsPanel')
  const draft = { ...base.settings, source: 'custom' as const, custom_host: 'ntp.example.com', enabled: true }
  const html = renderToStaticMarkup(createElement(TimeCheckSettingsPanel, {
    draft, onChange: () => {}, timer: parseSavedTimer('continuous', draft.cron_expr, null), onTimerChange: () => {},
    rawError: null, nextRun: '2026/10/19 18:00:00', conflict: false, onReload: () => {},
  }))
  expect(html).not.toContain('<select')
  expect(html).not.toContain('type="number"')
  expect(html).not.toContain('type="checkbox"')
  expect(html).toContain('role="switch"')
  expect(html).toContain('aria-haspopup="listbox"')
  expect(html).toContain('允许偏差')
  expect(html).toContain('UDP 端口')
  expect(html).toContain('服务器域名或 IP')
})
