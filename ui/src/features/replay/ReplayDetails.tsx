import { Fragment, type ReactNode } from 'react'
import { Segmented } from '@/components/ui/Segmented'
import { withViewTransition } from '@/lib/viewTransition'
import { OverflowText } from '@/components/ui/OverflowText'
import { replayTradeKey } from '@/features/replay/chartInteraction'
import { replayOrderPositionContext, replayTradeLiquidity } from '@/features/replay/depthGeometry'
import { replayOwnership } from '@/features/replay/ownership'
import { replayCommandPresentation, replayFieldText, replayInstrument, replayNumber, replayTime, replayStatusLabels as statusLabels } from '@/features/replay/presentation'
import type { ReplayContext, ReplayGap, ReplayOrder, ReplayProjection, ReplayRange, ReplayTrade } from '@/features/replay/types'

export type ReplayDetailTab = 'orders' | 'trades' | 'commands' | 'positions'

const directionText = (value: string | null) => value === 'BUY' ? '买' : value === 'SELL' ? '卖' : '方向未知'
const offsetText = (value: string | null) => ({ OPEN: '开', CLOSE: '平', CLOSETODAY: '平今', CLOSEYESTERDAY: '平昨' } as Record<string, string>)[value ?? ''] ?? ''

function RecordedFields({ value, context, kind, executionId, position }: { value: ReplayOrder | ReplayTrade; context: ReplayContext; kind: 'order' | 'trade'; executionId: string; position?: { label: string; time: number | null } }) {
  const ownership = replayOwnership(value, executionId)
  const fields = context.display?.[kind] ?? []
  const rows = fields.flatMap(field => { const text = replayFieldText(value.attributes[field.key], field); return text == null ? [] : [{ ...field, text }] })
  const identities = Object.entries(value.native_identity).filter(([, raw]) => typeof raw === 'string' || typeof raw === 'number')
  const instrument = replayInstrument(context, value.symbol)
  const fieldsId = `replay-${kind}-fields-${value.capture_seq}-${encodeURIComponent(value.key ?? '')}`
  return <div className="replay-recorded-fields">
    <button type="button" popoverTarget={fieldsId} className="replay-fields-trigger text-accent">完整记录</button>
    {'observed_status' in value && value.status === 'unknown' && value.observed_status && value.observed_status !== value.status && <p className="mt-2 text-[12px] text-warn">完整委托查询中未出现，当前状态未知；最近观察：{statusLabels[value.observed_status] ?? value.observed_status}。</p>}
    {position && <p className="replay-order-position text-ink-2" title={position.time == null ? '缺少挂单时的盘口观察' : `盘口判断时刻 ${replayTime(position.time)}；价格档位不等于队列位置`}>{position.label} · 排队未知</p>}
    <div id={fieldsId} popover="auto" className="replay-information bg-surface text-ink-1" aria-label="选中记录的完整字段">
      <div className="mb-4 flex items-center justify-between gap-4"><h2 className="text-sm font-medium">{kind === 'order' ? '委托' : '成交'} · {value.order_id ?? '尚未关联委托'}</h2><button type="button" popoverTarget={fieldsId} popoverTargetAction="hide" className="min-h-9 px-2 text-accent">关闭</button></div>
    <p className="text-xs text-ink-2">归属：{ownership.label}</p>
    <dl className="replay-order-summary num">
      {'status' in value ? <>
        <div><dt>当时状态</dt><dd>{value.cancel_pending ? '撤单待确认' : statusLabels[value.status ?? 'unknown'] ?? '状态未知'}</dd></div>
        <div><dt>买卖 / 开平</dt><dd>{directionText(value.direction)} {offsetText(value.offset)}</dd></div>
        <div><dt>委托价格</dt><dd>{replayNumber(value.price, instrument.price_decimals)}</dd></div>
        <div><dt>已成 / 委托</dt><dd>{replayNumber(value.filled_volume, instrument.quantity_decimals)} / {replayNumber(value.volume, instrument.quantity_decimals)}</dd></div>
        {position && <><div><dt>盘口位置</dt><dd>{position.label}</dd></div><div><dt>盘口判断时刻</dt><dd>{position.time == null ? '未记录' : replayTime(position.time)}</dd></div><div><dt>排队顺序</dt><dd>未知，未录到逐笔队列证据</dd></div></>}
      </> : <>
        <div><dt>成交时间</dt><dd>{replayTime(value.time_us)}</dd></div>
        <div><dt>成交价格</dt><dd>{replayNumber(value.price, instrument.price_decimals)}</dd></div>
        <div><dt>成交数量</dt><dd>{replayNumber(value.quantity, instrument.quantity_decimals)}</dd></div>
        <div><dt>主被动</dt><dd>{replayTradeLiquidity(value)}</dd></div>
      </>}
    </dl>
    {rows.length > 0 && <details open className="replay-native-fields"><summary>原生字段 · {rows.length}</summary><dl className="mt-3 grid gap-y-2 text-[13px] text-ink-2">
      {rows.map(field => <div key={field.key} className="flex min-w-0 justify-between gap-3"><dt>{field.group ? `${field.group} · ` : ''}{field.label}</dt><dd className="num break-all text-right">{field.text}</dd></div>)}
    </dl></details>}
    {!rows.length && <p className="mt-2 text-[12px] text-ink-2">本条记录没有额外的已声明字段。</p>}
    {ownership.executionId && <details className="mt-3 text-[12px] text-ink-2"><summary className="cursor-pointer focus-visible:outline-2 focus-visible:outline-accent">执行身份</summary><p className="mt-2 break-all">{ownership.label} · 执行 <span className="num select-text">{ownership.executionId}</span></p></details>}
    {identities.length > 0 && <details className="mt-3 text-[12px] text-ink-2"><summary className="w-fit cursor-pointer focus-visible:outline-2 focus-visible:outline-accent">原生身份</summary><dl className="mt-2 grid gap-2 sm:grid-cols-2">{identities.map(([key, raw]) => <div key={key}><dt>{key}</dt><dd className="num select-text break-all text-ink-2">{String(raw)}</dd></div>)}</dl></details>}
    </div>
  </div>
}

