import { afterEach, expect, test } from 'bun:test'
import { RawReader } from './rawReader'

const originalFetch = globalThis.fetch
const originalWorker = globalThis.Worker
afterEach(() => { globalThis.fetch = originalFetch; globalThis.Worker = originalWorker })

test('raw initialization completes before queries and preserves the bytes for Worker recovery', async () => {
  const requests: any[] = []
  let initialized!: () => void
  globalThis.Worker = class {
    onmessage: ((event: { data: any }) => void) | null = null
    terminate() {}
    postMessage(request: any) {
      requests.push(request)
      if (request.kind === 'init') initialized = () => this.onmessage?.({ data: { kind: 'initialized' } })
      else queueMicrotask(() => this.onmessage?.({ data: { id: request.id, present: [], wasm_bytes: 65536, cache_bytes: 0 } }))
    }
  } as unknown as typeof Worker
  const bytes = new ArrayBuffer(1)
  const phases: boolean[] = []
  const reader = new RawReader('E', () => {}, () => {}, bytes, value => phases.push(value))
  const client = reader as unknown as { workerRequest(request: Record<string, unknown>, signal: AbortSignal): Promise<unknown> }
  const signal = new AbortController().signal
  try {
    const query = client.workerRequest({ kind: 'has', keys: [] }, signal)
    expect(requests.map(request => request.kind)).toEqual(['init'])
    expect(phases).toEqual([true])
    expect(requests[0].wasm).not.toBe(bytes)
    initialized()
    await expect(query).resolves.toMatchObject({ present: [] })
    expect(phases).toEqual([true, false])
    await client.workerRequest({ kind: 'has', keys: [] }, signal)
    expect(requests.map(request => request.kind)).toEqual(['init', 'has', 'has'])
    expect(bytes.byteLength).toBe(1)
  } finally { reader.close() }
})

test('cancel during raw initialization terminates the Worker and rejects before any query', async () => {
  let terminated = false
  const kinds: string[] = []
  globalThis.Worker = class {
    onmessage = null
    terminate() { terminated = true }
    postMessage(request: any) { kinds.push(request.kind) }
  } as unknown as typeof Worker
  const reader = new RawReader('E', () => {}, () => {}, new ArrayBuffer(1))
  const client = reader as unknown as { workerRequest(request: Record<string, unknown>, signal: AbortSignal): Promise<unknown> }
  const abort = new AbortController()
  const result = client.workerRequest({ kind: 'has', keys: [] }, abort.signal)
  abort.abort()
  await expect(result).rejects.toThrow('REPLAY_WORKER_CANCELED')
  expect(terminated).toBeTrue()
  expect(kinds).toEqual(['init'])
  reader.close()
})

