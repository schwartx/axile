import { afterEach, expect, test } from 'bun:test'
import { useDomainStore } from './domain'

const originalFetch = globalThis.fetch
const initial = useDomainStore.getState()

afterEach(() => {
  globalThis.fetch = originalFetch
  useDomainStore.setState(initial)
})

test('轮询等待慢请求完成，再发起下一次刷新', async () => {
  let complete!: (response: Response) => void
  const signals: AbortSignal[] = []
  globalThis.fetch = ((_: Parameters<typeof fetch>[0], init?: RequestInit) => {
    signals.push(init!.signal as AbortSignal)
    return new Promise<Response>((resolve) => { complete = resolve })
  }) as unknown as typeof fetch

  const refresh = useDomainStore.getState().refreshAccounts
  const pending = refresh()
  await refresh({ ifIdle: true })
  await refresh({ ifIdle: true })
  expect(signals).toHaveLength(1)
  expect(signals[0].aborted).toBe(false)
  complete(Response.json({ data: [] }))
  await pending
  expect(useDomainStore.getState().accounts).toEqual([])

  const next = refresh({ ifIdle: true })
  expect(signals).toHaveLength(2)
  complete(Response.json({ data: [] }))
  await next
})

test('显式刷新可以替换在途请求，旧请求结束不清除新的在途状态', async () => {
  const requests: Array<{ signal: AbortSignal, complete: (response: Response) => void }> = []
  globalThis.fetch = ((_: Parameters<typeof fetch>[0], init?: RequestInit) => new Promise<Response>((complete) => {
    requests.push({ signal: init!.signal as AbortSignal, complete })
  })) as unknown as typeof fetch

  const refresh = useDomainStore.getState().refreshAccounts
  const first = refresh()
  const second = refresh()
  expect(requests[0].signal.aborted).toBe(true)
  requests[0].complete(Response.json({ data: [{ account_id: 1 }] }))
  await first
  expect(useDomainStore.getState().accounts).toBeNull()
  await refresh({ ifIdle: true })
  expect(requests).toHaveLength(2)
  requests[1].complete(Response.json({ data: [] }))
  await second
  expect(useDomainStore.getState().accountsError).toBeNull()
})

test('刷新失败释放在途状态，后续轮询可以重试并恢复', async () => {
  globalThis.fetch = (() => Promise.reject(new TypeError('Failed to fetch'))) as unknown as typeof fetch
  const refresh = useDomainStore.getState().refreshAccounts
  await refresh({ ifIdle: true })
  expect(useDomainStore.getState().accountsError).toBeInstanceOf(TypeError)

  globalThis.fetch = (() => Promise.resolve(Response.json({ data: [] }))) as unknown as typeof fetch
  await refresh({ ifIdle: true })
  expect(useDomainStore.getState().accountsError).toBeNull()
  expect(useDomainStore.getState().accounts).toEqual([])
})
