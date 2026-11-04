import { OrdersAndTrades, type ReplayDetailTab } from '@/features/replay/ReplayDetails'
import { useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Select } from '@/components/ui/Select'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { recordedDepth } from '@/features/replay/depthGeometry'
import { currentReplayQuote } from '@/features/replay/chartGeometry'
import { replayExecutionAt, replayExecutionScene } from '@/features/replay/executionScene'
import type { ReplayData, ReplayWindow } from '@/features/replay/types'
import '@/styles/theme.css'
import '@/features/replay/replay.css'

const range = { start_us: 0, end_us: 1_000_000 }
const scenarios = [{ value: 'history', label: '大数据拖动 · 一万盘口与五千成交' }, { value: 'records', label: '委托与成交定位' }, { value: 'orders', label: '同价双向挂单' }, { value: 'two', label: '买2 / 卖2' }, { value: 'four', label: '买4 / 卖4' }, { value: 'asym', label: '买2 / 卖7' },
  { value: 'many', label: '买120 / 卖120 · 聚合' }, { value: 'motion', label: '盘口动效' }, { value: 'changing', label: '档数随时间变化' }, { value: 'unknown', label: '未知挂量与零量' }, { value: 'expiry', label: '盘口过期' }, { value: 'gap', label: '盘口缺口' }]

function fixture(scenario: string): ReplayWindow {
  if (scenario === 'history') {
    const messages = [
      ...Array.from({ length: 10000 }, (_, i) => ({ time_us: i * 90, topic: '/quote/SYNTHETIC', data: {
        symbol: 'SYNTHETIC', book_valid: true, available: true, valid_until_us: (i + 1) * 90,
        bids: Array.from({ length: 10 }, (_, j) => ({ price: 100 + i % 20 * .01 - j * .01, quantity: (i + j) % 100 })),
        asks: Array.from({ length: 10 }, (_, j) => ({ price: 100.3 + i % 20 * .01 + j * .01, quantity: (i + j) % 100 })),
      } })),
      ...Array.from({ length: 5000 }, (_, i) => ({ time_us: i * 180, topic: '/market_trade/SYNTHETIC', data: { symbol: 'SYNTHETIC', price: 100.2 + i % 30 * .01, quantity: i % 10 + 1, direction: i % 2 ? 'BUY' : 'SELL', identity_quality: 'stable', trade_key: String(i) } })),
    ].sort((a,b) => a.time_us - b.time_us).map((row, i) => ({ ...row, schema_version: 1, recording_id: 'synthetic', capture_seq: i + 1 }))
    return { recording_id: 'synthetic', window_id: scenario, context: { schema_version: 1, instrument: { SYNTHETIC: { price_tick: .01, price_decimals: 2 } } },
      messages, complete: true, next_cursor: null, coverage: [range], gaps: [], state_before_start: { time_us: 0, state_through_seq: 0 } }
  }
  const dense = scenario === 'many', step = dense ? .0001 : 1
  const depth = (bids: number, asks: number): ReplayData => ({ symbol: 'SYNTHETIC', book_valid: true, available: true, available_depth: 5,
    valid_until_us: scenario === 'expiry' ? 600_000 : 2_000_000,
    bids: Array.from({ length: bids }, (_, index) => ({ price: 100 - index * step, quantity: index + 1 })),
    asks: Array.from({ length: asks }, (_, index) => ({ price: 100 + 2 * step + index * step, quantity: index + 2 })) })
  const frames = scenario === 'changing' ? [depth(2, 2), depth(4, 3), depth(1, 6)] : [depth(dense ? 120 : scenario === 'four' ? 4 : 2, dense ? 120 : scenario === 'asym' ? 7 : scenario === 'four' ? 4 : 2)]
  if (scenario === 'motion') {
    frames.splice(0, frames.length, ...[1, 4, 2].map((amount, index) => ({ ...depth(2, 2),
      bids: [{ price: 100, quantity: amount }, ...(index === 1 ? [{ price: 99.99, quantity: 2 }] : []), { price: 99.98, quantity: 3 }, { price: 95, quantity: 5 }],
      asks: [{ price: 100.01, quantity: 2 }, { price: 100.02, quantity: 3 }, { price: 105, quantity: 5 }],
    })))
  }
  if (scenario === 'unknown') frames[0].bids = [{ price: 100, quantity: null }, { price: 99, quantity: 0 }, { price: 98, quantity: 3 }]
  return { recording_id: 'synthetic', window_id: scenario, context: { schema_version: 1, instrument: { SYNTHETIC: { price_tick: step, price_decimals: dense ? 4 : 2, quantity_decimals: 2 } } },
    complete: true, coverage: [range], gaps: scenario === 'gap' ? [{ start_us: 500_000, end_us: 700_000, topic: 'depth', symbol: 'SYNTHETIC', reason: '合成缺口' }] : [],
    next_cursor: null, state_before_start: { time_us: 0, state_through_seq: 0, orders: scenario === 'orders' ? Object.fromEntries([
      { order_id: 'BUY-A', direction: 'BUY', quantity: 300, filled_quantity: 100, remaining_quantity: 200 },
      { order_id: 'BUY-B', direction: 'BUY', quantity: 50, filled_quantity: 0, remaining_quantity: 50 },
      { order_id: 'SELL-A', direction: 'SELL', quantity: 100, filled_quantity: 0, remaining_quantity: 100 },
    ].map(order => [JSON.stringify(['SYNTHETIC', order.order_id]), { symbol: 'SYNTHETIC', price: 100, status: 'working', confirmed_book: true, sync_generation: 1, ...order }])) : undefined },
    messages: [...frames.map((data, index) => ({ schema_version: 1, recording_id: 'synthetic', time_us: 100_000 + index * 300_000, capture_seq: index + 1, topic: '/quote/SYNTHETIC', data })),
      ...(scenario === 'records' ? ['/order', '/trade'].map((topic, index) => ({ schema_version: 1, recording_id: 'synthetic', capture_seq: frames.length + index + 1, time_us: 400_000 + index * 200_000, topic,
        data: { symbol: 'SYNTHETIC', order_id: 'DEMO-ORDER', trade_key: 'DEMO-TRADE', identity_quality: 'stable', execution_id: 'synthetic', direction: 'BUY', price: 100, quantity: 1, status: 'working' } })) : [])] }
}