test('point quote validity comes from the bounded control fold and preserves an exact same-time event cursor', async () => {
  const requests: any[] = []
  globalThis.Worker = class {
    onmessage: ((event: { data: any }) => void) | null = null
    terminate() {}
    postMessage(q: any) {
      requests.push(q)
      const result = q.kind === 'has' ? { present: [true] } : q.kind === 'decode' ? { events: [{ capture_seq: 1, time_us: 5, topic: '/quote/S', data: { symbol: 'S', book_valid: true, valid_until_us: 100 } }] }
        : q.kind === 'business-step' ? { need: [], needVersions: false, needLinks: false, done: true }
        : q.kind === 'business-result' ? { subjects: [] }
        : q.kind === 'interval-result' ? { result: { book: { spread_valid_us: 0 } } } : {}
      queueMicrotask(() => this.onmessage?.({ data: { id: q.id, wasm_bytes: 65536, cache_bytes: 1, ...result } }))
    }
  } as unknown as typeof Worker
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const q = JSON.parse(String(init?.body ?? '{}')), op = String(input).split('/').at(-1)
    if (op === 'session') return Response.json({ reader_epoch: 'owner', session: 's', recovery_wait_seconds: 0 })
    if (op === 'control') return Response.json({ state: 'settled', session_bytes: 200 })
    if (op === 'manifest') return Response.json({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
    const context = { account_id: 7, execution_id: 'E', recording_id: 'R', through_seq: 3, commit_id: 'c', index_generation: 'g' }
    if (op === 'plan') {
      expect(q.capture_seq).toBe(2)
      return Response.json({ ...context, target_us: 12, cutoff_seq: 2, selected: { seq: 1, block: 1 }, evidence: [],
        blocks: [{ id: 1, length: 1, raw_length: 1, sha256: 'hash' }], planned_bytes: 1,
        previous: { seq: 1, time: 5, block: 1, line: 0 }, next: { seq: 3, time: 12, block: 2, line: 0 } })
    }
    expect(op).toBe('range-blocks')
    return Response.json({ ...context, streams: [], initial: [], range_through: 2 })
  }) as typeof fetch
  let resolve!: (f: any) => void, reject!: (e: Error) => void
  const done = new Promise<any>((r,j) => { resolve = r; reject = j })
  const reader = new RawReader('E', resolve, reject)
  try {
    await reader.manifest()
    reader.seek({ recording: { id: 'R', seq: 3, commit_id: 'c', watermark: 100, start_us: 1, state: 'closed', streams: [{ id: 1, topic: '/quote/S', symbol: 'S' }] }, stream: 1, time_us: 12, capture_seq: 2, generation: 'g' })
    const frame = await done
    expect(frame.quote_valid).toBeFalse()
    expect(frame.target_seq).toBe(2)
    expect(frame.next).toMatchObject({ seq: 3, time: 12 })
    expect(requests.find(q => q.kind === 'interval-begin')).toMatchObject({ cutoff_seq: 2, interval_start_us: 12, interval_end_us: 13 })
  } finally { reader.close() }
})

test('a lost admitted response resumes the same logical root with reserved cost and a new fenced attempt', async () => {
  let opened=0, lost=false, resume: any
  globalThis.fetch=(async(input: string | URL | Request,init?:RequestInit)=>{
    const q=JSON.parse(String(init?.body??'{}')),op=String(input).split('/').at(-1)
    if(op==='session') { opened++; if(q.resume)resume=q.resume; return Response.json({reader_epoch:opened===1?'old':'new',session:'s'+opened,session_proof:'signed',recovery_wait_seconds:0}) }
    if(op==='control') {
      if(lost && q.reader_epoch==='old')return Response.json({detail:{code:'REPLAY_READER_EPOCH',message:'REPLAY_READER_EPOCH'}},{status:409})
      return Response.json({state:'settled',session_bytes:resume?resume.total_bytes+100:0})
    }
    if(op==='manifest')return Response.json({schema_version:2,account_id:7,execution_id:'E',index_generation:'g',recordings:[]})
    if(!lost) { lost=true;throw TypeError('connection lost after admission') }
    expect(q.root_seq).toBe(1); expect(q.attempt).toBe(2)
    return Response.json({account_id:7,execution_id:'E',recording_id:'R',through_seq:3,commit_id:'c',cutoff_seq:3,index_generation:'g',selected:null,evidence:[],blocks:[],planned_bytes:0,target_us:3})
  }) as typeof fetch
  let complete!:(f:any)=>void,fail!:(e:Error)=>void;const done=new Promise<any>((r,j)=>{complete=r;fail=j})
  const reader=new RawReader('E',complete,fail)
  try {
    await reader.manifest()
    reader.seek({recording:{id:'R',seq:3,commit_id:'c',watermark:3,start_us:0,state:'closed',streams:[]},stream:0,time_us:3,generation:'g'})
    const frame=await done
    expect(resume.root_seq).toBe(2);expect(resume.attempt).toBe(2)
    expect(resume.spent_bytes).toBe(256*1024);expect(resume.spent_steps).toBe(200000)
    expect(resume.elapsed_ms).toBeGreaterThanOrEqual(0)
    expect(frame.roots).toBe(2);expect(frame.cost_estimated).toBeTrue()
    expect(frame.cumulative_bytes).toBe(resume.total_bytes+100)
  } finally { reader.close() }
})

