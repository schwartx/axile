import { expect, it } from 'bun:test'
import { replayHeatWorker } from './heatWorker'
import type { HeatQuery } from './heatData'
it('one running, one latest pending; stale responses and terminated data never publish', () => {
  const sent: any[] = [], published: any[] = [], errors: string[] = []
  let closed = 0
  const worker: any = { postMessage: (value: unknown) => sent.push(value), terminate: () => closed++ }
  const client = replayHeatWorker(worker, (query, value) => published.push([query, value]), message => errors.push(message))
  const first = {} as HeatQuery, second = {} as HeatQuery, last = {} as HeatQuery
  client.request(first); expect(sent).toHaveLength(0)
  worker.onmessage({ data: { kind: 'ready' } }); expect(sent).toHaveLength(1)
  client.request(second); client.request(last); expect(sent).toHaveLength(1)
  worker.onmessage({ data: { kind: 'result', id: 1, value: 1 } })
  expect(published).toHaveLength(0); expect(sent).toHaveLength(2); expect(sent[1].query).toBe(last)
  worker.onmessage({ data: { kind: 'result', id: 1, value: 1 } }); expect(published).toHaveLength(0)
  worker.onmessage({ data: { kind: 'result', id: 2, value: 2 } }); expect(published).toEqual([[last, 2]])
  client.close(); worker.onmessage({ data: { kind: 'result', id: 2 } }); expect(published).toHaveLength(1); expect(closed).toBe(1)
  expect(errors).toEqual([])
})
it('worker failures terminate computation and expose an error rather than an empty book', () => {
  const errors: string[] = []
  let closed = 0
  const worker: any = { postMessage() {}, terminate: () => closed++ }
  replayHeatWorker(worker, () => { throw Error('must not publish') }, message => errors.push(message))
  worker.onmessage({ data: { kind: 'error', message: '失败' } })
  worker.onerror({}); worker.onmessageerror({})
  expect(errors).toEqual(['失败']); expect(closed).toBe(1)
})
