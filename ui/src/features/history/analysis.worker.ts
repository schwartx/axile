import init, { open_analysis, type AnalysisDatabase } from '@/generated/analysis/axile_browser'
import wasmUrl from '@/generated/analysis/axile_browser_bg.wasm?url'
import { apiGet, apiSend, apiPostText, ApiError } from '@/lib/api/client'
import type { AnalysisMetadata } from '@/lib/api/performance'

interface FactState { source_id: string; cursor: string; settings: unknown; trade_channel: string }
interface Job { generation: number; phase: string; remote: FactState; cursor: unknown }
interface Request { id: number; accountId: number; command: string; query: Record<string, unknown> }
const initialized = () => init({ module_or_path: wasmUrl })
let engine: ReturnType<typeof initialized> | undefined
const running = new Map<number, AbortController>()
const failures = new Map<number, { source: string; error: string }>()
const sourceKey = (state: FactState) => JSON.stringify(state)
const pathOf = (account: number) => `/account/performance/${account}`
async function digest(value: string) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))), byte => byte.toString(16).padStart(2, '0')).join('')
}

async function database<T>(account: number, action: (db: AnalysisDatabase) => T): Promise<T> {
  if (!globalThis.isSecureContext || !navigator.storage?.getDirectory || !navigator.locks) throw new Error('浏览器分析需要 HTTPS（或 localhost），以及支持本地文件存储的浏览器')
  await (engine ??= initialized())
  const key = await digest(`${location.origin}:${account}`)
  // Only a bounded local operation holds the lock; network requests never block cached reads.
  return navigator.locks.request('axile-analysis', async () => {
    const db = await open_analysis(key, BigInt(account), 'browser-ledger-2')
    try { return action(db) } finally { try { db.close() } finally { db.free() } }
  })
}
const requestSignal = (controller: AbortController) => AbortSignal.any([controller.signal, AbortSignal.timeout(30000)])
const sameJob = (a: Job | null, b: Job) => a?.generation === b.generation && a.phase === b.phase && JSON.stringify(a.cursor) === JSON.stringify(b.cursor)

async function synchronize(account: number, controller: AbortController, source: string) {
  const path = pathOf(account)
  let first = true
  try {
    for (;;) {
      controller.signal.throwIfAborted()
      let job: Job | null = await database(account, db => {
        const current: Job | null = JSON.parse(db.job())
        if (!first && current?.phase === 'calculate') {
          db.calculate(new Date().toISOString())
          return JSON.parse(db.job())
        }
        return current
      })
      if (!job || first) {
        const state = await apiSend<FactState>('POST', `${path}/facts-prepare`, undefined, requestSignal(controller))
        source = sourceKey(state)
        job = await database(account, db => {
          if (JSON.parse(db.metadata(JSON.stringify(state))).status === 'ready') return null
          return JSON.parse(db.begin(JSON.stringify(state)))
        })
        if (!job) return
        first = false
      }
      const current = job
      source = sourceKey(current.remote)
      if (current.phase === 'sync') {
        const page = await apiPostText(`${path}/facts`, { source_id: current.remote.source_id, until: current.remote.cursor, cursor: current.cursor }, requestSignal(controller))
        controller.signal.throwIfAborted()
        // Another tab may have advanced the same durable job while this page was in flight.
        await database(account, db => { if (sameJob(JSON.parse(db.job()), current)) db.page(page) })
      }
      await new Promise(resolve => setTimeout(resolve, 0))
    }
  } catch (error) {
    if (!controller.signal.aborted) failures.set(account, { source, error: error instanceof Error ? error.message : String(error) })
  } finally {
    if (running.get(account) === controller) running.delete(account)
  }
}

async function handle({ accountId, command, query }: Request) {
  if (!Number.isSafeInteger(accountId) || accountId <= 0) throw new Error('账户编号无效')
  if (command === 'status' || command === 'refresh') {
    const state = await apiGet<FactState>(`${pathOf(accountId)}/facts-state`, AbortSignal.timeout(30000))
    const metadata: AnalysisMetadata = await database(accountId, db => JSON.parse(db.metadata(JSON.stringify(state))))
    if (command === 'refresh' || failures.get(accountId)?.source !== sourceKey(state)) failures.delete(accountId)
    const failure = failures.get(accountId)
    if (failure) return { ...metadata, status: 'failed', retry_at: null, error: failure.error }
    if (metadata.status !== 'ready' && !running.has(accountId)) {
      const controller = new AbortController()
      running.set(accountId, controller)
      void synchronize(accountId, controller, sourceKey(state))
    }
    return metadata
  }
  if (command === 'costs') {
    const { cursor: _cursor, limit: _limit, ...filters } = query
    query.cursor_scope = await digest(JSON.stringify(Object.entries(filters).sort(([a], [b]) => a.localeCompare(b))))
  }
  return database(accountId, db => JSON.parse(db.query(JSON.stringify({ ...query, command }))))
}

globalThis.onmessage = ({ data }: MessageEvent<Request>) => {
  if (data.command === 'pause') { running.get(data.accountId)?.abort(); return }
  void handle(data).then(value => globalThis.postMessage({ id: data.id, value })).catch(error => {
    const message = error instanceof Error ? error.message : String(error)
    globalThis.postMessage({ id: data.id, error: { message, status: message.includes('SNAPSHOT_EXPIRED') ? 410 : error instanceof ApiError ? error.status : 503, code: error instanceof ApiError ? error.code : null } })
  })
}
