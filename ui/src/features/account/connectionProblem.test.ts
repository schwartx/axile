import { describe, expect, test } from 'bun:test'
import { connectionLabel, connectionPresentation, connectionProblem } from './connectionProblem'
import type { AccountConnectionDetail } from '../../types/api'

const base: AccountConnectionDetail = { status: 'error', observed_at: 'confirmed', message: 'SDK 原文\n原始末尾', error: { traceback: '完整调用栈\n最底层' } }

describe('channel supplied connection content', () => {
  test('arbitrary labels and empty values use one fallback', () => {
    for (const label of [undefined, null, '', ' \n\t']) expect(connectionLabel({ ...base, label })).toBe('连接异常')
    expect(connectionLabel({ ...base, label: '  全新插件文案  ' })).toBe('全新插件文案')
    expect(connectionLabel({ ...base, status: 'ok', label: '旧故障' })).toBe('连接正常')
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
    for (const status of ['ok', 'unknown'] as const) {
      const view = connectionProblem(8, { ...base, status, label: '旧故障', presentation: { title: '旧标题', sections: [{ steps: ['旧建议'] }] } })
      expect(view.sections).toEqual([])
      expect(view.technical).toBe('')
      expect(view.copy).not.toContain('旧')
      expect(view.copy).not.toContain(base.message!)
    }
  })
})