test('index generation recovery resolves the stream again and rejects the old numeric cursor within the same root',async()=>{
  const plans:any[]=[], cursors:number[][]=[]
  let generation='g1'
  globalThis.fetch=(async(input:string|URL|Request,init?:RequestInit)=>{
    const q=JSON.parse(String(init?.body??'{}')),op=String(input).split('/').at(-1)
    if(op==='session')return Response.json({reader_epoch:'owner',session:'s',session_proof:'signed',recovery_wait_seconds:0})
    if(op==='control')return Response.json(q.operation==='status'?{state:'stopped',bytes:40,index_steps:100,session_bytes:40}:{state:'settled',session_bytes:100})
    if(op==='manifest')return Response.json({schema_version:2,account_id:7,execution_id:'E',index_generation:generation,recordings:[{id:'R',seq:3,commit_id:'c',watermark:3,start_us:0,state:'closed',streams:[{id:generation==='g1'?1:9,topic:'/quote/S',symbol:'S'}]}]})
    if(op==='objects'){cursors.push(q.after);return Response.json({account_id:7,execution_id:'E',recording_id:'R',through_seq:3,commit_id:'c',index_generation:'g2',subjects:[],next:null})}
    if(op==='business-blocks')return Response.json({account_id:7,execution_id:'E',recording_id:'R',through_seq:3,commit_id:'c',index_generation:'g2',blocks:[],next:null})
    plans.push(q)
    if(q.index_generation==='g1'){generation='g2';return Response.json({detail:{code:'REPLAY_INDEX_GENERATION',message:'REPLAY_INDEX_GENERATION'}},{status:409})}
    return Response.json({account_id:7,execution_id:'E',recording_id:'R',through_seq:3,commit_id:'c',cutoff_seq:3,index_generation:'g2',selected:null,evidence:[],blocks:[],planned_bytes:0,target_us:3})
  })as typeof fetch
  let complete!:(f:any)=>void,fail!:(e:Error)=>void;const done=new Promise<any>((r,j)=>{complete=r;fail=j});const reader=new RawReader('E',f=>complete(f),e=>fail(e))
  try{await reader.manifest();const target={recording:{id:'R',seq:3,commit_id:'c',watermark:3,start_us:0,state:'closed',streams:[{id:1,topic:'/quote/S',symbol:'S'}]},stream:1,time_us:3,generation:'g1'};reader.seek({...target,business:{kind:'trade',after:[1,4]}});const frame=await done
    expect(frame.index_generation).toBe('g2')
    const next=new Promise<any>((r,j)=>{complete=r;fail=j});reader.seek({...target,business:{kind:'trade',after:[2,8],cursor_generation:frame.index_generation}});await next
    expect(plans.map(q=>[q.root_seq,q.attempt,q.stream,q.index_generation])).toEqual([[2,1,1,'g1'],[2,2,9,'g2'],[3,1,9,'g2']])
    expect(cursors).toEqual([[0,0],[2,8]])
  }finally{reader.close()}
})

test('refreshing a frozen manifest reuses its session and advances settled roots', async () => {
  let opens = 0
  const roots: number[] = []
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const request = JSON.parse(String(init?.body ?? '{}')), operation = String(input).split('/').at(-1)
    if (operation === 'session') { opens++; return Response.json({ reader_epoch: 'owner', session: 'session', recovery_wait_seconds: 0 }) }
    if (operation === 'control') return Response.json({ state: 'settled', session_bytes: 0 })
    roots.push(request.root_seq)
    return Response.json({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
  }) as typeof fetch
  const reader = new RawReader('E', () => {}, cause => { throw cause })
  try { await reader.manifest(); await reader.manifest(); await reader.manifest() }
  finally { reader.close() }
  expect(opens).toBe(1)
  expect(roots).toEqual([1, 2, 3])
})

