import init, { BusinessBatch, BusinessDerivation, IntervalAnalysis, RawBlock } from './raw-wasm/axile_replay_heat'
import wasmUrl from './raw-wasm/axile_replay_heat_bg.wasm?url'
import type { RawBlockInfo, RawPreview, RawSubject } from './raw'
import { RawScopes } from './rawScopes'

const CACHE_LIMIT = 4 * 1024 * 1024
const WASM_LIMIT = 32 * 1024 * 1024
const cache = new Map<string, Uint8Array<ArrayBuffer>>()
const pinned = new Set<string>()
const rootPins = new Set<string>()
let cacheBytes = 0, canceledThrough = 0
const ready = init(wasmUrl)
const yieldTask = () => new Promise(resolve => setTimeout(resolve, 0))
let batch: BusinessBatch | undefined
let batchSlots = 0
let interval: IntervalAnalysis | undefined, book = false
type Link = { seq: number; object: number; conflict: boolean }
let links: Link[] = [], linkOffset = 0, moreLinks = false, linkSequence = 0, currentLinks: Link[] = []
const derivations = new Map<string, BusinessDerivation>()
const orderStates = new Map<number, unknown>()
const checkpoints = new RawScopes()
let checkpoint: ReturnType<RawScopes['get']> | undefined, checkpointKey = '', checkpointBoundary = 0
let versions: { seq: number; generation: number }[] = [], versionOffset = 0, moreVersions = false
type CachedBlock = { key: string; block: RawBlockInfo; bytes?: ArrayBuffer }
type Stream = { block?: RawBlock; sequence?: number; page: CachedBlock[]; offset: number; ended: boolean; more: boolean }
const streams = new Map<number, Stream>()

type Request = {
  kind: 'cancel' | 'pin-root' | 'has' | 'cache' | 'decode' | 'business-begin' | 'business-page' | 'business-step' | 'business-result' | 'derive-begin' | 'derive-feed' | 'derive-result' | 'order-reset' | 'versions' | 'links' | 'interval-begin' | 'interval-feed' | 'interval-second' | 'interval-result' | 'checkpoint' | 'scope-revise' | 'root-begin'
  id: number; key: string; block: RawBlockInfo; bytes?: ArrayBuffer; keys: string[]
  sequences: number[]; quote_seq: number | null; stream: number; ended: boolean
  recording: string; through_seq: number; cutoff_seq: number; subjects: RawSubject[]; streams: number[]
  rows: CachedBlock[]; more: boolean
  mode: string; targets: unknown[]; execution: string; start_observed: boolean; members: unknown[]; fences: unknown[]
  cutoffs: number[]; orders: unknown[]; after_seq: number; resume: boolean; save: boolean
  versions: { seq: number; generation: number }[]
  market: boolean; evaluator: string; links: Link[]; source: string
  scope_key: string; scope_group: string; incarnation: number; revision: number
}

function clearBatch() {
  batch?.free(); batch = undefined
  batchSlots = 0
  book = false; links = []; linkOffset = 0; moreLinks = false; linkSequence = 0; currentLinks = []
  for (const stream of streams.values()) stream.block?.free()
  streams.clear()
  pinned.clear()
}

async function compressed(data: CachedBlock) {
  if (data.key.length > 256) throw Error('REPLAY_CACHE_KEY')
  let bytes = cache.get(data.key)
  if (data.bytes) {
    bytes = new Uint8Array(data.bytes)
    if (bytes.byteLength !== data.block.length || bytes.byteLength > 600 * 1024) throw Error('REPLAY_BLOCK_LENGTH')
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(b => b.toString(16).padStart(2, '0')).join('')
    if (hash !== data.block.sha256) throw Error('REPLAY_BLOCK_HASH')
    const previous = cache.get(data.key)
    if (previous) { cache.delete(data.key); cacheBytes -= previous.byteLength }
    while (cacheBytes + bytes.byteLength > CACHE_LIMIT || cache.size >= 4096) {
      let first: [string, Uint8Array<ArrayBuffer>] | undefined
      for (const entry of cache) { if (!pinned.has(entry[0]) && !rootPins.has(entry[0])) { first = entry; break } }
      if (!first) throw Error('REPLAY_PIN_BUDGET')
      cache.delete(first[0]); cacheBytes -= first[1].byteLength
    }
    cache.set(data.key, bytes); cacheBytes += bytes.byteLength
  }
  if (!bytes) throw Error('REPLAY_CACHE_MISS')
  cache.delete(data.key); cache.set(data.key, bytes)
  return bytes
}

