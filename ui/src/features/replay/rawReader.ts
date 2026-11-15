import { ApiError, apiSend, apiErrorFromBody } from '@/lib/api/client'
import type { RawBlockInfo, RawBusiness, RawCursor, RawFrame, RawManifest, RawPlan, RawPreview, RawSession, RawSubject, RawTarget } from './raw'
import { RawScopes } from './rawScopes'

const delay = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  const canceled = () => { clearTimeout(timer); reject(Error('REPLAY_CLOSED')) }
  const timer = setTimeout(() => { signal?.removeEventListener('abort', canceled); resolve() }, ms)
  signal?.addEventListener('abort', canceled, { once: true })
  if (signal?.aborted) canceled()
})
const digest = async (value: unknown) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(value))))].map(b => b.toString(16).padStart(2, '0')).join('')

export class RawReader {
  private worker: Worker | null = null
  private workerId = 0
  private root = 0
  private logical = 0
  private workerLogical = 0
  private profile: 'point' | 'interval' = 'point'
  private account = 0
  private session: RawSession | null = null
  private catalog: RawManifest | null = null
  private latest: { target: RawTarget; revision: number } | null = null
  private revision = 0
  private running = false
  private closed = false
  private abort: AbortController | null = null
  private pending: { id: number; reject: (cause: Error) => void } | null = null
  private bytes = 0
  private offsetBytes = 0
  private wasmBytes = 0
  private cacheBytes = 0
  private renewal: ReturnType<typeof setInterval> | null = null
  private attempt = 1
  private started = 0
  private spent = { bytes: 0, index_steps: 0 }
  private known = { bytes: 0, index_steps: 0 }
  private estimated = false
  private results = new RawScopes()
  private dependencyPlan: { raw_bytes: number; index_step_limit: number; member_lookups: number; folds: number } | undefined

  private beginRoot() {
    this.root++; this.logical++; this.attempt = 1; this.started = performance.now()
    this.spent = { bytes: 0, index_steps: 0 }
    this.known = { bytes: 0, index_steps: 0 }
    this.dependencyPlan = undefined
  }
  private usage(response: Response) {
    const header = response.headers.get('X-Replay-Usage')
    if (!header) return false
    const value = JSON.parse(header)
    if (![value.bytes, value.index_steps, value.session_bytes].every(n => Number.isSafeInteger(n) && n >= 0) || value.bytes < this.known.bytes || value.index_steps < this.known.index_steps) throw Error('REPLAY_USAGE_RECEIPT')
    this.known = this.spent = { bytes: value.bytes, index_steps: value.index_steps }; this.bytes = this.offsetBytes + value.session_bytes
    return true
  }
  private reserveLost(bytes: number) {
    this.spent = { bytes: this.spent.bytes + bytes, index_steps: this.spent.index_steps + 200_000 }; this.bytes += bytes
  }
  private async recover(signal?: AbortSignal) {
    if (this.attempt >= 3 || !this.session?.session_proof) throw Error('REPLAY_RECOVERY_LIMIT')
    const old = this.session
    const control = (operation: string) => apiSend<any>('POST', this.path('control'), { ...old, root_seq: this.root, attempt: this.attempt, operation, profile: this.profile }, AbortSignal.timeout(1500))
    try {
      await control('cancel')
      const until = performance.now() + 4000
      for (;;) {
        const status = await control('status')
        if (status.state === 'stopped' && !status.estimated) {
          this.known = this.spent = { bytes: status.bytes, index_steps: status.index_steps }; this.bytes = this.offsetBytes + status.session_bytes
          this.attempt++; return
        }
        if (status.state === 'retired' || status.state === 'settled') {
          if (typeof status.bytes === 'number') { this.known = this.spent = { bytes: status.bytes, index_steps: status.index_steps }; this.bytes = this.offsetBytes + status.session_bytes }
          break
        }
        if (performance.now() >= until) throw Error('REPLAY_RECOVERY_STOPPING')
        await delay(25, signal)
      }
    } catch (cause) {
      if (signal?.aborted) throw cause
      if (cause instanceof ApiError && !['REPLAY_READER_EPOCH', 'REPLAY_READER_SESSION', 'REPLAY_READER_EXPIRED', 'REPLAY_STOP_PROOF'].includes(cause.code ?? cause.message)) throw cause
      if (cause instanceof Error && cause.message === 'REPLAY_RECOVERY_STOPPING') throw cause
    }
    const session = await apiSend<RawSession>('POST', this.path('session'), { resume: { ...old, root_seq: this.root, attempt: this.attempt + 1, profile: this.profile,
      elapsed_ms: performance.now() - this.started, spent_bytes: this.spent.bytes, spent_steps: this.spent.index_steps, total_bytes: this.bytes } }, AbortSignal.timeout(5000))
    this.session = session; this.offsetBytes = 0; this.root = 1; this.attempt++; this.estimated = true
    if (session.recovery_wait_seconds > 0) await delay(Math.ceil(session.recovery_wait_seconds * 1000), signal)
    if (performance.now() - this.started >= (this.profile === 'interval' ? 120_000 : 60_000)) throw Error('REPLAY_ROOT_DEADLINE')
  }

  constructor(private execution: string, private publish: (frame: RawFrame) => void, private fail: (cause: Error) => void) {}

