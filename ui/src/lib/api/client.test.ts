import { describe, expect, it } from 'bun:test'

import { ApiError, apiErrorFromBody, apiGet, apiUpload } from './client'

const LEGACY_PASSWORD_HEADER = ['x', 'api', 'password'].join('-')

describe('apiErrorFromBody', () => {
  it('preserves complete structured diagnostics', () => {
    const error = apiErrorFromBody(409, 'Conflict', {
      message: '账户正在执行', code: 'EXECUTION_RUNNING', request_id: 'req-1',
      details: { password: 'must-not-be-retained' },
    })
    expect(error).toBeInstanceOf(ApiError)
    expect(error.message).toBe('账户正在执行')
    expect(error.code).toBe('EXECUTION_RUNNING')
    expect(error.requestId).toBe('req-1')
    expect(JSON.stringify(error)).toContain('must-not-be-retained')
  })

  it('formats every validation issue and retains input', () => {
    const error = apiErrorFromBody(422, 'Unprocessable Entity', {
      detail: [{ loc: ['body', 'cron_expr'], msg: 'Field required', input: 'secret-value' }, { loc: ['body', 'token'], msg: 'Invalid token', input: 'abc' }],
      body: { api_key: 'secret-value' },
    })
    expect(error.message).toBe('cron_expr：Field required\ntoken：Invalid token')
    expect(error.validationIssues).toHaveLength(2)
    expect(JSON.stringify(error)).toContain('secret-value')
  })

  it('retains object detail, metadata and an unmodified non-JSON body', () => {
    const body = { detail: { code: 'SNAPSHOT_EXPIRED', message: '快照版本已失效', traceback: 'line 1\nline 2' } }
    const error = apiErrorFromBody(410, 'Gone', body)
    expect(error.message).toBe('快照版本已失效')
    expect(error.code).toBe('SNAPSHOT_EXPIRED')
    expect(error.body).toEqual(body)
    expect(apiErrorFromBody(500, 'Error', null, 'proxy\nroot cause').message).toBe('proxy\nroot cause')
  })

  it('falls back to an HTTP label when the response is not JSON', () => {
    expect(apiErrorFromBody(503, '', null).message).toBe('HTTP 503')
  })
})

describe('API requests', () => {
  it('does not send a password header for JSON requests', async () => {
    const originalFetch = globalThis.fetch
    let request: RequestInit | undefined
    globalThis.fetch = ((_input: string, init?: RequestInit) => {
      request = init
      return Promise.resolve(new Response('{}'))
    }) as typeof fetch

    try {
      await apiGet('/status')
    } finally {
      globalThis.fetch = originalFetch
    }

    expect(new Headers(request?.headers).has(LEGACY_PASSWORD_HEADER)).toBe(false)
  })

  it('does not send a password header for multipart uploads', async () => {
    const originalFetch = globalThis.fetch
    let request: RequestInit | undefined
    globalThis.fetch = ((_input: string, init?: RequestInit) => {
      request = init
      return Promise.resolve(new Response('{}'))
    }) as typeof fetch

    try {
      await apiUpload('/upload', new File(['test'], 'test.txt'))
    } finally {
      globalThis.fetch = originalFetch
    }

    expect(new Headers(request?.headers).has(LEGACY_PASSWORD_HEADER)).toBe(false)
  })
})

it('retains the non-JSON response text and request header after reading the body once', async () => {
  const original = globalThis.fetch
  const raw = '<html>proxy failure\npassword=synthetic\n尾部根因</html>'
  globalThis.fetch = Object.assign(() => Promise.resolve(new Response(raw, { status: 502, headers: { 'x-request-id': 'proxy-97' } })), { preconnect: original.preconnect })
  try {
    await apiGet('/synthetic').then(() => { throw new Error('expected failure') }, error => {
      expect(error).toBeInstanceOf(ApiError)
      expect(error.rawText).toBe(raw)
      expect(error.message).toBe(raw)
      expect(error.requestId).toBe('proxy-97')
    })
  } finally { globalThis.fetch = original }
})
