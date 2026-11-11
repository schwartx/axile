import { afterEach, describe, expect, it, test } from 'bun:test'

import { initStatus, peekInitValues, saveBrowserTitle, saveExecutionAlert, initSavePayload, initValuesFromStatus } from './init'

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
})

describe('saveExecutionAlert', () => {
  it('使用 PATCH 保存并同步更新配置缓存', async () => {
    const requests: Array<{ input: string; init?: RequestInit }> = []
    const responses = [
      {
        configured: true,
        environment: 'local',
        values: {
          sqlalchemy_database_configured: true,
          exe_err_feishu_configured: true,
          system_execution_notification_mode: 'default',
          system_execution_notification_code: '',
          environment: 'local',
          app_log_dir: './logs',
          axile_log_rotation: '1 day',
          algorithm_modules: [],
          algorithm_directories: [],
        },
      },
      { ok: true, message: '执行告警配置已保存并立即生效。' },
    ]
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      requests.push({ input: String(input), init })
      return Response.json(responses.shift())
    }) as typeof fetch

    await initStatus()
    const result = await saveExecutionAlert('new-key', 'default', '')

    expect(result.ok).toBeTrue()
    expect(requests[1]?.input).toBe('/api/v1/init/execution-alert')
    expect(requests[1]?.init?.method).toBe('PATCH')
    expect(requests[1]?.init?.body).toBe(JSON.stringify({ exe_err_feishu_key: 'new-key', system_execution_notification_mode: 'default', system_execution_notification_code: '' }))
    expect(peekInitValues()?.exe_err_feishu_key).toBe('')
    expect(peekInitValues()?.exe_err_feishu_configured).toBeTrue()
  })
})

const values = initValuesFromStatus({ sqlalchemy_database_configured: true, exe_err_feishu_configured: true, system_execution_notification_mode: 'default', system_execution_notification_code: '', environment: 'local', app_log_dir: './logs', axile_log_rotation: '1 day', algorithm_modules: [], algorithm_directories: [] })
test('仅修改高级配置不提交隐藏凭证或默认数据库', () => {
  const payload = JSON.parse(JSON.stringify(initSavePayload({ ...values, app_log_dir: './new-logs' }, true)))
  expect(payload.app_log_dir).toBe('./new-logs')
  expect(payload).not.toHaveProperty('sqlalchemy_database_uri')
  expect(payload).not.toHaveProperty('exe_err_feishu_key')
})
test('新数据库地址可以替换，首启仍提交完整配置', () => {
  expect(initSavePayload({ ...values, sqlalchemy_database_uri: 'sqlite+aiosqlite:///new.db' }, true).sqlalchemy_database_uri).toBe('sqlite+aiosqlite:///new.db')
  expect(initSavePayload(values, false).exe_err_feishu_key).toBe('')
})

test('保存默认模式仍保留自定义源码，缓存不留存 Key', async () => {
  globalThis.fetch = (async () => Response.json({ ok: true, message: 'saved' })) as unknown as typeof fetch
  await saveExecutionAlert('secret', 'default', 'def notify(context): pass')
  expect(peekInitValues()?.system_execution_notification_code).toBe('def notify(context): pass')
  expect(peekInitValues()?.exe_err_feishu_key).toBe('')
})
test('高级设置不提交系统告警或凭据状态字段', () => {
  const payload = JSON.parse(JSON.stringify(initSavePayload(values, true)))
  expect(payload).not.toHaveProperty('system_execution_notification_mode')
  expect(payload).not.toHaveProperty('system_execution_notification_code')
  expect(payload).not.toHaveProperty('exe_err_feishu_configured')
  expect(initSavePayload({ ...values, browser_title: '旧标题' }, true)).not.toHaveProperty('browser_title')
})

test('标题独立 PATCH 保存并更新缓存，清空可恢复默认', async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = []
  const responses = [
    { configured: true, environment: 'local', values: { ...values, browser_title: '旧标题' } },
    { ok: true, message: 'saved' },
    { ok: true, message: 'saved' },
  ]
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ input: String(input), init })
    return Response.json(responses.shift())
  }) as typeof fetch
  await initStatus()
  expect(peekInitValues()?.browser_title).toBe('旧标题')
  await saveBrowserTitle('  实盘 · axile  ')
  expect(peekInitValues()?.browser_title).toBe('实盘 · axile')
  expect(requests[1]?.input).toBe('/api/v1/init/browser-title')
  expect(requests[1]?.init?.method).toBe('PATCH')
  expect(requests[1]?.init?.body).toBe(JSON.stringify({ browser_title: '实盘 · axile' }))
  await saveBrowserTitle('  ')
  expect(peekInitValues()?.browser_title).toBe('')
  expect(requests[2]?.init?.body).toBe(JSON.stringify({ browser_title: '' }))
})

test('标题写入失败不更新缓存', async () => {
  const responses = [
    Response.json({ configured: true, environment: 'local', values: { ...values, browser_title: '原有标题' } }),
    Response.json({ detail: 'disk full' }, { status: 500 }),
  ]
  globalThis.fetch = (async () => responses.shift()!) as unknown as typeof fetch
  await initStatus()
  await expect(saveBrowserTitle('新标题')).rejects.toThrow('disk full')
  expect(peekInitValues()?.browser_title).toBe('原有标题')
})