  private path(operation: string) { return `/account/executions/${encodeURIComponent(this.execution)}/replay/raw/${operation}` }
  private remember(manifest: RawManifest) {
    if (manifest.schema_version !== 2 || manifest.execution_id !== this.execution || !Number.isSafeInteger(manifest.account_id) || manifest.account_id <= 0 || this.account > 0 && manifest.account_id !== this.account || manifest.recordings.length > 64) throw Error('REPLAY_MANIFEST_SCHEMA')
    this.catalog = manifest
    return manifest
  }
  private resolveTarget(target: RawTarget): RawTarget {
    const generation=this.catalog?.index_generation ?? target.generation
    let resolved=target
    if (generation !== target.generation) {
      const recording=this.catalog!.recordings.find(r=>r.id===target.recording.id), source=target.recording.streams.find(s=>s.id===target.stream)
      const stream=recording?.streams.find(s=>s.topic===source?.topic && s.symbol===source?.symbol)
      if (!recording || !stream && target.stream!==0) throw Error('REPLAY_SOURCE_VIEW')
      resolved={...target,generation,recording:{...recording,seq:target.recording.seq,commit_id:target.recording.commit_id,watermark:target.recording.watermark},stream:stream?.id??0}
    }
    if (resolved.business) {
      const valid=(resolved.business.cursor_generation ?? target.generation)===generation
      resolved={...resolved,business:{...resolved.business,after:valid?resolved.business.after:[0,0],cursor_generation:generation}}
    }
    return resolved
  }
  private async data(operation: string, body: Record<string, unknown>, signal?: AbortSignal) {
    const until = performance.now() + 12_000
    for (;;) {
      let received = false
      try {
        const value = await apiSend('POST', this.path(operation), { ...this.session, root_seq: this.root, attempt: this.attempt, profile: this.profile, ...body },
          signal ? AbortSignal.any([signal, AbortSignal.timeout(5000)]) : AbortSignal.timeout(5000), response => { received = response.ok && this.usage(response) })
        if (!received && operation !== 'control') this.reserveLost(256 * 1024)
        return value
      } catch (cause) {
        const rate = cause instanceof ApiError && cause.status === 429
        if (!received && operation !== 'control' && !rate) this.reserveLost(256 * 1024)
        if (!signal?.aborted && (operation !== 'control' || body.operation === 'renew') && (cause instanceof TypeError || cause instanceof DOMException || cause instanceof ApiError && ['REPLAY_READER_EPOCH', 'REPLAY_READER_SESSION', 'REPLAY_READER_EXPIRED', 'REPLAY_STOP_PROOF'].includes(cause.code ?? cause.message))) {
          await this.recover(signal); continue
        }
        if (!(cause instanceof ApiError) || cause.status !== 429 || !['REPLAY_READ_WAIT', 'REPLAY_READ_RATE', 'REPLAY_INDEX_RATE', 'REPLAY_READ_CONCURRENCY', 'REPLAY_CONTROL_RATE'].includes(cause.message) || performance.now() >= until) throw cause
        await delay(250, signal)
      }
    }
  }

  async manifest(): Promise<RawManifest> {
    if (this.closed) throw Error('REPLAY_CLOSED')
    if (this.running || this.abort) throw Error('REPLAY_BUSY')
    this.profile = 'point'
    const abort = this.abort = new AbortController()
    let admitted = false
    try {
      const fresh = !this.session
      if (fresh) this.session = await apiSend<RawSession>('POST', this.path('session'), {}, AbortSignal.any([abort.signal, AbortSignal.timeout(5000)]))
      if (this.closed) throw Error('REPLAY_CLOSED')
      this.renewal ??= setInterval(() => {
        if (!this.closed) void apiSend('POST',this.path('control'),{ ...this.session,root_seq:this.root,operation:'renew' },AbortSignal.timeout(5000)).catch(() => {})
      }, 30_000)
      if (fresh && this.session!.recovery_wait_seconds > 0) await delay(Math.ceil(this.session!.recovery_wait_seconds * 1000), abort.signal)
      if (this.closed) throw Error('REPLAY_CLOSED')
      this.beginRoot(); admitted = true
      const manifest = this.remember(await this.data('manifest', {}, abort.signal) as RawManifest)
      this.account = manifest.account_id
      return manifest
    } finally {
      let settled = false
      try { if (admitted) await this.settle(); settled = true }
      finally {
        this.abort = null
        if (settled && this.latest && this.account > 0) void this.pump()
      }
    }
  }

  seek(target: RawTarget) {
    if (this.closed) return
    this.latest = { target, revision: ++this.revision }
    this.abort?.abort()
    if (this.pending) {
      this.worker?.postMessage({ kind: 'cancel', id: this.pending.id })
      this.pending.reject(Error('REPLAY_SUPERSEDED'))
      // Termination releases the linear memory before another root can allocate.
      this.worker?.terminate(); this.worker = null; this.pending = null
    }
    void this.pump()
  }

  private async settle() {
    if (!this.session || !this.root) return
    const until = performance.now() + 4000
    let sealed = false
    for (;;) {
      if (performance.now() >= until) throw Error('REPLAY_SETTLEMENT_TIMEOUT')
      try {
        const receipt = await this.data('control', { operation: sealed ? 'ack' : 'seal' },
          AbortSignal.timeout(Math.max(1, Math.min(1000, until - performance.now())))) as { state: string; session_bytes?: number; estimated?: boolean }
        if (receipt.estimated) { await this.retire(); return }
        if (receipt.session_bytes !== undefined) this.bytes = this.offsetBytes + receipt.session_bytes
        if (receipt.state === 'retired' || sealed && receipt.state === 'settled') return
        if (receipt.state === 'settled') sealed = true
      } catch (cause) {
        if (cause instanceof ApiError && ['REPLAY_READER_EPOCH', 'REPLAY_READER_SESSION', 'REPLAY_READER_EXPIRED', 'REPLAY_STOP_PROOF'].includes(cause.code ?? cause.message)) { await this.retire(); return }
      }
      await delay(25)
    }
  }

  private async retire() {
    const old = this.session, sequence = this.root
    if (!old?.session_proof) throw Error('REPLAY_STOP_PROOF')
    const fresh = await apiSend<RawSession>('POST',this.path('session'),{},AbortSignal.timeout(5000))
    const proof = await apiSend<{state:string;estimated?:boolean}>('POST',this.path('control'),{...old,root_seq:sequence,operation:'status'},AbortSignal.timeout(5000))
    if (proof.state !== 'retired') throw Error('REPLAY_RECOVERY_STOPPING')
    this.offsetBytes = this.bytes; this.session = fresh; this.root = 0; this.estimated = true
    if (fresh.recovery_wait_seconds > 0) await delay(Math.ceil(fresh.recovery_wait_seconds*1000))
  }