export function OrdersAndTrades({ projection, context, symbol, selected, onSelect, selectedTradeKey, onSelectTrade, executionId, tab, onTabChange, onSeek, coverage, gaps = [], selectionPending = false, children }: { selectedTradeKey: string | null; onSelectTrade: (key: string) => void; children?: ReactNode; onTabChange: (tab: ReplayDetailTab) => void; tab: ReplayDetailTab; onSeek: (time: number) => void; coverage: ReplayRange[]; gaps?: ReplayGap[]; selectionPending?: boolean; projection: ReplayProjection; context: ReplayContext; symbol: string; selected: string | null; onSelect: (key: string) => void; executionId: string }) {
  const instrument = replayInstrument(context, symbol), decimals = instrument.quantity_decimals ?? 6
  const orders = Object.values(projection.orders).filter(order => order.symbol === symbol)
  const trades = projection.ownTrades.filter(trade => trade.symbol === symbol)
  const selectedTrade = selectedTradeKey == null ? trades.at(-1) : trades.find(trade => replayTradeKey(trade) === selectedTradeKey)
  const commands = projection.commands.filter(command => !command.symbol || command.symbol === symbol).slice(-6)
  return <div className="replay-stage">
    <div className="replay-chart-column">
      <div className="replay-chart-area">{children}</div>
      <section className="replay-dock" aria-label="回放明细">
        <div className="replay-dock-header">
          <Segmented value={tab} options={[{ value: 'orders', label: '委托' }, { value: 'trades', label: '成交' }, { value: 'commands', label: '报撤观察' }, { value: 'positions', label: '持仓' }]} onChange={value => withViewTransition(() => onTabChange(value))} size="sm" />
        </div>
        <div className="replay-detail-panels" data-tab={tab}>
    <section className="replay-order-section" aria-label="游标处自有委托">
      <div className="sr-only"><h2 className="font-semibold text-ink-1">自己的委托</h2><span className="text-[12px] text-ink-2">选择一张单查看轨迹</span></div>
      <div className="replay-detail-list"><table className="num w-full text-left text-[12px]"><thead className="text-ink-2"><tr><th className="pb-2 font-medium">委托</th><th className="pb-2 font-medium">归属</th><th className="pb-2 font-medium">买卖 / 开平</th><th className="pb-2 text-right font-medium">价格</th><th className="pb-2 text-right font-medium">已成 / 委托</th><th className="pb-2 pl-3 font-medium">当时状态</th></tr></thead><tbody>
        {orders.map(order => <Fragment key={order.key}><tr onClick={() => onSelect(order.key)} className={`border-t border-line ${order.key === selected ? 'bg-accent-soft' : ''}`}>
          <td className="py-2 pr-3"><button type="button" aria-pressed={order.key === selected} aria-expanded={order.key === selected} aria-controls={`replay-order-extra-${encodeURIComponent(order.key)}`} className="min-h-9 max-w-36 cursor-pointer text-left text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-accent"><OverflowText text={order.order_id} className="block max-w-36" /></button></td>
          <td className="whitespace-nowrap pr-3 text-ink-2">{replayOwnership(order, executionId).label}</td>
          <td className="whitespace-nowrap">{directionText(order.direction)} {offsetText(order.offset)}</td><td className="pl-3 text-right">{replayNumber(order.price, instrument?.price_decimals)}</td><td className="whitespace-nowrap pl-3 text-right">{replayNumber(order.filled_volume, decimals)} / {replayNumber(order.volume, decimals)}</td><td className="pl-3"><span className={order.cancel_pending || !order.confirmed && !order.terminal ? 'text-warn' : ''}>{order.cancel_pending ? '撤单待确认' : statusLabels[order.status ?? 'unknown'] ?? '状态未知'}</span>{order.carry_in && <small className="block text-ink-2">窗口前已存在</small>}</td>
        </tr><tr className="replay-expanded-row"><td colSpan={6}>
          <div id={`replay-order-extra-${encodeURIComponent(order.key)}`} className="replay-row-expansion" data-open={order.key === selected} inert={order.key !== selected}>
            <div><RecordedFields value={order} context={context} kind="order" executionId={executionId} position={replayOrderPositionContext(order, projection, gaps)} /></div>
          </div>
        </td></tr></Fragment>)}
      </tbody></table></div>
      {selectionPending && <p className="px-3 py-2 text-xs text-ink-2">选中委托在游标处尚未观察到。</p>}
      {!orders.length && <p className="py-4 text-[13px] text-ink-2">此时尚未观察到相关委托。</p>}


    </section>
    <section className="replay-trade-section" aria-label="当前游标已知的自己的成交">
      <div className="sr-only"><h2 className="font-semibold text-ink-1">自己的成交</h2><span className="text-[12px] text-ink-2">独立于市场成交圆点</span></div>
      <div className="replay-detail-list">{trades.map(trade => <div key={replayTradeKey(trade)}><button type="button" onClick={() => withViewTransition(() => onSelectTrade(replayTradeKey(trade)))} aria-pressed={trade === selectedTrade} aria-expanded={trade === selectedTrade} aria-controls={`replay-trade-extra-${encodeURIComponent(replayTradeKey(trade))}`} className={`grid min-h-11 w-full grid-cols-[minmax(0,1fr)_auto] gap-x-3 border-t border-line py-2 text-left text-[12px] focus-visible:outline-2 focus-visible:outline-accent ${trade === selectedTrade ? 'bg-accent-soft' : 'hover:bg-fill'}`}><span className="num text-ink-2">{replayTime(trade.time_us)}<span className="block text-ink-2">{trade.order_id ?? '委托待关联'} · {directionText(trade.direction)} · {replayOwnership(trade, executionId).label}</span></span><span className="num text-right text-ink-1">{replayNumber(trade.quantity, decimals)} {instrument?.quantity_label} @ {replayNumber(trade.price, instrument?.price_decimals)}{trade.identity_quality !== 'stable' && <span className="block text-warn">身份未确认，不作为确定累计</span>}</span></button>
        <div id={`replay-trade-extra-${encodeURIComponent(replayTradeKey(trade))}`} className="replay-row-expansion" data-open={trade === selectedTrade} inert={trade !== selectedTrade}><div>
          <RecordedFields value={trade} context={context} kind="trade" executionId={executionId} />
          <p className="text-xs text-ink-2">{replayTradeLiquidity(trade)}</p>
          <button type="button" className="replay-trade-seek text-accent disabled:text-ink-2" disabled={!coverage.some(range => trade.time_us >= range.start_us && trade.time_us < range.end_us)} onClick={() => onSeek(trade.time_us)}>{coverage.some(range => trade.time_us >= range.start_us && trade.time_us < range.end_us) ? '定位此成交' : '成交时刻未被覆盖'}</button>
        </div></div>
      </div>)}</div>
      {selectedTradeKey && !selectedTrade && <p className="px-3 py-2 text-xs text-ink-2">选中成交在游标处尚未观察到。</p>}
      {!trades.length && <p className="py-4 text-[13px] text-ink-2">游标前暂无已记录的自有成交。</p>}

    </section>
      <section className="replay-command-section replay-detail-list" aria-label="最近报撤请求"><h2 className="sr-only">最近报撤请求 · {commands.length} 条观察</h2><ul className="mt-2 space-y-1.5">{commands.map(command => {
        const observation = replayCommandPresentation(command.data)
        return <li key={`${command.capture_seq}:${command.command_id}`} className="flex flex-wrap gap-x-3"><time className="num">{replayTime(command.time_us)}</time><span>{command.operation === 'cancel_order' ? '撤单' : command.operation === 'place_order' ? '报单' : '操作'} {command.order_id ?? command.symbol ?? ''} · {replayOwnership(command.data, executionId).label}</span><span className={observation.failed ? 'text-warn' : ''}>{observation.text}</span></li>
      })}</ul>{!commands.length && <p className="text-ink-2">游标前尚无报撤请求。</p>}</section>
    <section className="replay-position-section replay-detail-list" aria-label="游标处持仓观察">
      <p className="mb-2 text-xs text-ink-2">推导需要业务对齐证据；可卖与冻结只采用原生观察。</p>
      <table className="num w-full text-right text-xs"><thead className="text-ink-2"><tr><th className="text-left font-normal">方向</th>{['原生持仓', '今仓', '昨仓', '可卖 / 可平', '冻结', '有证据的推导'].map(label => <th key={label} className="whitespace-nowrap px-2 font-normal">{label}</th>)}</tr></thead><tbody>{Object.values(projection.observedPositions).filter(row => row.symbol === symbol).map(position => <tr key={position.direction ?? 'unknown'} className="border-t border-line"><th className="py-2 text-left font-normal">{({ LONG: '多头', SHORT: '空头', NET: '净持仓' } as Record<string, string>)[position.direction ?? ''] ?? '方向未知'}</th>{[position.volume, position.today_volume, position.yesterday_volume, position.available_volume, position.frozen_volume, Object.values(projection.derivedPositions).find(value => value.symbol === symbol && value.direction === position.direction)?.volume].map((value, index) => <td key={index} className="whitespace-nowrap px-2">{replayNumber(value, instrument.quantity_decimals)}</td>)}</tr>)}</tbody></table>
      {!Object.values(projection.observedPositions).some(row => row.symbol === symbol) && <p className="py-3 text-xs text-ink-2">当前持仓尚未观察到，或该层不可用。</p>}
    </section>
        </div>
      </section>
    </div>
  </div>
}
