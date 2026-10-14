import { expect, test } from 'bun:test'
import { getCachedExecuteRecords, getExecuteRecords } from './accounts'

test('precise record filters stay out of the account history cache', async () => {
  const original = globalThis.fetch
  const urls: string[] = []
  globalThis.fetch = ((input: string) => {
    urls.push(input)
    return Promise.resolve(Response.json({ count: input.includes('execution_id=') ? 1 : 602, data: [] }))
  }) as typeof fetch
  try {
    const history = await getExecuteRecords(97007, { limit: 500 })
    const selected = await getExecuteRecords(97007, { limit: 500, execution_id: 'old-execution', record_id: 1 })
    expect(selected.count).toBe(1)
    expect(getCachedExecuteRecords(97007)).toBe(history)
    expect(urls[1]).toContain('execution_id=old-execution&record_id=1')
  } finally { globalThis.fetch = original }
})