  private async pump() {
    if (this.running || !this.session || this.abort) return
    this.running = true
    try {
      while (this.latest && !this.closed) {
        const { target, revision } = this.latest
        this.latest = null
        this.profile = target.business?.kind === 'interval' ? 'interval' : 'point'
        this.beginRoot()
        const abort = this.abort = new AbortController()
        const deadline = setTimeout(() => abort.abort(), this.profile === 'interval' ? 120_000 : 60_000)
        try {
          await this.data('control', { operation: 'renew' })
          let candidate = this.resolveTarget(target)
          let frame: RawFrame
          for (;;) {
            try { frame = await this.render(candidate, abort.signal); break }
            catch (cause) {
              if (!(cause instanceof ApiError) || cause.code !== 'REPLAY_INDEX_GENERATION' && cause.message !== 'REPLAY_INDEX_GENERATION') throw cause
              await this.recover(abort.signal)
              this.remember(await this.data('manifest', {}, abort.signal) as RawManifest)
              candidate = this.resolveTarget(target)
            }
          }
          await this.settle()
          frame.cost_estimated = this.estimated
          frame.cumulative_bytes = this.bytes
          if (revision === this.revision && !this.closed) this.publish(frame)
        } catch (cause) {
          this.worker?.postMessage({ kind: 'cancel', id: this.workerId })
          this.worker?.terminate(); this.worker = null
          try { await this.data('control', { operation: 'cancel' }) }
          catch (error) { if (!(error instanceof ApiError) || !['REPLAY_READER_EPOCH','REPLAY_READER_SESSION','REPLAY_READER_EXPIRED','REPLAY_STOP_PROOF'].includes(error.code ?? error.message)) throw error }
          await this.settle()
          if (revision === this.revision && !this.closed) throw cause
        } finally { clearTimeout(deadline); this.abort = null }
      }
    } catch (cause) {
      this.latest = null
      if (!this.closed) this.fail(cause instanceof Error ? cause : Error(String(cause)))
    } finally { this.running = false }
  }

  private async render(target: RawTarget, signal: AbortSignal): Promise<RawFrame> {
    if (this.worker && this.workerLogical !== this.logical) { await this.workerRequest({ kind: 'root-begin' }, signal); this.workerLogical = this.logical }
    const view = { index_generation: target.generation, recording: target.recording.id, through_seq: target.recording.seq, commit_id: target.recording.commit_id,
      time_us: target.time_us, capture_seq: target.capture_seq, stream: target.stream }
    const plan = await this.data('plan', view, signal) as RawPlan
    if (plan.account_id !== this.account || plan.execution_id !== this.execution || plan.recording_id !== target.recording.id || plan.through_seq !== target.recording.seq || plan.target_us !== target.time_us) throw Error('REPLAY_PLAN_CONTEXT')
    if (plan.index_generation !== target.generation || plan.commit_id !== target.recording.commit_id || !Number.isSafeInteger(plan.cutoff_seq) || plan.cutoff_seq > target.recording.seq || plan.blocks.length > 33 || plan.evidence.length > 32 || plan.planned_bytes > 4 * 1024 * 1024) throw Error('REPLAY_PLAN_BUDGET')
    const events: RawPreview[] = []
    let readBytes = 0
    const previewRows = plan.blocks.map(block => ({ key: `${this.account}:${target.recording.id}:${block.sha256}:${block.length}:${block.raw_length}`, block }))
    for (let offset = 0; offset < previewRows.length;) {
      const rows = []
      let size = 0
      while (offset < previewRows.length && size + previewRows[offset].block.length <= 1024 * 1024) {
        const row = previewRows[offset++]; rows.push(row); size += row.block.length
      }
      if (!rows.length) throw Error('REPLAY_BLOCK_BUDGET')
      const loaded = await this.prefetch(view, rows, signal)
      readBytes += loaded.read
      await this.workerRequest({ kind: 'cache', rows: loaded.rows }, signal)
    }
    for (const block of plan.blocks) {
      signal.throwIfAborted()
      const sequences = plan.evidence.filter(row => row.block === block.id).map(row => row.seq)
      if (plan.selected?.block === block.id && !sequences.includes(plan.selected.seq)) sequences.push(plan.selected.seq)
      const key = `${this.account}:${target.recording.id}:${block.sha256}:${block.length}:${block.raw_length}`
      const decoded = await this.workerRequest<{ events: RawPreview[] }>({ kind: 'decode', key, block, sequences, quote_seq: plan.selected?.seq ?? null }, signal)
      if (!Array.isArray(decoded.events) || decoded.events.length > 33) throw Error('REPLAY_PREVIEW_BUDGET')
      events.push(...decoded.events)
    }
    signal.throwIfAborted()
    const frame: RawFrame = { target_us: plan.target_us, target_seq: plan.cutoff_seq, index_generation: target.generation,
      quote: events.find(event => event.capture_seq === plan.selected?.seq && event.topic.startsWith('/quote/')) ?? null,
      quote_valid: false, previous: plan.previous, next: plan.next,
      evidence: plan.evidence.map(row => events.find(event => event.capture_seq === row.seq)!).filter(Boolean),
      planned_bytes: plan.planned_bytes, read_bytes: readBytes, cumulative_bytes: 0, roots: this.logical,
      wasm_bytes: this.wasmBytes, cache_bytes: this.cacheBytes }
    const businessView = { ...view, cutoff_seq: plan.cutoff_seq, scope_symbol: target.symbol ?? target.recording.streams.find(s => s.id === target.stream)?.symbol, interval_start_us: target.recording.start_us ?? 0 }
    if (frame.quote && businessView.scope_symbol) {
      const validity = await this.businessCompute(businessView, { kind: 'interval', after: [0,0], interval: { start_us: target.time_us, end_us: target.time_us + 1, price_tick: 1, distribution: false } }, signal, 128, true)
      const book = validity.result.subjects[0].data.book as { spread_valid_us: number }
      frame.quote_valid = book.spread_valid_us === 1
      frame.read_bytes += validity.read; frame.planned_bytes += validity.planned
    }
    if (target.business) {
      const business = await this.business(businessView, target.business, signal, target.recording.streams.filter(s => s.topic === '/command').length > 16 ? 8 : 128)
      frame.business = business.result; frame.read_bytes += business.read; frame.planned_bytes += business.planned
    }
    frame.wasm_bytes = this.wasmBytes; frame.cache_bytes = this.cacheBytes
    frame.dependency_plan = this.dependencyPlan
    frame.cost_estimated = this.estimated
    return frame
  }

