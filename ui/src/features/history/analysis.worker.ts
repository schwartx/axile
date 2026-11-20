import init, { AccountAnalysis } from '@/features/replay/heat-wasm/axile_replay_heat'

import type { CostView, CostViewQuery, LocalResult } from './localAnalysis'

let overview: Omit<CostView, 'detail'> | undefined
let windowKey: string | undefined

function costView(engine: AccountAnalysis, query: CostViewQuery): CostView {
  const costs = (filters: CostViewQuery['window'] & { symbol?: string; side?: string }): LocalResult =>
    JSON.parse(engine.query(JSON.stringify(filters)))
  const key = JSON.stringify(query.window)
  if (!overview || key !== windowKey) {
    const all = costs(query.window)
    const bySide = (side: string) => new Map(costs({ ...query.window, side }).symbols.map(row => [row.symbol, row.summary]))
    const buys = bySide('buy'), sells = bySide('sell')
    overview = {
      summary: all.summary, unknown: all.unknown,
      rows: all.symbols.map(({ symbol, summary }) => {
        const buy = buys.get(symbol), sell = sells.get(symbol)
        return { symbol, buy, sell, hasUnknownSide: summary.count > (buy?.count ?? 0) + (sell?.count ?? 0) }
      }),
    }
    windowKey = key
  }
  const { symbol, side } = query
  const detail = symbol ? { symbol, side, result: costs({ ...query.window, symbol, side: side || undefined }) } : null
  return { ...overview, detail }
}

let kernel: AccountAnalysis | null = null
self.onmessage = async (event: MessageEvent<{ kind: 'load'; input: string; wasm: ArrayBuffer } | { kind: 'query'; id: number; query: CostViewQuery | { fee_rate: number; contribution?: [string, boolean] } }>) => {
  const request = event.data
  try {
    if (request.kind === 'load') {
      if (!(request.wasm instanceof ArrayBuffer)) throw Error('请先下载本地分析组件')
      await init({ module_or_path: request.wasm })
      kernel?.free()
      overview = undefined
      kernel = new AccountAnalysis(request.input)
      self.postMessage({ kind: 'ready' })
    } else {
      if (!kernel) throw new Error('本地分析输入尚未就绪')
      self.postMessage({ kind: 'result', id: request.id, value: 'window' in request.query ? costView(kernel, request.query) : JSON.parse(kernel.query(JSON.stringify(request.query))) })
    }
  } catch (error) { self.postMessage({ kind: 'error', message: error instanceof Error ? error.message : String(error) }) }
}