test('a failed session open can be retried without retaining a busy refresh', async () => {
  let opens = 0
  globalThis.fetch = (async (input: string | URL | Request) => {
    const operation = String(input).split('/').at(-1)
    if (operation === 'session') {
      if (++opens === 1) throw Error('offline')
      return Response.json({ reader_epoch: 'owner', session: 'session', recovery_wait_seconds: 0 })
    }
    if (operation === 'control') return Response.json({ state: 'settled' })
    return Response.json({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
  }) as typeof fetch
  const reader = new RawReader('E', () => {}, () => {})
  try {
    await expect(reader.manifest()).rejects.toThrow('offline')
    expect((await reader.manifest()).account_id).toBe(7)
    expect(opens).toBe(2)
  } finally { reader.close() }
})

test('a seek during refresh waits for the aborted manifest root to settle', async () => {
  let started!: () => void, completed!: () => void
  const refreshing = new Promise<void>(r => { started = r }), done = new Promise<void>(r => { completed = r })
  const calls: string[] = []
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const request = JSON.parse(String(init?.body ?? '{}')), operation = String(input).split('/').at(-1)
    calls.push(`${operation === 'control' ? request.operation : operation}:${request.root_seq}`)
    if (operation === 'session') return Response.json({ reader_epoch: 'owner', session: 'session', recovery_wait_seconds: 0 })
    if (operation === 'control') return Response.json({ state: 'settled' })
    if (operation === 'manifest') {
      if (request.root_seq === 2) { started(); return new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(Error('aborted')), { once: true })) }
      return Response.json({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
    }
    return Response.json({ account_id: 7, execution_id: 'E', recording_id: 'R', through_seq: 3, commit_id: 'c', cutoff_seq: 3, index_generation: 'g', selected: null, evidence: [], blocks: [], planned_bytes: 0, target_us: 3 })
  }) as typeof fetch
  const reader = new RawReader('E', () => completed(), cause => { throw cause })
  try {
    await reader.manifest()
    const refresh = reader.manifest().catch(e => e)
    await refreshing
    reader.seek({ recording: { id: 'R', commit_id: 'c', seq: 3, watermark: 3, start_us: 1, state: 'closed', streams: [] }, stream: 0, time_us: 3, generation: 'g' })
    expect((await refresh).message).toBe('aborted')
    await done
    expect(calls.indexOf('ack:2')).toBeLessThan(calls.indexOf('plan:3'))
  } finally { reader.close() }
})