async function cachePage(rows: CachedBlock[], check: () => void) {
  // Pin hits before inserting misses, which may otherwise evict a later row in this page.
  for (const row of rows) pinned.add(row.key)
  for (const row of rows) { await compressed(row); check() }
}

function head(stream: Stream) {
  if (stream.block && stream.sequence === undefined) {
    stream.sequence = stream.block.head()
    if (stream.sequence === undefined) { stream.block.free(); stream.block = undefined }
  }
  if (!stream.block && stream.offset < stream.page.length) {
    const item = stream.page[stream.offset++], bytes = cache.get(item.key)
    if (!bytes) throw Error('REPLAY_CACHE_MISS')
    stream.block = new RawBlock(bytes, item.block.raw_length)
    stream.sequence = stream.block.head()
    pinned.delete(item.key)
  }
  if (!stream.block && stream.offset === stream.page.length && !stream.more) stream.ended = true
  return stream.sequence
}

self.onmessage = async ({ data }: MessageEvent<Request>) => {
  if (data.kind === 'cancel') { canceledThrough = Math.max(canceledThrough, data.id); return }
  let previewBlock: RawBlock | undefined
  try {
    const wasm = await ready
    const check = () => {
      if (data.id <= canceledThrough) throw Error('REPLAY_WORKER_CANCELED')
      if (wasm.memory.buffer.byteLength > WASM_LIMIT) throw Error('REPLAY_WORKER_BUDGET')
    }
    check()
    let output: Record<string, unknown>
    if (data.kind === 'root-begin') { checkpoints.beginRoot(); rootPins.clear(); output = {} }
    else if (data.kind === 'checkpoint' || data.kind === 'scope-revise') {
      if (data.scope_key.length > 128 || data.scope_group.length > 128) throw Error('REPLAY_SCOPE_BUDGET')
      const scope = data.kind === 'scope-revise' ? checkpoints.revise(data.scope_key,data.scope_group) : checkpoints.get(data.scope_key,data.scope_group,data.cutoff_seq)
      output = { after_seq: scope.value ? scope.boundary : 0, incarnation: scope.incarnation, revision: scope.revision }
    } else if (data.kind === 'pin-root') {
      if(data.keys.length>128||data.keys.some(key=>key.length>256||!cache.has(key)))throw Error('REPLAY_PIN_BUDGET')
      for(const key of data.keys)rootPins.add(key)
      output = {}
    } else if (data.kind === 'has') {
      if (data.keys.length > 128 || data.keys.some(key => key.length > 256)) throw Error('REPLAY_CACHE_QUERY')
      output = { present: data.keys.map(key => cache.has(key)) }
    } else if (data.kind === 'interval-begin') {
      interval?.free(); interval = new IntervalAnalysis(JSON.stringify(data)); output = {}
    } else if (data.kind === 'interval-feed') {
      if (!interval) throw Error('REPLAY_INTERVAL_MISSING')
      interval.feed(JSON.stringify(data)); output = {}
    } else if (data.kind === 'interval-second') {
      if (!interval) throw Error('REPLAY_INTERVAL_MISSING')
      interval.begin_second(); output = {}
    } else if (data.kind === 'interval-result') {
      if (!interval) throw Error('REPLAY_INTERVAL_MISSING')
      output = { result: JSON.parse(interval.results()) }; interval.free(); interval = undefined
    } else if (data.kind === 'cache') {
      if (data.rows.length > 128 || data.rows.reduce((n, row) => n + row.block.length, 0) > 1024 * 1024) throw Error('REPLAY_PAGE_BUDGET')
      await cachePage(data.rows, check)
      output = {}
    } else if (data.kind === 'order-reset') {
      orderStates.clear(); output = {}
    } else if (data.kind === 'derive-begin') {
      if (!['owner', 'position', 'statistics', 'fences'].includes(data.mode)) throw Error('REPLAY_DERIVE_MODE')
      derivations.get(data.mode)?.free()
      derivations.set(data.mode, new BusinessDerivation(JSON.stringify(data)))
      output = { needed: derivations.get(data.mode)!.needed() }
    } else if (data.kind === 'derive-feed') {
      const derivation = derivations.get(data.mode)
      if (!derivation) throw Error('REPLAY_DERIVE_MISSING')
      derivation.feed(JSON.stringify(data)); output = { needed: derivation.needed() }
    } else if (data.kind === 'derive-result') {
      const derivation = derivations.get(data.mode)
      if (!derivation) throw Error('REPLAY_DERIVE_MISSING')
      output = { result: JSON.parse(derivation.results()) }
      derivation.free(); derivations.delete(data.mode)
    } else if (data.kind === 'business-begin') {
      clearBatch()
      if (data.streams.length > 16 || new Set(data.streams).size !== data.streams.length) throw Error('REPLAY_STREAM_BUDGET')
      batchSlots = data.subjects.length
      book = data.evaluator === 'book'
      checkpointKey = data.scope_key ?? ''
      checkpoint = checkpointKey ? checkpoints.get(checkpointKey, data.scope_group, data.cutoff_seq) : undefined
      checkpointBoundary = data.cutoff_seq
      if (checkpoint && (checkpoint.incarnation !== data.incarnation || checkpoint.revision !== data.revision)) throw Error('REPLAY_SCOPE_RETIRED')
      if (book && !interval) throw Error('REPLAY_INTERVAL_MISSING')
      if (!book) batch = new BusinessBatch(JSON.stringify({ recording: data.recording, through_seq: data.through_seq, cutoff_seq: data.cutoff_seq, subjects: data.subjects, after_seq: data.after_seq ?? 0,
        states: data.resume ? data.subjects.map(s => orderStates.get(s.id)) : checkpoint?.value ? JSON.parse(checkpoint.value) : [], cutoffs: data.cutoffs ?? [], orders: data.orders ?? [] }))
      for (const id of data.streams) streams.set(id, { page: [], offset: 0, ended: false, more: true })
      versions = []; versionOffset = 0; moreVersions = data.subjects.some(s => s.kind === 'order')
      moreLinks = data.market === true || data.subjects.some(s => s.kind === 'trade')
      output = {}
    } else if (data.kind === 'links') {
      if (linkOffset !== links.length || data.links.length > 128 || data.links.some((row, i, all) => !Number.isSafeInteger(row.seq) || !Number.isSafeInteger(row.object) || row.seq < 1 || row.object < 1 || i > 0 && (row.seq < all[i-1].seq || row.seq === all[i-1].seq && row.object <= all[i-1].object))) throw Error('REPLAY_LINK_PAGE')
      links = data.links; linkOffset = 0; moreLinks = data.more; output = {}
    } else if (data.kind === 'versions') {
      if (versionOffset !== versions.length || data.versions.length > 128 || data.versions.some((v, i, a) => !Number.isSafeInteger(v.seq) || !Number.isSafeInteger(v.generation) || v.generation < 0 || i > 0 && v.seq <= a[i - 1].seq)) throw Error('REPLAY_GENERATION_PAGE')
      versions = data.versions; versionOffset = 0; moreVersions = data.more
      output = {}
    } else if (data.kind === 'business-page') {
      const stream = streams.get(data.stream)
      if ((!batch && !book) || !stream || stream.block || stream.offset < stream.page.length || stream.ended) throw Error('REPLAY_STREAM_STATE')
      const size = data.rows.reduce((n, row) => n + row.block.length, 0)
      if (data.rows.length > 128 || data.rows.length > 1 && size > Math.floor(1024 * 1024 / streams.size)) throw Error('REPLAY_PAGE_BUDGET')
      await cachePage(data.rows, check)
      stream.page = data.rows.map(({ key, block }) => ({ key, block })); stream.offset = 0; stream.more = data.more
      head(stream)
      output = {}
    } else if (data.kind === 'business-step') {
      if (!batch && !book) throw Error('REPLAY_BATCH_MISSING')
      const need: number[] = []
      let needVersions = false
      let needLinks = false
      await yieldTask(); check()
      for (let i = 0; i < 16; i++) {
        check()
        for (const [id, stream] of streams) {
          head(stream)
          if (!stream.ended && !stream.block) need.push(id)
        }
        if (need.length) break
        let selected: Stream | undefined, sequence = Infinity
        for (const stream of streams.values()) {
          const head = stream.sequence
          if (head !== undefined && head < sequence) { selected = stream; sequence = head }
        }
        if (!selected) {
          while (versionOffset < versions.length) batch!.version(versions[versionOffset++].generation)
          needVersions = moreVersions
          if (linkOffset < links.length || currentLinks.length) throw Error('REPLAY_DEPENDENCY_MISSING')
          needLinks = moreLinks
          break
        }
        while (versionOffset < versions.length && versions[versionOffset].seq <= sequence) batch!.version(versions[versionOffset++].generation)
        if (versionOffset === versions.length && moreVersions) { needVersions = true; break }
        if (linkSequence !== sequence) { linkSequence = sequence; currentLinks = [] }
        while (linkOffset < links.length && links[linkOffset].seq <= sequence) {
          if (links[linkOffset].seq !== sequence) throw Error('REPLAY_DEPENDENCY_MISSING')
          currentLinks.push(links[linkOffset++]); if (currentLinks.length > 128) throw Error('REPLAY_LINK_BUDGET')
        }
        if (linkOffset === links.length && moreLinks) { needLinks = true; break }
        if (currentLinks.length) batch!.links(sequence, JSON.stringify(currentLinks))
        if (book) selected.block!.apply_interval_next(interval!)
        else selected.block!.apply_next(batch!)
        selected.sequence = undefined; currentLinks = []; linkSequence = 0
      }
      output = { need, needVersions, needLinks, done: !needVersions && !needLinks && [...streams.values()].every(stream => stream.ended) }
    } else if (data.kind === 'business-result') {
      if ((!batch && !book) || [...streams.values()].some(stream => !stream.ended)) throw Error('REPLAY_BATCH_INCOMPLETE')
      output = { subjects: book ? [] : JSON.parse(batch!.results()) }
      if (data.save) {
        const snapshot = batch!.snapshot()
        if (snapshot.length > 256 * 1024) throw Error('REPLAY_STATE_BUDGET')
        for (const state of JSON.parse(snapshot)) orderStates.set(state.subject.id, state)
        if (orderStates.size > 8) throw Error('REPLAY_STATE_BUDGET')
      }
      if (data.cutoffs?.length) output.fences = JSON.parse(batch!.fences())
      if (checkpoint && batch) {
        const state = batch.snapshot()
        if (state.length <= 256 * 1024) checkpoints.put(checkpointKey, checkpoint, checkpointBoundary, state)
      }
      checkpoint = undefined; checkpointKey = ''
      clearBatch()
    } else {
      const bytes = await compressed(data); check()
      previewBlock = new RawBlock(bytes, data.block.raw_length)
      const request = JSON.stringify({ sequences: data.sequences, quote_seq: data.quote_seq })
      const events: RawPreview[] = []
      for (;;) {
        await yieldTask(); check()
        const step = JSON.parse(previewBlock.step(request)) as { done: boolean; events: RawPreview[] }
        events.push(...step.events)
        if (events.length > 33) throw Error('REPLAY_WORKER_BUDGET')
        if (step.done) break
      }
      output = { events }
    }
    check()
    self.postMessage({ id: data.id, ...output, wasm_bytes: wasm.memory.buffer.byteLength, cache_bytes: cacheBytes, checkpoint_bytes: checkpoints.usage().bytes })
  } catch (cause) {
    const split = batchSlots > 8 && cause instanceof Error && ['REPLAY_BATCH_STATE_BUDGET', 'REPLAY_BATCH_RESULT_BUDGET'].includes(cause.message)
    clearBatch()
    if (split) {
      const wasm = await ready
      self.postMessage({ id: data.id, split: true, wasm_bytes: wasm.memory.buffer.byteLength, cache_bytes: cacheBytes })
    } else self.postMessage({ id: data.id, error: cause instanceof Error ? cause.message : String(cause) })
  } finally { previewBlock?.free(); if (data.kind === 'decode') pinned.delete(data.key) }
}
