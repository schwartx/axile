import { useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Select } from '../src/components/ui/Select'
import { ReplayChart } from '../src/features/replay/ReplayChart'
import { replayExecutionAt, replayExecutionScene } from '../src/features/replay/executionScene'
import type { ReplayWindow } from '../src/features/replay/types'
import '../src/styles/theme.css'
import '../src/features/replay/replay.css'

const full = { start_us: 0, end_us: 200_000 }
function fixture(scenario: string) {
  const context = { schema_version: 1, instrument: { X: { price_tick: 1 } }, streams: { X: { market_trade: { availability: 'available' as const, source: 'synthetic' } } } }
  const fact = { symbol: 'X', trade_key: 'T', identity_quality: 'stable', price: 100, quantity: scenario === 'completion' ? null : 1 }
  const quote = { symbol: 'X', available: true, book_valid: true, available_depth: 1, valid_until_us: 200_000, bids: [{ price: 100, quantity: 3 }], asks: [{ price: 101, quantity: 1 }] }
  const first: ReplayWindow = { recording_id: 'r', window_id: 'a', context, coverage: [{ start_us: 0, end_us: 100_000 }], gaps: [], complete: true, next_cursor: null, state_before_start: null, messages: [
    { schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 1, topic: '/quote/X', data: quote },
    { schema_version: 1, recording_id: 'r', capture_seq: 2, time_us: 50_000, topic: '/market_trade/X', data: fact },
  ] }
  const second: ReplayWindow = { ...first, window_id: 'b', coverage: [{ start_us: 100_000, end_us: 200_000 }],
    context: scenario === 'metadata' ? { schema_version: 1, streams: { X: { market_trade: { availability: 'unavailable' } } } } : context,
    state_before_start: { time_us: 100_000, state_through_seq: 2, trade_accounting: { market_seen_keys: ['T'], market_records: { T: { data: fact, first_time_us: 50_000, first_capture_seq: 2, conflict: false } } } },
    messages: [
      { schema_version: 1, recording_id: 'r', capture_seq: 3, time_us: 100_000, topic: '/quote/X', data: quote },
      ...(scenario === 'metadata' ? [] : [{ schema_version: 1, recording_id: 'r', capture_seq: 4, time_us: 150_000, topic: '/market_trade/X', data: { ...fact, price: scenario === 'conflict' ? 101 : 100, quantity: scenario === 'completion' ? 2 : 1 } }]),
    ] }
  return { first, overview: replayExecutionScene([{ window: first, range: first.coverage[0] }, { window: second, range: second.coverage[0] }], full, [])! }
}
export function Fixture() {
  const [scenario, setScenario] = useState('conflict')
  const [range, setRange] = useState(full)
  const [cursor, setCursor] = useState(180_000)
  const { first, overview } = useMemo(() => fixture(scenario), [scenario])
  const projection = useMemo(() => replayExecutionAt(overview, { time_us: cursor }, 'X'), [overview, cursor])
  return <main className="min-h-screen bg-bg p-4 text-ink-1">
    <div className="mb-3 flex flex-wrap items-center gap-3"><h1>回放区间统计 · 合成验收</h1>
      <Select ariaLabel="统计验收场景" value={scenario} onChange={value => { setScenario(value); setRange(full) }} options={[{ value: 'conflict', label: '跨窗迟到冲突' }, { value: 'completion', label: '跨窗数量补全' }, { value: 'metadata', label: '选区外元数据缺失' }]} />
      <button type="button" className="min-h-9 rounded border border-line px-3 text-sm" onClick={() => setRange({ start_us: 0, end_us: 100_000 })}>首窗视野</button>
      <button type="button" className="min-h-9 rounded border border-line px-3 text-sm" onClick={() => setRange(full)}>完整视野</button>
    </div>
    <div style={{ height: 680 }}><ReplayChart key={scenario} window={first} range={range} scene={overview.scene} slices={overview.slices} projection={projection} symbol="X" executionId="synthetic" selectedOrder={null} selectedTrade={null} onSelectOrder={() => {}} onSelectTrade={() => {}} onInspect={() => {}} onSeek={setCursor} initialTimeUs={0} />
    </div><p className="mt-2 text-xs text-ink-3">定位游标 {cursor} μs</p>
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
