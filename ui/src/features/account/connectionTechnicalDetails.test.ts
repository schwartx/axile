import { describe, expect, test } from 'bun:test'
import { connectionTechnicalDetails } from './connectionTechnicalDetails'
import { connectionProblem } from './connectionProblem'
import type { AccountConnectionDetail } from '../../types/api'

const message = '交易前置已断开'
const raw = 'CTP 交易前置断线: 4097'
const base: AccountConnectionDetail = { status: 'unavailable', observed_at: null, message, error: {
  message, technical_detail: raw, traceback: `axile.executor.ctp.ctp_execute.CtpSessionRecoveryRequired: ${raw}\n`,
  exception_type: 'CtpSessionRecoveryRequired', error_scope: 'account_connection', reason_code: '4097',
} }

describe('structured connection evidence', () => {
  test('callback summary is not a traceback; duplicate messages disappear while codes stay visible', () => {
    const info = connectionTechnicalDetails(base)!
    expect(info.message).toBe(message)
    expect(info.sections).toEqual([{ key: 'technical_detail', label: '技术说明', value: raw, kind: 'text' }])
    expect(info.fields).toContainEqual({ key: 'reason_code', label: '原因码', value: '4097' })
    expect(info.fields).toContainEqual({ key: 'error_scope', label: '错误范围', value: '账户连接' })
    expect(info.stackNotice).toContain('未包含异常调用帧')
    expect(connectionProblem(7, base).copy).toContain(base.error!.traceback as string)
  })
  test('all stack sources keep their exact text and take precedence over duplicate technical prose', () => {
    const original = 'Traceback (most recent call last):\n  File "sdk.py", line 19, in connect\n    open_socket()\nOSError: root\n'
    const propagation = `${original}\nDuring handling of the above exception, another exception occurred:\n  File "worker.py", line 55\nRuntimeError: wrapper\n`
    const reporting = '  File "spi.py", line 25, in OnFrontDisconnected\n    report()\n'
    const info = connectionTechnicalDetails({ ...base, error: { ...base.error, technical_detail: original,
      traceback: original, propagation_traceback: propagation, reporting_stack: reporting } })!
    expect(info.stackNotice).toBe('')
    expect(info.sections.map(section => section.value)).toEqual([original, propagation, reporting])
    expect(info.sections.map(section => section.label)).toEqual(['原始异常栈', '异常传播栈', '上报现场栈'])
  })
  test('reporting stack alone does not claim an exception traceback; unknown structured evidence survives', () => {
    const info = connectionTechnicalDetails({ ...base, error: { ...base.error,
      traceback_unavailable_reason: '首次上报时未抛出', reporting_stack: '  File "callback.py", line 9\n    report()\n',
      plugin_code: 0, response: { code: 4097, rows: [1, 2] }, return_code: 0, error_id: 0 } })!
    expect(info.stackNotice).toBe('首次上报时未抛出')
    expect(info.fields.filter(field => field.value === '0')).toHaveLength(3)
    expect(info.sections.find(section => section.key === 'response')).toMatchObject({ kind: 'json', value: JSON.stringify({ code: 4097, rows: [1, 2] }, null, 2) })
  })
  test('legacy exception-only text and empty records explain missing frames; recovered results hide all evidence', () => {
    expect(connectionTechnicalDetails({ ...base, error: { traceback: 'SomePluginError: legacy\n' } })!.sections[0]!.label).toBe('异常摘要')
    expect(connectionTechnicalDetails({ ...base, error: null })!.stackNotice).toContain('未包含异常调用帧')
    for (const status of ['ready', 'unknown'] as const) expect(connectionTechnicalDetails({ ...base, status })).toBeNull()
  })
  test('multiline exception messages without file frames do not claim to have a traceback', () => {
    const info = connectionTechnicalDetails({ ...base, error: { traceback: 'RuntimeError: first line\nsecond line\n' } })!
    expect(info.sections[0]!.label).toBe('异常摘要')
    expect(info.stackNotice).toContain('未包含异常调用帧')
  })
})