test('wide dependencies and large results split within the same root and keep every scalar contribution', async () => {
  const batches: number[][] = [], roots = new Set<number>()
  let usage=0
  const reply=(body:unknown)=>Response.json(body,{headers:{'X-Replay-Usage':JSON.stringify({bytes:++usage*100,index_steps:usage*100,session_bytes:usage*100})}})
  globalThis.Worker = class {
    onmessage: ((event: { data: Record<string, unknown> }) => void) | null = null
    subjects: Record<string, unknown>[] = []
    owners: Record<string, unknown>[] = []
    quantity = 0
    terminate() {}
    postMessage(data: Record<string, any>) {
      let output: Record<string, unknown> = {}
      if (data.kind === 'checkpoint') output = { after_seq: 0, incarnation: 1, revision: 1 }
      if (data.kind === 'business-begin') this.subjects = data.subjects
      if (data.kind === 'business-step') output = { done: true, need: [], needVersions: false }
      if (data.kind === 'business-result') output = this.subjects.length > 8 ? { split: true } : { subjects: this.subjects.map(s => ({ ...s, data: { execution_id: 'E', start_observed: true } })), fences: [] }
      if (data.kind === 'derive-begin' && data.mode === 'owner') this.owners = data.targets
      if (data.kind === 'derive-feed' && data.mode === 'statistics') this.quantity += data.subjects.length
      if (data.kind === 'derive-result') output = { result: data.mode === 'owner' ? this.owners : { known_quantity: this.quantity, complete: true } }
      if (data.kind === 'interval-result') output = { result: { book: { own_capture_us: 200 } } }
      queueMicrotask(() => this.onmessage?.({ data: { id: data.id, wasm_bytes: 65536, cache_bytes: 0, ...output } }))
    }
  } as unknown as typeof Worker
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const request = JSON.parse(String(init?.body ?? '{}')), operation = String(input).split('/').at(-1)
    if (operation === 'session') return Response.json({ reader_epoch: 'owner', session: 'session', recovery_wait_seconds: 0 })
    if (operation === 'manifest') return reply({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
    if (operation === 'control') return Response.json({ state: 'settled', session_bytes: 0 })
    roots.add(request.root_seq)
    const context = { account_id: 7, execution_id: 'E', recording_id: 'R', through_seq: 200, commit_id: 'c', index_generation: 'g' }
    if (operation === 'plan') return reply({ ...context, cutoff_seq: 200, selected: null, evidence: [], blocks: [], planned_bytes: 0, target_us: 200 })
    if (operation === 'objects') {
      const subjects = request.kind === 'execution' ? [{ id: 1, kind: 'execution', key: 'E', first_seq: 1 }] : request.kind === 'trade' ? Array.from({ length: 128 }, (_, i) => ({ id: i + 2, kind: 'trade', key: `T${i}`, first_seq: i + 2 })) : []
      return reply({ ...context, subjects, next: null })
    }
    if (operation === 'range-blocks') return reply({ ...context, streams: [], initial: [] })
    if (operation === 'business-blocks') return reply({ ...context, blocks: [], next: null })
    if (operation === 'commands') return reply({ ...context, subjects: [], next: null })
    if (operation === 'object-streams') {
      if (request.objects.length > 16) return Response.json({ detail: 'REPLAY_DEPENDENCY_WORKING_SET' }, { status: 413 })
      if (request.objects[0] !== 1) batches.push(request.objects)
      return reply({ ...context, streams: [], initial: [] })
    }
    throw Error('unexpected ' + operation)
  }) as typeof fetch
  let resolve!: (quantity: unknown) => void, reject!: (cause: Error) => void
  const result = new Promise((r, j) => { resolve = r; reject = j })
  const reader = new RawReader('E', f => resolve(f.business?.subjects[0].data.known_quantity), reject)
  try {
    await reader.manifest()
    reader.seek({ recording: { id: 'R', commit_id: 'c', seq: 200, watermark: 200, start_us: 1, state: 'closed', streams: [], symbols: ['S'] }, stream: 0, symbol: 'S', time_us: 200, generation: 'g', business: { kind: 'statistics', after: [0, 0] } })
    expect(await result).toBe(128)
    expect(batches.filter(b => b.length === 8).flat()).toEqual(Array.from({ length: 128 }, (_, i) => i + 2))
    expect(batches.filter(b=>b.length===16).length).toBeGreaterThanOrEqual(8)
    expect([...roots]).toEqual([2])
  } finally { reader.close() }
})

test('rapid seeks settle the old root, retry lost ACK, and start only the latest candidate', async () => {
  const calls: { operation: string; root: number; time?: number }[] = []
  let started!: () => void, completed!: () => void
  const firstPlan = new Promise<void>(resolve => { started = resolve })
  const done = new Promise<void>(resolve => { completed = resolve })
  let lostAck = false
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input)
    expect(url).toStartWith('/api/v1/account/executions/E/replay/raw/')
    const request = JSON.parse(String(init?.body ?? '{}'))
    const operation = url.split('/').at(-1)!
    calls.push({ operation: operation === 'control' ? request.operation : operation, root: request.root_seq, time: request.time_us })
    if (operation === 'session') return Response.json({ reader_epoch: 'owner', session: 'session', recovery_wait_seconds: 0 })
    if (operation === 'manifest') return Response.json({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
    if (operation === 'control') {
      if (request.operation === 'ack' && request.root_seq === 2 && !lostAck) {
        lostAck = true
        throw Error('receipt lost after server applied ACK')
      }
      return Response.json({ state: request.root_seq === 2 && lostAck ? 'retired' : 'settled', session_bytes: request.root_seq * 100 })
    }
    if (request.time_us === 1) {
      started()
      return new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(Error('aborted')), { once: true }))
    }
    return Response.json({ account_id: 7, execution_id: 'E', recording_id: 'R', through_seq: 3, commit_id: 'c', cutoff_seq: 3, index_generation: 'g', selected: null, evidence: [], blocks: [], planned_bytes: 0, target_us: request.time_us })
  }) as typeof fetch
  const frames: number[] = []
  const reader = new RawReader('E', frame => { frames.push(frame.target_us); completed() }, cause => { throw cause })
  await reader.manifest()
  const recording = { id: 'R', commit_id: 'c', seq: 3, watermark: 3, start_us: 0, state: 'closed', streams: [] }
  reader.seek({ recording, stream: 0, time_us: 1, generation: 'g' })
  await firstPlan
  reader.seek({ recording, stream: 0, time_us: 2, generation: 'g' })
  reader.seek({ recording, stream: 0, time_us: 3, generation: 'g' })
  await done
  expect(frames).toEqual([3])
  expect(calls.filter(call => call.operation === 'plan').map(call => call.time)).toEqual([1, 3])
  const next = calls.findIndex(call => call.operation === 'plan' && call.root === 3)
  expect(calls.slice(0, next).filter(call => call.operation === 'ack' && call.root === 2)).toHaveLength(2)
  expect(calls.some(call => call.operation === 'cancel' && call.root === 2)).toBeTrue()
  reader.close()
})

