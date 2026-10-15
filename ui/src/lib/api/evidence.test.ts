import { describe, expect, it } from 'bun:test'
import { ApiError } from './client'
import { getEvidencePackage } from './evidence'

const target = { kind: 'execution', accountId: 7, executionId: 'A' } as const
const options = { profile: 'shareable', includeLogs: false, dates: { from: '', to: '' } } as const

async function withResponse(response: Response, run: (signal: AbortSignal) => Promise<void>) {
  const original = globalThis.fetch
  const controller = new AbortController()
  globalThis.fetch = Object.assign((_input: unknown, init?: RequestInit) => {
    expect(init?.signal).toBe(controller.signal)
    return Promise.resolve(response)
  }, { preconnect: original.preconnect }) as typeof fetch
  try { await run(controller.signal) } finally { globalThis.fetch = original }
}

describe('binary evidence API', () => {
  it('returns ZIP bytes without JSON parsing', async () => {
    const bytes = new Uint8Array([80, 75, 5, 6])
    await withResponse(new Response(bytes, { headers: { 'Content-Type': 'application/zip' } }), async signal => {
      const blob = await getEvidencePackage(target, options, signal)
      expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes)
    })
  })

  it('preserves structured failure details and status for retries', async () => {
    const body = { detail: { code: 'EVIDENCE_LIMIT_EXCEEDED', message: '材料过多，请缩小日期范围。' } }
    await withResponse(new Response(JSON.stringify(body), { status: 413 }), async signal => {
      await getEvidencePackage(target, options, signal).then(() => { throw new Error('expected failure') }, error => {
        expect(error).toBeInstanceOf(ApiError)
        expect(error.status).toBe(413)
        expect(error.code).toBe(body.detail.code)
        expect(error.body).toEqual(body)
        expect(error.message).toBe(body.detail.message)
      })
    })
  })

  it('keeps a proxy error body instead of downloading it as a ZIP', async () => {
    await withResponse(new Response('upstream unavailable', { status: 502 }), async signal => {
      await expect(getEvidencePackage(target, options, signal)).rejects.toMatchObject({ status: 502, rawText: 'upstream unavailable' })
    })
  })

  it('rejects successful HTML and empty ZIP responses', async () => {
    for (const response of [new Response('<html>error</html>', { headers: { 'Content-Type': 'text/html' } }), new Response('', { headers: { 'Content-Type': 'application/zip' } })]) {
      await withResponse(response, async signal => {
        await expect(getEvidencePackage(target, options, signal)).rejects.toMatchObject({ code: 'EVIDENCE_INVALID_RESPONSE' })
      })
    }
  })

  it('rejects a late body even when transport resolves after abort', async () => {
    const original = globalThis.fetch
    const controller = new AbortController()
    const response = new Response('zip', { headers: { 'Content-Type': 'application/zip' } })
    response.blob = async () => { controller.abort(); return new Blob(['zip'], { type: 'application/zip' }) }
    globalThis.fetch = Object.assign(() => Promise.resolve(response), { preconnect: original.preconnect })
    try {
      await expect(getEvidencePackage(target, options, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    } finally { globalThis.fetch = original }
  })
})