export function Acceptance() {
  const [selected, setSelected] = useState<string | null>(null), [selectedTrade, setSelectedTrade] = useState<string | null>(null)
  const [tab, setTab] = useState<ReplayDetailTab>('orders')
  const [scenario, setScenario] = useState('two'), [cursor, setCursor] = useState(800_000)
  const replayWindow = useMemo(() => fixture(scenario), [scenario])
  const overview = useMemo(() => replayExecutionScene([{ range, window: replayWindow }], range, [])!, [replayWindow])
  const projection = useMemo(() => replayExecutionAt(overview, { time_us: cursor }, 'SYNTHETIC'), [overview, cursor])
  const scene = overview.scene
  const quote = currentReplayQuote(projection.quotes.SYNTHETIC, cursor, replayWindow.gaps)
  return <main className="replay-workbench" style={{ height: '100dvh' }}>
    <header className="replay-desk-header"><h1 className="text-lg font-semibold text-ink-1">动态盘口 · 合成验收数据</h1>
      <Select value={scenario} onChange={setScenario} options={scenarios} ariaLabel="验收场景" />
      <p className="text-sm text-ink-2" role="status">{quote ? `已录买 ${quote.bids.length} / 卖 ${quote.asks.length} 档，共 ${recordedDepth(quote).length} 档` : '盘口未知／已失效'}</p>
    </header>
    <OrdersAndTrades projection={projection} scene={scene} context={replayWindow.context} symbol="SYNTHETIC" selected={selected} selectedTradeKey={selectedTrade}
      onSelect={setSelected} onSelectTrade={setSelectedTrade} executionId="synthetic" tab={tab} onTabChange={setTab} onSeek={setCursor} coverage={[range]}>
    <div className="replay-main-chart"><ReplayChart window={replayWindow} range={range} projection={projection} scene={scene} slices={overview.slices} symbol="SYNTHETIC" executionId="synthetic" selectedOrder={selected} selectedTrade={selectedTrade}
      onSelectOrder={setSelected} onSelectTrade={setSelectedTrade} onInspect={() => {}} onSeek={setCursor} firstQuoteControl={<button type="button" className="replay-first-quote cursor-pointer rounded-chip text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent" onClick={() => setCursor(100_000)}>跳到首个盘口</button>} symbolControl={<strong className="text-sm">SYNTHETIC</strong>} /></div>
    </OrdersAndTrades>
    {scenario === 'motion' && <div className="flex gap-4">{[100_000, 400_000, 700_000].map((time, index) => <button key={time} type="button" onClick={() => setCursor(time)}>行情帧 {index + 1}</button>)}</div>}
    <ValueSlider label="合成回放游标" min={0} max={999_999} step={1} value={cursor} onChange={setCursor} formatValue={value => `${(value / 1_000_000).toFixed(3)} 秒`} />
  </main>
}

createRoot(document.getElementById('root')!).render(<Acceptance />)