test('a synchronous Worker send failure releases the callback and settles exactly once', async () => {
  let terminated = 0
  globalThis.Worker = class {
    postMessage() { throw Error('send failed') }
    terminate() { terminated++ }
  } as unknown as typeof Worker
  const controls: string[] = []
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const request = JSON.parse(String(init?.body ?? '{}')), operation = String(input).split('/').at(-1)
    if (operation === 'session') return Response.json({ reader_epoch: 'owner', session: 'session', recovery_wait_seconds: 0 })
    if (operation === 'manifest') return Response.json({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
    if (operation === 'control') { controls.push(request.operation); return Response.json({ state: 'settled', session_bytes: 0 }) }
    return Response.json({ account_id: 7, execution_id: 'E', recording_id: 'R', through_seq: 3, commit_id: 'c', cutoff_seq: 3, index_generation: 'g', selected: null, evidence: [], blocks: [{ id: 1, length: 1 }], planned_bytes: 1, target_us: 3 })
  }) as typeof fetch
  let fail!: (error: Error) => void
  const failure = new Promise<Error>(resolve => { fail = resolve })
  const reader = new RawReader('E', () => { throw Error('unexpected frame') }, fail)
  await reader.manifest()
  reader.seek({ recording: { id: 'R', commit_id: 'c', seq: 3, watermark: 3, start_us: 0, state: 'closed', streams: [] }, stream: 1, time_us: 3, generation: 'g' })
  expect((await failure).message).toBe('send failed')
  reader.close()
  expect(terminated).toBe(1)
  expect(controls.filter(operation => operation === 'cancel')).toHaveLength(1)
  expect(controls.filter(operation => operation === 'ack')).toHaveLength(2)
})

test('rate waits retry the same root and attempt without resetting its budget', async () => {
  const roots: number[] = []
  let complete!: () => void
  const done = new Promise<void>(resolve => { complete = resolve })
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? '{}')), operation = String(input).split('/').at(-1)
    if (operation === 'session') return Response.json({ reader_epoch: 'owner', session: 'session', recovery_wait_seconds: 0 })
    if (operation === 'manifest') return Response.json({ schema_version: 2, account_id: 7, execution_id: 'E', index_generation: 'g', recordings: [] })
    if (operation === 'control') return Response.json({ state: 'settled', session_bytes: 0 })
    roots.push(body.root_seq)
    expect(body.attempt).toBe(1)
    if (roots.length === 1) return Response.json({ detail: { message: 'REPLAY_INDEX_RATE' } }, { status: 429 })
    return Response.json({ account_id: 7, execution_id: 'E', recording_id: 'R', through_seq: 3, commit_id: 'c', cutoff_seq: 3, index_generation: 'g', selected: null, evidence: [], blocks: [], planned_bytes: 0, target_us: 3 })
  }) as typeof fetch
  const reader = new RawReader('E', () => complete(), cause => { throw cause })
  await reader.manifest()
  reader.seek({ recording: { id: 'R', commit_id: 'c', seq: 3, watermark: 3, start_us: 0, state: 'closed', streams: [] }, stream: 0, time_us: 3, generation: 'g' })
  await done
  reader.close()
  expect(roots).toEqual([2, 2])
})
