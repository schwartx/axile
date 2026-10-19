import { expect, test } from 'bun:test'
import { terminateExecution } from './executions'

test('终止请求保留用户选择的挂单处理模式', async () => {
  const original = globalThis.fetch
  const calls: { url: unknown; init?: RequestInit }[] = []
  globalThis.fetch = Object.assign((url: unknown, init?: RequestInit) => {
    calls.push({ url, init })
    return Promise.resolve(Response.json({ account_id: 7, message: '已受理终止请求' }, { status: 202 }))
  }, { preconnect: original.preconnect }) as typeof fetch
  try {
    for (const mode of ['graceful', 'cancel_pending'] as const) {
      await expect(terminateExecution(7, { mode })).resolves.toMatchObject({ account_id: 7 })
      const call = calls.at(-1)!
      expect(call.url).toBe('/api/v1/account/7/terminate')
      expect(call.init?.method).toBe('POST')
      expect(JSON.parse(call.init?.body as string)).toEqual({ mode })
    }
  } finally { globalThis.fetch = original }
})
