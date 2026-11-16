export const WASM_CACHE = 'axile-wasm-v1'
export interface WasmAsset { url: string; bytes: number }
export interface WasmDownloadState {
  phase: 'checking' | 'missing' | 'downloading' | 'cached' | 'error'
  loaded: number
  total: number
  error: string | null
}

/** Download consent belongs to callers; cache reads never fall back to the network. */
export function createWasmDownload(assets: WasmAsset[]) {
  let state: WasmDownloadState = { phase: 'checking', loaded: 0, total: assets.reduce((sum, file) => sum + file.bytes, 0), error: null }
  const listeners = new Set<() => void>()
  let task: Promise<boolean> | undefined
  let controller: AbortController | undefined
  let revision = 0
  const publish = (patch: Partial<WasmDownloadState>) => { state = { ...state, ...patch }; listeners.forEach(listener => listener()) }
  const open = async () => {
    if (!globalThis.caches) throw Error('组件缓存需要 HTTPS 或 localhost，以及支持缓存的浏览器')
    return caches.open(WASM_CACHE)
  }
  const cached = async (cache: Cache, asset: WasmAsset) => {
    const response = await cache.match(asset.url)
    return response && Number(response.headers.get('Content-Length')) === asset.bytes ? response : undefined
  }
  const check = async () => {
    if (task) return
    const current = revision
    try {
      const cache = await open()
      const found = await Promise.all(assets.map(asset => cached(cache, asset)))
      if (current === revision) publish({ phase: found.every(Boolean) ? 'cached' : 'missing', error: null })
    } catch (cause) { if (current === revision) publish({ phase: 'error', error: String(cause instanceof Error ? cause.message : cause) }) }
  }
  const download = (): Promise<boolean> => {
    if (task) return task
    revision++
    const abort = new AbortController()
    controller = abort
    task = (async () => {
      try {
        const cache = await open()
        const found = await Promise.all(assets.map(asset => cached(cache, asset)))
        const missing = assets.filter((_, index) => !found[index])
        abort.signal.throwIfAborted()
        if (!missing.length) { publish({ phase: 'cached', error: null }); return true }
        publish({ phase: 'downloading', loaded: 0, total: missing.reduce((sum, file) => sum + file.bytes, 0), error: null })
        for (const asset of missing) {
          const response = await fetch(asset.url, { signal: abort.signal })
          if (!response.ok || !response.body) throw Error(`组件下载失败（${response.status}）`)
          const reader = response.body.getReader()
          const bytes = new Uint8Array(asset.bytes)
          let offset = 0
          try {
            for (;;) {
              const { done, value } = await reader.read()
              abort.signal.throwIfAborted()
              if (done) break
              if (offset + value.length > bytes.length) throw Error('组件资源大小与当前版本不一致，请刷新页面')
              bytes.set(value, offset); offset += value.length
              publish({ loaded: state.loaded + value.length })
            }
          } finally { await reader.cancel().catch(() => {}) }
          if (offset !== bytes.length) throw Error('组件下载不完整，请重试')
          abort.signal.throwIfAborted()
          try { await cache.put(asset.url, new Response(bytes, { headers: { 'Content-Length': String(bytes.length) } })) }
          catch { throw Error('组件缓存保存失败，请检查浏览器可用空间后重试') }
        }
        abort.signal.throwIfAborted()
        publish({ phase: 'cached', error: null })
        return true
      } catch (cause) {
        publish({ phase: abort.signal.aborted ? 'missing' : 'error', error: abort.signal.aborted ? null : String(cause instanceof Error ? cause.message : cause) })
        return false
      } finally { task = undefined; controller = undefined }
    })()
    return task
  }
  const read = async () => {
    const cache = await open()
    const responses = await Promise.all(assets.map(asset => cached(cache, asset)))
    if (responses.some(response => !response)) {
      publish({ phase: 'missing', error: null })
      throw Error('本地组件尚未下载或已被浏览器清理，请下载后启用')
    }
    return Promise.all(responses.map(response => response!.arrayBuffer()))
  }
  return {
    bytes: assets.reduce((sum, file) => sum + file.bytes, 0),
    check, download, read,
    cancel: () => controller?.abort(),
    getSnapshot: () => state,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } },
  }
}
