import { describe, expect, test } from 'bun:test'
import { connectionLabel, connectionPresentation, connectionProblem } from './connectionProblem'
import type { AccountConnectionDetail } from '../../types/api'

const base: AccountConnectionDetail = { status: 'unavailable', observed_at: 'confirmed', message: 'SDK 原文\n原始末尾', error: { traceback: '完整调用栈\n最底层' } }

describe('channel supplied connection content', () => {
  test('resident connection states explain recovery and retain cooldown evidence', () => {
    for (const [status, label] of [['connecting', '连接中…'], ['recovering', '恢复中…'], ['unavailable', '连接异常'], ['disconnected', '已断开']] as const) {
      const view = connectionProblem(7, { ...base, status })
      expect(view.title).toBe(status === 'unavailable' ? '账户会话暂不可用' : label)
      if (status === 'unavailable') expect(view.technical).toContain('最底层')
      else expect(view.technical).toBe('')
    }
  })
  test('arbitrary labels and empty values use one fallback', () => {
    for (const label of [undefined, null, '', ' \n\t']) expect(connectionLabel({ ...base, label })).toBe('连接异常')
    expect(connectionLabel({ ...base, label: '  全新插件文案  ' })).toBe('全新插件文案')
    expect(connectionLabel({ ...base, status: 'ready', label: '旧故障' })).toBe('已连接')
  })
  test('keeps valid blocks and text while ignoring unusable blocks and unknown actions', () => {
    expect(connectionPresentation({ sections: [null, {}, { title: '只有标题' }, { text: '说明', fields: [false, { label: '事实', value: '证据' }], steps: [1, '', '步骤'] }],
      actions: [{ label: '危险操作', intent: 'execute_script' }, { label: '设置', intent: 'open_connection_settings', path: '/arbitrary' }] })).toEqual({
      title: '', summary: '', sections: [{ title: '', text: '说明', fields: [{ label: '事实', value: '证据' }], steps: ['步骤'] }],
      actions: [{ label: '设置', intent: 'open_connection_settings' }],
    })
  })
  test('label and presentation can be supplied independently; copy includes unescaped original evidence', () => {
    expect(connectionProblem(7, { ...base, label: '自定义' }).title).toBe('自定义')
    const view = connectionProblem(7, { ...base, presentation: { title: '独立详情', sections: [{ steps: ['检查一'] }] } })
    expect(view.title).toBe('独立详情')
    expect(view.copy).toContain('检查一')
    expect(view.copy).toContain(base.message!)
    expect(view.copy).toContain(base.error!.traceback as string)
  })
  test('recovered or unknown result never presents stale failure steps or raw errors', () => {
    for (const status of ['ready', 'unknown'] as const) {
      const view = connectionProblem(8, { ...base, status, label: '旧故障', presentation: { title: '旧标题', sections: [{ steps: ['旧建议'] }] } })
      expect(view.sections).toEqual([])
      expect(view.technical).toBe('')
      expect(view.copy).not.toContain('旧')
      expect(view.copy).not.toContain(base.message!)
    }
  })
})


test('current session and historical failure remain independent and copy retains evidence', () => {
  const view = connectionProblem(8, { status: 'ready', observed_at: '2026-10-15T16:00:00', message: null, error: null,
    worker_alive: true, last_success_at: '2026-10-15T16:01:00', last_failure_at: '2026-10-15T15:00:00',
    last_failure: { observed_at: '2026-10-15T15:00:00', message: '历史超时', error: { technical_detail: 'historical native error' } } })
  expect(view.title).toBe('已连接')
  expect(view.summary).not.toContain('历史超时')
  expect(view.technicalInfo).toBeNull()
  expect(view.history?.message).toBe('历史超时')
  expect(view.copy).toContain('historical native error')
  expect(view.facts).toContainEqual({ label: '最近访问成功', value: '2026-10-15 16:01:00' })
  expect(view.facts).toContainEqual({ label: '工作进程', value: '运行中' })
  expect(view.facts.some(fact => fact.label === '最近确认')).toBe(false)
})

test('uninitialized session can show a prior successful access without claiming readiness', () => {
  const view = connectionProblem(8, { status: 'uninitialized', observed_at: null, message: null, error: null, last_success_at: 'yesterday' })
  expect(view.title).toBe('未连接')
  expect(view.summary).toContain('当前服务尚未建立')
  expect(view.facts).toContainEqual({ label: '最近访问成功', value: 'yesterday' })
})

test('display hides redundant facts and rounds times while copy preserves evidence', () => {
  const stamp = '2026-10-15T16:49:37.823783'
  const view = connectionProblem(8, { status: 'uninitialized', observed_at: null, message: null, error: null, last_success_at: stamp, last_failure_at: stamp })
  expect(view.facts.map(field => field.label)).toEqual(['工作进程', '最近访问成功'])
  expect(view.facts[1].value).toBe('2026-10-15 16:49:37')
  expect(view.copy).toContain('账户：8')
  expect(view.copy).toContain('最近访问成功：2026-10-15 16:49:37.823783')
  expect(view.copy).toContain('最近连接失败：2026-10-15 16:49:37.823783')
})
