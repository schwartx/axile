import { afterEach, beforeEach, expect, test } from 'bun:test'
import { createWasmDownload } from './wasmDownload'

const originalFetch = globalThis.fetch
const originalCaches = globalThis.caches
let files: Map<string, Response>
let writesFail = false
beforeEach(() => {
  files = new Map(); writesFail = false
  globalThis.caches = { open: async () => ({
    match: async (url: string) => files.get(url)?.clone(),
    put: async (url: string, response: Response) => { if (writesFail) throw Error('quota'); files.set(url, response.clone()) },
  }) } as unknown as CacheStorage
})
afterEach(() => { globalThis.fetch = originalFetch; globalThis.caches = originalCaches })

test('checking missing assets never downloads, and reads cannot bypass consent', async () => {
  let requests = 0
  globalThis.fetch = (async () => { requests++; return new Response('abc') }) as unknown as typeof fetch
  const download = createWasmDownload([{ url: '/a-v1', bytes: 3 }])
  await download.check()
  expect(download.getSnapshot().phase).toBe('missing')
  await expect(download.read()).rejects.toThrow('尚未下载')
  expect(requests).toBe(0)
})

test('concurrent callers share one download and combined byte progress covers both editor files', async () => {
  const urls: string[] = [], progress: number[] = []
  globalThis.fetch = (async (url: string) => {
    urls.push(url)
    return new Response(new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(1)); controller.enqueue(new Uint8Array(2)); controller.close() } }))
  }) as unknown as typeof fetch
  const download = createWasmDownload([{ url: '/wasm', bytes: 3 }, { url: '/library', bytes: 3 }])
  download.subscribe(() => { if (download.getSnapshot().phase === 'downloading') progress.push(download.getSnapshot().loaded) })
  const first = download.download(), second = download.download()
  expect(first).toBe(second)
  expect(await first).toBeTrue()
  expect(urls).toEqual(['/wasm', '/library'])
  expect(progress).toEqual([0, 1, 3, 4, 6])
  expect(download.getSnapshot()).toMatchObject({ phase: 'cached', loaded: 6, total: 6 })
  const a = await download.read(), b = await download.read()
  structuredClone(a[0], { transfer: [a[0]!] })
  expect(b[0]!.byteLength).toBe(3)
})

test('a fresh page reuses cached files without fetching; a new version waits for consent', async () => {
  let requests = 0
  globalThis.fetch = (async () => { requests++; return new Response('abc') }) as unknown as typeof fetch
  const asset = [{ url: '/wasm-v1', bytes: 3 }]
  expect(await createWasmDownload(asset).download()).toBeTrue()
  const next = createWasmDownload(asset)
  await next.check(); expect(next.getSnapshot().phase).toBe('cached')
  await next.download(); await next.read()
  expect(requests).toBe(1)
  const update = createWasmDownload([{ url: '/wasm-v2', bytes: 3 }])
  await update.check(); expect(update.getSnapshot().phase).toBe('missing')
  expect(requests).toBe(1)
  files.clear(); await next.check()
  expect(next.getSnapshot().phase).toBe('missing')
})

test('truncation and HTTP failures leave no incomplete cache and can be retried', async () => {
  const download = createWasmDownload([{ url: '/wasm', bytes: 3 }])
  globalThis.fetch = (async () => new Response('ab')) as unknown as typeof fetch
  expect(await download.download()).toBeFalse()
  expect(download.getSnapshot().error).toContain('不完整')
  expect(files.size).toBe(0)
  globalThis.fetch = (async () => new Response('abcd')) as unknown as typeof fetch
  expect(await download.download()).toBeFalse()
  expect(download.getSnapshot().error).toContain('大小')
  expect(files.size).toBe(0)
  globalThis.fetch = (async () => new Response('', { status: 503 })) as unknown as typeof fetch
  expect(await download.download()).toBeFalse()
  expect(download.getSnapshot().error).toContain('503')
  globalThis.fetch = (async () => new Response('abc')) as unknown as typeof fetch
  expect(await download.download()).toBeTrue()
})

test('cancel aborts the network and does not mark the group ready', async () => {
  let aborted = false
  globalThis.fetch = ((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
    init.signal!.addEventListener('abort', () => { aborted = true; reject(Error('aborted')) })
    queueMicrotask(() => download.cancel())
  })) as unknown as typeof fetch
  const download = createWasmDownload([{ url: '/wasm', bytes: 3 }])
  expect(await download.download()).toBeFalse()
  expect(aborted).toBeTrue()
  expect(download.getSnapshot()).toMatchObject({ phase: 'missing', error: null })
  expect(files.size).toBe(0)
})

test('an incomplete resource group reuses complete files but is not enabled before the library succeeds', async () => {
  const urls: string[] = []
  let failed = true
  globalThis.fetch = (async (url: string) => { urls.push(url); return new Response(url === '/library' && failed ? 'a' : 'abc') }) as unknown as typeof fetch
  const download = createWasmDownload([{ url: '/wasm', bytes: 3 }, { url: '/library', bytes: 3 }])
  expect(await download.download()).toBeFalse()
  await expect(download.read()).rejects.toThrow('尚未下载')
  failed = false
  expect(await download.download()).toBeTrue()
  expect(urls).toEqual(['/wasm', '/library', '/library'])
  expect(download.getSnapshot().total).toBe(3)
})

test('storage failure cannot report download success or silently fall back to network on read', async () => {
  writesFail = true
  globalThis.fetch = (async () => new Response('abc')) as unknown as typeof fetch
  const download = createWasmDownload([{ url: '/wasm', bytes: 3 }])
  expect(await download.download()).toBeFalse()
  expect(download.getSnapshot().error).toContain('缓存保存失败')
  expect(files.size).toBe(0)
})

test('a synchronous cache access failure releases the task so retry can succeed', async () => {
  const storage = globalThis.caches
  globalThis.caches = { open() { throw Error('cache blocked') } } as unknown as CacheStorage
  globalThis.fetch = (async () => new Response('abc')) as unknown as typeof fetch
  const download = createWasmDownload([{ url: '/wasm', bytes: 3 }])
  expect(await download.download()).toBeFalse()
  globalThis.caches = storage
  expect(await download.download()).toBeTrue()
})