  private async binary(operation: string, body: Record<string, unknown>, length: number, signal: AbortSignal) {
    for (;;) {
      const previous = this.spent.bytes
      try { return await this.binaryOnce(operation, body, length, signal) }
      catch (cause) {
        if (!(cause instanceof ApiError && cause.status === 429) && this.spent.bytes === previous) this.reserveLost(1024 * 1024 + 1032)
        if (signal.aborted || !(cause instanceof TypeError || cause instanceof DOMException || cause instanceof ApiError && ['REPLAY_READER_EPOCH', 'REPLAY_READER_SESSION', 'REPLAY_READER_EXPIRED'].includes(cause.code ?? cause.message))) throw cause
        await this.recover(signal)
      }
    }
  }
  private async binaryOnce(operation: string, body: Record<string, unknown>, length: number, signal: AbortSignal) {
    if (!Number.isSafeInteger(length) || length <= 0 || length > 1024 * 1024 + 1032) throw Error('REPLAY_BLOCK_BUDGET')
    let response: Response
    const until = performance.now() + 12_000
    for (;;) {
      response = await fetch('/api/v1' + this.path(operation), { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...this.session, root_seq: this.root, attempt: this.attempt, profile: this.profile, ...body }), signal })
      if (response.ok) { if (!this.usage(response)) this.reserveLost(1024 * 1024 + 1032); break }
      const text = await response.text()
      let detail: unknown
      try { detail = JSON.parse(text) } catch { detail = null }
      const cause = apiErrorFromBody(response.status, response.statusText, detail, text)
      if (cause.status !== 429 || !['REPLAY_READ_WAIT', 'REPLAY_READ_RATE', 'REPLAY_INDEX_RATE', 'REPLAY_READ_CONCURRENCY'].includes(cause.message) || performance.now() >= until) throw cause
      await delay(250, signal)
    }
    if (!response.body) throw Error('REPLAY_BLOCK_BODY')
    const reader = response.body.getReader()
    const bytes = new Uint8Array(length)
    let offset = 0
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        if (offset + value.byteLength > bytes.byteLength) throw Error('REPLAY_BLOCK_LENGTH')
        bytes.set(value, offset); offset += value.byteLength
      }
      if (offset !== bytes.byteLength) throw Error('REPLAY_BLOCK_LENGTH')
      return bytes.buffer
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
  }

  private async prefetch(view: Record<string, unknown>, rows: { key: string; block: RawBlockInfo }[], signal: AbortSignal) {
    const cached = await this.workerRequest<{ present: boolean[] }>({ kind: 'has', keys: rows.map(row => row.key) }, signal)
    if (cached.present.length !== rows.length || cached.present.some(value => typeof value !== 'boolean')) throw Error('REPLAY_CACHE_QUERY')
    const missing = rows.filter((_, i) => !cached.present[i])
    let read = 0
    const buffers = new Map<number, ArrayBuffer>()
    if (missing.length) {
      for (const row of missing) {
        if (!Number.isSafeInteger(row.block.id) || !Number.isSafeInteger(row.block.length) || row.block.length <= 0 || row.block.length > 600 * 1024) throw Error('REPLAY_BLOCK_BUDGET')
        read += row.block.length
      }
      const bytes = await this.binary('blocks', { ...view, blocks: missing.map(row => row.block.id) }, 8 + missing.length * 8 + read, signal)
      const header = new DataView(bytes)
      if (header.getUint32(0, true) !== 0x32305241 || header.getUint32(4, true) !== missing.length) throw Error('REPLAY_BLOCK_FRAMING')
      let offset = 8
      for (const row of missing) {
        if (header.getUint32(offset, true) !== row.block.id || header.getUint32(offset + 4, true) !== row.block.length) throw Error('REPLAY_BLOCK_FRAMING')
        offset += 8
        buffers.set(row.block.id, bytes.slice(offset, offset + row.block.length)); offset += row.block.length
      }
      if (offset !== bytes.byteLength) throw Error('REPLAY_BLOCK_FRAMING')
    }
    return { read, rows: rows.map(row => ({ ...row, bytes: buffers.get(row.block.id) })) }
  }

  private workerRequest<T>(request: Record<string, unknown>, signal: AbortSignal): Promise<T> {
    this.worker ??= new Worker(new URL('./raw.worker.ts', import.meta.url), { type: 'module' })
    const worker = this.worker, id = ++this.workerId
    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout>
      let finished = false
      const finish = (cause?: Error, result?: T, preserveWorker = false) => {
        if (finished) return
        finished = true
        clearTimeout(timer); signal.removeEventListener('abort', canceled)
        worker.onmessage = null; worker.onerror = null
        if (this.pending?.id === id) this.pending = null
        if (cause) { if (!preserveWorker) { worker.terminate(); if (this.worker === worker) this.worker = null }; reject(cause) }
        else resolve(result!)
      }
      const canceled = () => finish(Error('REPLAY_WORKER_CANCELED'))
      this.pending = { id, reject: cause => finish(cause) }
      signal.addEventListener('abort', canceled, { once: true })
      timer = setTimeout(() => finish(Error('REPLAY_WORKER_TIMEOUT')), 5000)
      worker.onerror = () => finish(Error('REPLAY_WORKER_ERROR'))
      worker.onmessage = ({ data }) => {
        if (data.id !== id) return
        if (data.error) { finish(Error(data.error), undefined, data.error === 'REPLAY_DEPENDENCY_MISSING'); return }
        if (!Number.isSafeInteger(data.wasm_bytes) || data.wasm_bytes > 32 * 1024 * 1024 || !Number.isSafeInteger(data.cache_bytes) || data.cache_bytes > 4 * 1024 * 1024) { finish(Error('REPLAY_WORKER_BUDGET')); return }
        if (data.checkpoint_bytes !== undefined && (!Number.isSafeInteger(data.checkpoint_bytes) || data.checkpoint_bytes < 0 || data.checkpoint_bytes > 1024 * 1024)) { finish(Error('REPLAY_CHECKPOINT_BUDGET')); return }
        this.wasmBytes = data.wasm_bytes; this.cacheBytes = data.cache_bytes
        if (data.split === true) { finish(Error('REPLAY_DEPENDENCY_RESULT_BUDGET'), undefined, true); return }
        finish(undefined, data)
      }
      const transfers = request.bytes instanceof ArrayBuffer ? [request.bytes] : Array.isArray(request.rows) ? request.rows.flatMap(row => row.bytes instanceof ArrayBuffer ? [row.bytes] : []) : []
      try { worker.postMessage({ ...request, id }, transfers) }
      catch (cause) { finish(cause instanceof Error ? cause : Error(String(cause))) }
      if (signal.aborted) canceled()
    })
  }

  private async business(view: Record<string, unknown>, selection: NonNullable<RawTarget['business']>, signal: AbortSignal, commandLimit = 128) {
    if (selection.kind === 'interval') {
      const interval=selection.interval
      if (!interval || !Number.isSafeInteger(interval.start_us) || !Number.isSafeInteger(interval.end_us) || interval.start_us < Number(view.interval_start_us) || interval.start_us >= interval.end_us || interval.end_us > Number(view.time_us)+1 || !Number.isFinite(interval.price_tick) || interval.price_tick <= 0) throw Error('REPLAY_INTERVAL_PARAMETERS')
    }
    const group = await digest([this.account,this.execution,view.recording,view.commit_id,view.index_generation,view.scope_symbol,view.interval_start_us,selection.interval,'raw-v2.4'])
    const key = await digest([group,selection,view.cutoff_seq]), cutoff = Number(view.cutoff_seq)
    const scope = this.results.get(key,group,cutoff)
    if (scope.value) {
      const result=JSON.parse(scope.value) as NonNullable<RawFrame['business']>
      if (selection.kind==='statistics') for (const subject of result.subjects) subject.data.interval_end_us=Number(view.time_us)+1
      return { result, read:0, planned:0 }
    }
    const output = await this.businessCompute(view, selection, signal, commandLimit)
    this.results.put(key,scope,cutoff,JSON.stringify(output.result))
    return output
  }

  private async businessCompute(view: Record<string, unknown>, selection: NonNullable<RawTarget['business']>, signal: AbortSignal, commandLimit = 128, bookOnly = false) {
    let planned = 0, read = 0, estimatedPlan = 0, estimatedObjects = 0, estimatedFolds = 0, prefixReady = false, prefixBytes = 0
    const planStart = () => ({ bytes: this.spent.bytes, steps: this.spent.index_steps, raw: estimatedPlan, objects: estimatedObjects, folds: estimatedFolds })
    const acceptPlan = (start: ReturnType<typeof planStart>, memberGroups=0) => {
      const raw=prefixReady?0:estimatedPlan-start.raw, count=estimatedObjects-start.objects, index=this.spent.index_steps-start.steps
      const futureBytes=raw+(this.spent.bytes-start.bytes)+count*16*(1+memberGroups)
      const futureSteps=index+count*64*(1+memberGroups)
      if(this.spent.bytes+futureBytes+256*1024>8*1024*1024||!prefixReady&&this.spent.index_steps+futureSteps>8_000_000)throw Error('REPLAY_DEPENDENCY_PLAN_BUDGET')
      this.dependencyPlan={raw_bytes:prefixReady?prefixBytes:raw,index_step_limit:8_000_000-this.spent.index_steps,member_lookups:count*memberGroups,folds:estimatedFolds-start.folds}
    }
    const metadata = async <T>(operation: string, fields: Record<string, unknown>) => {
      const result = await this.data(operation, { ...view, ...fields }, signal) as T & RawPlan
      if (result.account_id !== this.account || result.execution_id !== this.execution || result.index_generation !== view.index_generation || result.recording_id !== view.recording || result.through_seq !== view.through_seq || result.commit_id !== view.commit_id) throw Error('REPLAY_BUSINESS_CONTEXT')
      return result
    }
    const objects = async (kind: string, after: RawCursor = [0, 0], limit = 8, planning=false) => {
      const page = await metadata<{ subjects: RawSubject[]; ids?: RawCursor[]; keys?:string[]; next: RawCursor | null }>(kind.includes('market_trade') ? 'market-objects' : 'objects', { kind, after, limit,planning })
      if(page.ids){
        if(!planning&&(!page.keys||page.keys.length!==page.ids.length))throw Error('REPLAY_SUBJECT_BUDGET')
        page.subjects=page.ids.map(([first_seq,id],i)=>({id,first_seq,kind,key:page.keys?.[i]??''}))
      }
      if (page.subjects.length > limit || page.subjects.some(subject => subject.kind !== kind || !Number.isSafeInteger(subject.id) || subject.key.length > 4096) || page.next && (page.next[0] < after[0] || page.next[0] === after[0] && page.next[1] <= after[1])) throw Error('REPLAY_SUBJECT_BUDGET')
      return page
    }
    if(!bookOnly&&this.profile==='point'){
      const blockPage=(after:RawCursor)=>metadata<{blocks:RawBlockInfo[];next:RawCursor|null}>('business-blocks',{after})
      let cursor:RawCursor=[0,0],count=0
      for(;;){
        const page=await blockPage(cursor)
        if(page.blocks.length>128||page.next&&(page.next[0]<cursor[0]||page.next[0]===cursor[0]&&page.next[1]<=cursor[1]))throw Error('REPLAY_PLAN_CURSOR')
        prefixBytes+=page.blocks.reduce((n,b)=>n+b.length,0);count+=page.blocks.length
        if(prefixBytes>4*1024*1024||count>4096)break
        if(!page.next){prefixReady=true;break}cursor=page.next
      }
      if(prefixReady){
        if(this.spent.bytes+prefixBytes+256*1024>8*1024*1024)throw Error('REPLAY_DEPENDENCY_PLAN_BUDGET')
        cursor=[0,0]
        for(;;){
          const page=await blockPage(cursor)
          if(page.blocks.length>128||page.next&&(page.next[0]<cursor[0]||page.next[0]===cursor[0]&&page.next[1]<=cursor[1]))throw Error('REPLAY_PLAN_CURSOR')
          for(let offset=0;offset<page.blocks.length;){
            const rows=[];let size=0
            while(offset<page.blocks.length&&size+page.blocks[offset].length<=1024*1024){const b=page.blocks[offset++];rows.push({key:`${this.account}:${view.recording}:${b.sha256}:${b.length}:${b.raw_length}`,block:b});size+=b.length}
            if(!rows.length)throw Error('REPLAY_BLOCK_BUDGET')
            const loaded=await this.prefetch(view,rows,signal);read+=loaded.read
            await this.workerRequest({kind:'cache',rows:loaded.rows},signal)
            await this.workerRequest({kind:'pin-root',keys:rows.map(r=>r.key)},signal)
          }
          if(!page.next)break;cursor=page.next
        }
        planned+=prefixBytes
      }
    }
    const derive = async <T>(mode: string) => (await this.workerRequest<{ result: T }>({ kind: 'derive-result', mode }, signal)).result
    const foldOnce = async (subjects: RawSubject[], options: Record<string, unknown> = {}) => {
      const book = options.evaluator === 'book', market = subjects.some(s => s.kind.includes('market_trade'))
      if (!subjects.length && !book) return { subjects: [] as RawBusiness[], fences: [] }
      const fields = { objects: subjects.map(subject => subject.id), cutoff_seq: options.cutoff_seq ?? view.cutoff_seq, after_seq: options.after_seq ?? 0 }
      const catalog = await metadata<{ streams: number[]; initial: { stream: number; blocks: RawBlockInfo[]; next: number | null }[]; range_through?: number }>(book ? 'range-blocks' : market ? 'market-streams' : 'object-streams', book ? { catalog: true } : fields)
      if (catalog.streams.length > 16 || new Set(catalog.streams).size !== catalog.streams.length) throw Error('REPLAY_STREAM_BUDGET')
      const blockPage = (stream: number, after_block: number) => metadata<{ blocks: RawBlockInfo[]; next: number | null }>(book ? 'range-blocks' : market ? 'market-blocks' : 'object-blocks', { ...fields, range_through: catalog.range_through, stream, after_block, page_bytes: Math.floor(1024 * 1024 / catalog.streams.length) })
      let stageBytes = 0
      if (catalog.initial.length !== catalog.streams.length || catalog.initial.reduce((n, p) => n + p.blocks.length, 0) > 128) throw Error('REPLAY_PLAN_BUDGET')
      const firstPlans = new Map(catalog.initial.map(p => [p.stream, p]))
      for (const stream of catalog.streams) {
        let after = 0
        for (;;) {
          const plan = after === 0 ? firstPlans.get(stream) : await blockPage(stream, after)
          if (!plan) throw Error('REPLAY_PLAN_STREAM')
          if (plan.blocks.length > 128 || plan.next !== null && plan.next <= after) throw Error('REPLAY_PLAN_CURSOR')
          for (const block of plan.blocks) stageBytes += block.length
          if (stageBytes > (book ? 128 : 4) * 1024 * 1024) throw Error('REPLAY_DEPENDENCY_BUDGET')
          if (plan.next === null) break
          after = plan.next
        }
      }
      if (options.planning) { estimatedPlan += stageBytes; estimatedFolds++; return { subjects: [] as RawBusiness[], fences: [] } }
      planned += stageBytes
      await this.workerRequest({ kind: 'business-begin', ...view, ...options, market, subjects, streams: catalog.streams }, signal)
      let linkCursor: RawCursor | null = market || subjects.some(s => s.kind === 'trade') ? [0, 0] : null
      const loadLinks = async () => {
        const response = linkCursor === null ? { links: [], next: null } : await metadata<{ links: { seq: number; object: number; conflict: boolean }[]; next: RawCursor | null }>(market ? 'market-links' : 'object-links', { ...fields, after: linkCursor })
        if (response.links.length > 128 || response.next && linkCursor && (response.next[0] < linkCursor[0] || response.next[0] === linkCursor[0] && response.next[1] <= linkCursor[1])) throw Error('REPLAY_LINK_CURSOR')
        linkCursor = response.next
        await this.workerRequest({ kind: 'links', links: response.links, more: response.next !== null }, signal)
      }
      let versionCursor: number | null = Number(options.after_seq ?? 0)
      const loadVersions = async () => {
        const versions = versionCursor === null ? { versions: [], next: null } : await metadata<{ versions: { seq: number; generation: number }[]; next: number | null }>('versions', { ...fields, after: versionCursor })
        if (versions.next !== null && versions.next <= (versionCursor ?? 0)) throw Error('REPLAY_GENERATION_CURSOR')
        versionCursor = versions.next
        await this.workerRequest({ kind: 'versions', versions: versions.versions, more: versions.next !== null }, signal)
      }
      const cursors = new Map(catalog.streams.map(stream => [stream, 0 as number | null]))
      const load = async (stream: number) => {
        if (!cursors.has(stream)) throw Error('REPLAY_STREAM_STATE')
        const cursor = cursors.get(stream)!
        const plan = cursor === null ? { blocks: [], next: null } : firstPlans.get(stream) ?? await blockPage(stream, cursor)
        firstPlans.delete(stream)
        if (plan.blocks.length > 128 || plan.next !== null && plan.next <= (cursor ?? 0)) throw Error('REPLAY_PLAN_BUDGET')
        cursors.set(stream, plan.next)
        const loaded = await this.prefetch(view, plan.blocks.map(block => ({ key: `${this.account}:${view.recording}:${block.sha256}:${block.length}:${block.raw_length}`, block })), signal)
        read += loaded.read
        await this.workerRequest({ kind: 'business-page', stream, rows: loaded.rows, more: plan.next !== null }, signal)
      }
      for (const stream of catalog.streams) await load(stream)
      for (;;) {
        const step = await this.workerRequest<{ need: number[]; needVersions: boolean; needLinks: boolean; done: boolean }>({ kind: 'business-step' }, signal)
        if (step.need.length > 16 || new Set(step.need).size !== step.need.length) throw Error('REPLAY_STREAM_BUDGET')
        if (step.done) break
        for (const stream of step.need) await load(stream)
        if (step.needVersions) await loadVersions()
        if (step.needLinks) await loadLinks()
      }
      const output = await this.workerRequest<{ subjects: RawBusiness[]; fences: unknown[] }>({ kind: 'business-result', save: options.save, cutoffs: options.cutoffs }, signal)
      if (output.subjects.length !== subjects.length || output.subjects.some((subject, i) => subject.id !== subjects[i].id || subject.key !== subjects[i].key || subject.kind !== subjects[i].kind)) throw Error('REPLAY_BATCH_RESULT')
      return output
    }
    const group = await digest([this.account,this.execution,view.recording,view.commit_id,view.index_generation,view.scope_symbol,view.interval_start_us,selection.interval,'raw-v2.4'])
    const fold = async (subjects: RawSubject[], options: Record<string, unknown> = {}) => {
      if (!subjects.length || options.save || options.cutoffs || options.after_seq || options.planning) return foldOnce(subjects,options)
      const scope_key = await digest([group,subjects]), cutoff = Number(options.cutoff_seq ?? view.cutoff_seq)
      let scope = await this.workerRequest<{ after_seq: number; incarnation: number; revision: number }>({ kind: 'checkpoint',scope_key,scope_group:group,cutoff_seq:cutoff },signal)
      if (!Number.isSafeInteger(scope.after_seq) || scope.after_seq < 0 || scope.after_seq > cutoff || !Number.isSafeInteger(scope.incarnation) || !Number.isSafeInteger(scope.revision)) throw Error('REPLAY_CHECKPOINT_CONTEXT')
      const revise = async () => { scope = await this.workerRequest({ kind:'scope-revise',scope_key,scope_group:group,cutoff_seq:cutoff },signal) }
      if (scope.after_seq > 0 && subjects.some(s=>s.kind==='trade'||s.kind==='market_trade')) {
        const origin = await metadata<{ rebuild: boolean }>(subjects[0].kind.includes('market_trade') ? 'market-origin' : 'dependency-boundary',{ objects:subjects.map(s=>s.id),after_seq:scope.after_seq })
        if (origin.rebuild) await revise()
      }
      try { return await foldOnce(subjects,{ ...options,scope_key,scope_group:group,...scope }) }
      catch (cause) {
        if (!(cause instanceof Error) || cause.message!=='REPLAY_DEPENDENCY_MISSING') throw cause
        await revise()
        return foldOnce(subjects,{ ...options,scope_key,scope_group:group,...scope,after_seq:0 })
      }
    }
    const owners = async (targets: RawBusiness[]) => {
      await this.workerRequest({ kind: 'derive-begin', mode: 'owner', targets }, signal)
      const ids = targets.filter(t => t.kind === 'order' || !t.conflict && typeof t.data.order_id === 'string' && !t.data.execution_id && !['manual', 'external'].includes(String(t.data.origin))).map(t => t.id)
      if (ids.length) {
        let after: RawCursor = [0, 0]
        for (;;) {
          const page = await metadata<{ subjects: RawSubject[]; next: RawCursor | null }>('commands', { objects: ids, after, limit: commandLimit })
          if (page.subjects.length > commandLimit || page.next && (page.next[0] < after[0] || page.next[0] === after[0] && page.next[1] <= after[1])) throw Error('REPLAY_COMMAND_CURSOR')
          const result = await fold(page.subjects)
          await this.workerRequest({ kind: 'derive-feed', mode: 'owner', subjects: result.subjects }, signal)
          if (page.next === null) break
          after = page.next
        }
      }
      return derive<RawBusiness[]>('owner')
    }
    const trades = async (consume: (rows: RawBusiness[], fences: unknown[]) => Promise<void | boolean>, options: Record<string, unknown> = {}, ownership = false, kinds = ['trade', 'weak_trade']) => {
      const visit = async (subjects: RawSubject[]): Promise<boolean> => {
        if(options.planning&&prefixReady){estimatedObjects+=subjects.length;estimatedFolds+=2*Math.ceil(subjects.length/8)-1;return true}
        let output
        try { output = await fold(subjects, options) }
        catch (cause) {
          const message = cause instanceof Error ? cause.message : ''
          if (subjects.length <= 8 || !['REPLAY_DEPENDENCY_WORKING_SET', 'REPLAY_DEPENDENCY_BUDGET', 'REPLAY_DEPENDENCY_RESULT_BUDGET','REPLAY_INDEX_BUDGET'].includes(message)) throw cause
          const middle = Math.ceil(subjects.length / 2)
          return await visit(subjects.slice(0, middle)) && await visit(subjects.slice(middle))
        }
        if(options.planning)estimatedObjects+=subjects.length
        if (options.planning && ownership) {
          let after: RawCursor = [0, 0]
          for (;;) {
            const page = await metadata<{ subjects: RawSubject[]; next: RawCursor | null }>('commands', { objects: subjects.map(s => s.id), after, limit: commandLimit })
            if (page.subjects.length) await fold(page.subjects, { planning: true })
            if (!page.next) break
            if (page.next[0] < after[0] || page.next[0] === after[0] && page.next[1] <= after[1]) throw Error('REPLAY_COMMAND_CURSOR')
            after = page.next
          }
        }
        return options.planning === true || await consume(ownership ? await owners(output.subjects) : output.subjects, output.fences) !== false
      }
      for (const kind of kinds) {
        let after: RawCursor = [0, 0]
        for (;;) {
          const page = await objects(kind, after, 128, options.planning===true)
          if (page.subjects.length) {
            if (!await visit(page.subjects)) return
          }
          if (!page.next) break
          after = page.next
        }
      }
    }
    if (selection.kind === 'interval') {
      const interval = selection.interval!
      view = { ...view, interval_start_us: interval.start_us, interval_end_us: interval.end_us }
      let startObserved = false, cursor: RawCursor = [0, 0]
      for (; !bookOnly;) {
        const page = await objects('execution', cursor), subject = page.subjects.find(s => s.key === this.execution)
        if (subject) { startObserved = (await fold([subject])).subjects[0]?.data.start_observed === true; break }
        if (!page.next) break
        cursor = page.next
      }
      const marketKinds = ['market_trade', 'weak_market_trade']
      const planStart = estimatedPlan
      for (let pass = 0; pass < (bookOnly ? 0 : interval.distribution ? 2 : 1); pass++) {
        await trades(async () => {}, { planning: true }, true)
        await trades(async () => {}, { planning: true }, false, marketKinds)
      }
      await fold([], { evaluator: 'book', planning: true })
      if (estimatedPlan - planStart > 120 * 1024 * 1024) throw Error('REPLAY_INTERVAL_PLAN_BUDGET')
      await this.workerRequest({ kind: 'interval-begin', ...view, ...interval, point: bookOnly, execution: this.execution, start_observed: startObserved }, signal)
      for (let pass = 0; pass < (bookOnly ? 0 : interval.distribution ? 2 : 1); pass++) {
        if (pass) await this.workerRequest({ kind: 'interval-second' }, signal)
        await trades(async rows => { await this.workerRequest({ kind: 'interval-feed', source: 'own', subjects: rows }, signal) }, {}, true)
        await trades(async rows => { await this.workerRequest({ kind: 'interval-feed', source: 'market', subjects: rows }, signal) }, {}, false, marketKinds)
      }
      await fold([], { evaluator: 'book' })
      const result = await this.workerRequest<{ result: Record<string, unknown> }>({ kind: 'interval-result' }, signal)
      const complete = ['own','market','book'].every(key => (result.result[key] as { complete?: boolean } | undefined)?.complete === true)
      return { result: { kind: 'interval', subjects: [{ id: 1, kind: 'interval', key: this.execution, data: result.result, first_seq: 0, observed_seq: Number(view.cutoff_seq), last_fact_seq: 0, valid: complete, conflict: false, owner_conflict: false, resolved_seq: 0, proofs: {} }], next: null }, read, planned }
    }
    if (selection.kind === 'statistics') {
      let startObserved = false, after: RawCursor = [0, 0]
      for (;;) {
        const page = await objects('execution', after)
        const subject = page.subjects.find(s => s.key === this.execution)
        if (subject) { startObserved = (await fold([subject])).subjects[0]?.data.start_observed === true; break }
        if (!page.next) break
        after = page.next
      }
      const sourceView=view
      view={...view,controls_only:true,interval_end_us:Number(view.time_us)+1}
      await this.workerRequest({kind:'interval-begin',...view,price_tick:1,distribution:false,start_observed:startObserved},signal)
      await fold([],{evaluator:'book'})
      const coverage=(await this.workerRequest<{result:{book:{own_capture_us:number}}}>({kind:'interval-result'},signal)).result.book
      view=sourceView
      const estimate=planStart()
      await trades(async()=>{}, {planning:true}, true)
      acceptPlan(estimate)
      await this.workerRequest({ kind: 'derive-begin', mode: 'statistics', execution: this.execution, scope_symbol: view.scope_symbol, interval_start_us: view.interval_start_us, interval_end_us: Number(view.time_us) + 1, start_observed: startObserved }, signal)
      await trades(async rows => { await this.workerRequest({ kind: 'derive-feed', mode: 'statistics', subjects: rows }, signal) }, {}, true)
      const data = await derive<Record<string, unknown>>('statistics')
      data.complete=data.complete===true&&coverage.own_capture_us===Number(view.time_us)+1-Number(view.interval_start_us)
      return { result: { kind: selection.kind, subjects: [{ id: 1, kind: 'statistics', key: this.execution, data, first_seq: 0, observed_seq: Number(view.cutoff_seq), last_fact_seq: 0, valid: data.complete === true, conflict: false, owner_conflict: false, resolved_seq: 0, proofs: {} }], next: null }, read, planned }
    }
    const page = await objects(selection.kind, selection.after)
    if (!page.subjects.length) return { result: { kind: selection.kind, subjects: [], next: null }, read, planned }
    let result: RawBusiness[]
    if (selection.kind === 'order') {
      await this.workerRequest({ kind: 'order-reset' }, signal)
      const identities = page.subjects.map(subject => ({ ...subject, data: {} })) as RawBusiness[]
      const owned = await owners(identities)
      let after = 0, from = 0
      result = []
      const queryPage = async (after:number) => {
        const queries = await metadata<{ sequences: number[]; next: number | null }>('queries', { objects: page.subjects.map(s => s.id), after })
        if (queries.sequences.length > 128 || queries.sequences.some((seq, i, a) => !Number.isSafeInteger(seq) || seq <= (i ? a[i - 1] : after)) || queries.next !== null && queries.next <= after) throw Error('REPLAY_QUERY_CURSOR')
        const cutoffs = queries.sequences
        if (queries.next === null && cutoffs.at(-1) !== view.cutoff_seq) {
          if (cutoffs.length === 128) { /* The final target is its own finite continuation. */ }
          else cutoffs.push(Number(view.cutoff_seq))
        }
        if (!cutoffs.length) throw Error('REPLAY_QUERY_PLAN')
        return queries
      }
      const estimate=planStart()
      for(let cursor=0,start=0;;){
        const queries=await queryPage(cursor),until=queries.sequences.at(-1)!
        await trades(async()=>{},{planning:true,cutoff_seq:until,cutoffs:queries.sequences,orders:page.subjects})
        await fold(page.subjects,{planning:true,after_seq:start,cutoff_seq:until})
        if(queries.next===null&&until===view.cutoff_seq)break
        cursor=queries.next??until;start=until
      }
      acceptPlan(estimate)
      for (;;) {
        const queries=await queryPage(after),cutoffs=queries.sequences
        await this.workerRequest({ kind: 'derive-begin', mode: 'fences', targets: page.subjects, cutoffs }, signal)
        await trades(async (rows, fences) => { await this.workerRequest({ kind: 'derive-feed', mode: 'fences', subjects: rows, fences }, signal) }, { cutoffs, orders: page.subjects, cutoff_seq: cutoffs.at(-1) })
        const fences = await derive<Array<Array<{ seq: number; quantity: number; complete: boolean }>>>('fences')
        if (fences.some(f => f.length !== cutoffs.length)) throw Error('REPLAY_QUERY_FENCES')
        const until = cutoffs.at(-1)!
        const subjects = page.subjects.map((subject, i) => ({ ...subject, aux: { ...subject.aux, owner: owned[i].owner, trade_fences: fences[i], final_trades: fences[i].at(-1) } }))
        result = (await fold(subjects, { after_seq: from, cutoff_seq: until, resume: from > 0, save: true })).subjects
        from = until
        if (queries.next === null && until === view.cutoff_seq) break
        after = queries.next ?? until
      }
      await this.workerRequest({ kind: 'order-reset' }, signal)
    } else {
      result = (await fold(page.subjects)).subjects
      if (selection.kind === 'trade' || selection.kind === 'weak_trade') result = await owners(result)
      if (selection.kind === 'position') {
        const dependency = await this.workerRequest<{ needed: boolean }>({ kind: 'derive-begin', mode: 'position', targets: result }, signal)
        if(dependency.needed){
          const estimate=planStart(), proofs=new Set<string>()
          for(const target of result)for(const value of Object.values(target.proofs)){
            const p=value as {valid:boolean;kind:string;seq:number;row:number}
            if(p.valid&&p.kind==='included_trade_keys')proofs.add(`${p.seq}:${p.row}`)
          }
          await trades(async()=>{}, {planning:true})
          acceptPlan(estimate,proofs.size)
        }
        if (dependency.needed) await trades(async rows => {
          const proofs = new Map<string, unknown>()
          const members: Record<string, unknown>[] = []
          for (const target of result) {
            const fields: Record<string, unknown> = {}
            for (const [field, value] of Object.entries(target.proofs)) {
              const proof = value as { valid: boolean; kind: string; seq: number; row: number }
              if (!proof.valid || proof.kind !== 'included_trade_keys') continue
              const key = `${proof.seq}:${proof.row}`
              let response = proofs.get(key)
              if (!response) {
                response = await metadata('members', { proof_seq: proof.seq, row: proof.row, objects: rows.map(row => row.id) })
                proofs.set(key, response)
              }
              fields[field] = response
            }
            members.push(fields)
          }
          return (await this.workerRequest<{ needed: boolean }>({ kind: 'derive-feed', mode: 'position', subjects: rows, members }, signal)).needed
        })
        result = await derive<RawBusiness[]>('position')
      }
    }
    return { result: { kind: selection.kind, subjects: result, next: page.next }, read, planned }
  }

  close() {
    if (this.renewal) clearInterval(this.renewal)
    this.closed = true; this.latest = null; this.abort?.abort()
    this.worker?.terminate(); this.worker = null
    this.pending?.reject(Error('REPLAY_CLOSED')); this.pending = null
    this.results.clear()
    // Server lease/deadline also fences an abrupt page close that cannot send this control.
  }
}
