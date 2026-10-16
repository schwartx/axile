import { useState } from 'react'
import { Segmented } from '@/components/ui/Segmented'
import { withViewTransition } from '@/lib/viewTransition'
import { amount, quantityText } from '@/features/history/costs'
import { snapshotPositions, quantityUnit } from '@/features/history/executionEvidenceModel'
import type { ExecutionDetailModel } from '@/features/account/executionDetail'
import type { ChannelCapability } from '@/types/api'

const actions = { open: '建仓', close: '平仓', increase: '加仓', reduce: '减仓', flip: '翻向', aligned: '持仓不变', skipped: '跳过', failed: '未完成' }
const tableClass = 'w-full whitespace-nowrap text-left text-xs [&_td]:px-3 [&_td]:py-1.5 [&_th]:px-3 [&_th]:py-1.5'

export function ExecutionEvidence({ model, units, currency, view: controlledView, onViewChange }: { model: ExecutionDetailModel; units?: ChannelCapability['units']; currency: string; view?: 'actions' | 'positions'; onViewChange?: (view: 'actions' | 'positions') => void }) {
  const [localView, setLocalView] = useState<'actions' | 'positions'>('actions')
  const view = controlledView ?? localView
  const setView = onViewChange ?? setLocalView
  const before = snapshotPositions(model.artifacts, 'account_snapshot_before')
  const after = snapshotPositions(model.artifacts, 'account_snapshot')
  const involved = model.symbols.filter(s => s.action !== 'aligned' || s.filled !== 0 || s.broken || s.reached === false || s.orders.some(o => (o.filled_volume ?? 0) > 0))
  const hasFill = model.symbols.some(s => s.filled !== 0 || s.orders.some(o => (o.filled_volume ?? 0) > 0))
  const flat = after?.length === 0
  const state = flat ? hasFill ? '执行后空仓' : before?.length === 0 ? '持续空仓' : '执行后空仓' : after ? `执行后持仓 ${new Set(after.map(p => p.symbol)).size} 个品种` : '持仓状态未知'
  return <div data-testid="execution-evidence" className="pb-2">
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><span className="text-sm">{state}</span><Segmented size="sm" value={view} options={[{ value: 'actions', label: '本次调仓' }, { value: 'positions', label: '全部持仓' }]} onChange={value => withViewTransition(() => setView(value))} /></div>
    <div className="overflow-x-auto">{view === 'positions' ? after == null ? <p className="py-3 text-ink-3">执行后持仓快照缺失或不可用</p> : after.length === 0 ? <p className="py-3 text-ink-3">执行后空仓</p> : <table className={tableClass}><thead className="text-ink-3"><tr><th>品种</th><th>方向</th><th>执行后持仓</th></tr></thead><tbody>{after.map((p, i) => <tr key={`${p.symbol}:${i}`} className="border-t border-line"><td>{p.symbol}</td><td>{p.direction || '—'}</td><td>{quantityText(p.volume)} {quantityUnit(units, p.symbol, currency)}</td></tr>)}</tbody></table>
      : <><table className={tableClass}><thead className="text-ink-3"><tr><th>品种 / 动作</th><th>执行前 → 目标 → 执行后</th><th>距目标差额</th><th>计划净交易 / 实际净成交</th><th>到达中间价 → 成交均价</th><th>滑点损耗 BP</th><th>结果</th></tr></thead><tbody>{involved.map(s => {
        const unit = quantityUnit(units, s.symbol, currency)
        const qty = (n: number | null) => `${quantityText(n)}${unit ? ` ${unit}` : ''}`
        const loss = s.tca?.slippage_bps == null ? null : -s.tca.slippage_bps
        return <tr key={s.symbol} className="border-t border-line"><td>{s.symbol}<small className="block text-ink-3">{s.observedBefore != null && s.observedAfter != null ? actions[s.action] : '持仓变化未知'}</small></td><td>{qty(s.observedBefore)} → {qty(s.target)} → {qty(s.observedAfter)}</td><td>{qty(s.target != null && s.observedAfter != null ? s.target - s.observedAfter : null)}</td><td>{qty(s.observedBefore != null && s.target != null ? s.target - s.observedBefore : null)} / {qty(s.filled)}</td><td>{amount(s.tca?.arrival_mid ?? null)} → {amount(s.avgPrice)}</td><td className={loss == null || loss === 0 ? '' : loss > 0 ? 'text-warn' : 'text-accent'}>{amount(loss)}</td><td className={`max-w-56 whitespace-normal break-words ${s.broken ? 'text-warn' : 'text-ink-3'}`}>{s.reason || (s.reached === true ? '已到位' : s.reached === false ? '未到位' : '到位状态未知')}</td></tr>
      })}</tbody></table>{!involved.length && <p className="py-3 text-ink-3">{model.hasReconciliation ? flat && before?.length === 0 ? '空仓 · 本次无需交易' : '本次无调仓动作' : '历史记录无逐品种对账证据'}</p>}</>}
    </div>
  </div>
}
