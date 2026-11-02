import { replayPriceY, replayRemainingQuantity, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import { recordedDepth, replayDepthBuckets, type ReplayDepthBucket } from '@/features/replay/depthGeometry'
import { replayOwnership } from '@/features/replay/ownership'
import { Tooltip } from '@/components/ui/Tooltip'
import type { ReplayOrder, ReplayQuote } from '@/features/replay/types'

export function ReplayDepthLadder({ quote, future = false, orders, view, layout, height, executionId, selected, onSelect, onZoom, priceText, quantityText }: {
  quote: ReplayQuote | null; future?: boolean; orders: ReplayOrder[]; view: ReplayPriceView; layout: ReplayChartLayout; height: number
  executionId: string; selected: string | null; onSelect: (key: string) => void; onZoom: (view: ReplayPriceView) => void
  priceText: (price: number | null) => string; quantityText: (quantity: number | null) => string
}) {
  const levels = recordedDepth(quote), buckets = replayDepthBuckets(levels, orders, view, layout)
  const max = Math.max(1, ...levels.map(level => level.quantity ?? 0))
  const outside = levels.filter(level => level.price < view.min || level.price > view.max).length
  const aggregated = buckets.some(bucket => bucket.minPrice !== bucket.maxPrice)
  const describe = (bucket: ReplayDepthBucket) => {
    const prices = bucket.minPrice === bucket.maxPrice ? priceText(bucket.minPrice) : `${priceText(bucket.minPrice)}–${priceText(bucket.maxPrice)}`
    const rank = bucket.levels.length === 1 ? `${bucket.levels[0].side}${bucket.levels[0].rank ?? '·0'}` : bucket.levels.length ? `${bucket.levels.length}档` : quote ? '档外' : '未知'
    const amount = bucket.quantity == null ? '未知' : `${quantityText(bucket.quantity)}${bucket.levels.length > 1 ? ' 峰' : ''}${bucket.unknown ? ' ?' : ''}`
    const own = bucket.orders.length ? `${quantityText(replayRemainingQuantity(bucket.orders))}${bucket.orders.length > 1 ? ` (${bucket.orders.length})` : ''}` : '—'
    return { rank, prices, amount, own }
  }
  const select = (bucket: ReplayDepthBucket) => {
    if (bucket.minPrice !== bucket.maxPrice) onZoom({ ...view, min: bucket.minPrice - view.tick * 2, max: bucket.maxPrice + view.tick * 2 })
    else if (bucket.orders.length) onSelect((bucket.orders.find(order => order.key === selected) ?? bucket.orders[0]).key)
  }
  return <div className="replay-depth-ladder num" data-future={future} style={{ left: layout.right + 8, height }} aria-label={future ? '未到达的首个已录盘口' : '与大图同价对齐的当前盘口'}>
    <div className="replay-ladder-head"><div><span>档位</span><span>价格</span><span>市场量</span><Tooltip content="回放游标时刻，账户在该价位的有效委托未成交剩余量，包含其他执行；不代表持仓或排队位置。"><button type="button" className="text-right" aria-label="账户挂单说明">账户挂单</button></Tooltip></div></div>
    <svg className="replay-ladder-lines" width="100%" height={height} aria-hidden="true">
      {buckets.map(bucket => <g key={`${bucket.minPrice}:${bucket.maxPrice}`}>
        <circle cx="6" cy={bucket.y} r="2" fill="var(--color-ink-2)" />
        <path d={`M 10 ${bucket.y} H 24 L 68 ${bucket.labelY} H 82`} fill="none" stroke="var(--color-ink-3)" strokeWidth=".7" />
      </g>)}
      {orders.map(order => order.price != null && order.price >= view.min && order.price <= view.max ? <path key={order.key} d={`M 0 ${replayPriceY(order.price, view, layout)} h 59`} stroke={order.cancel_pending ? 'var(--color-warn)' : 'var(--color-accent)'} strokeWidth={order.key === selected ? 3 : 1.5} /> : null)}
    </svg>
    <div role="list" aria-label={future ? '未到达的已录价位' : '当前已录价位'}>
      {buckets.map(bucket => {
        const row = describe(bucket), grouped = bucket.minPrice !== bucket.maxPrice
        const current = bucket.orders.filter(order => replayOwnership(order, executionId).kind === 'current')
        const title = `${row.rank} · ${row.prices} · ${grouped ? '区间峰值' : '市场挂量'} ${row.amount}${bucket.unknown ? '，含未知量' : ''} · 自有剩余 ${row.own}${current.length ? `，本次 ${quantityText(replayRemainingQuantity(current))}` : ''}${grouped ? '；点击放大看单档' : ''}；价位不代表排队顺序`
        const content = <><span className={bucket.levels[0]?.side === '买' ? 'replay-depth-bid' : 'replay-depth-ask'}>{row.rank}</span><span className={grouped ? 'replay-ladder-price-range' : ''}>{grouped ? <><span>{priceText(bucket.minPrice)}</span><span>– {priceText(bucket.maxPrice)}</span></> : row.prices}</span><span className="replay-ladder-amount"><i aria-hidden="true" className={bucket.levels[0]?.side === '买' ? 'replay-depth-bid' : 'replay-depth-ask'} style={{ width: `${(bucket.quantity ?? 0) / max * 100}%` }} /><span>{row.amount}</span></span><span className={bucket.orders.some(order => order.cancel_pending) ? 'text-warn' : 'text-accent'}>{row.own}</span></>
        const multiple = !grouped && bucket.orders.length > 1
        const button = <button type="button" aria-label={multiple ? `${title}；展开 ${bucket.orders.length} 张同价挂单` : title} className={`replay-ladder-row ${grouped ? 'is-grouped' : ''} ${bucket.orders.some(order => order.key === selected) ? 'is-selected' : ''}`} style={{ top: bucket.labelY - (grouped ? 12 : 10) }} onClick={event => { if (multiple) event.currentTarget.focus(); else select(bucket) }}>{content}</button>
        return <div role="listitem" key={`${bucket.minPrice}:${bucket.maxPrice}`}>
          {multiple ? <Tooltip interactive delay={0} content={<div aria-label={`${row.prices} 的账户挂单`}><p>{row.prices} · {bucket.orders.length} 张挂单 · 剩余 {row.own}</p><div className="replay-ladder-orders">{bucket.orders.map(order => <button key={order.key} type="button" aria-pressed={order.key === selected} onClick={() => onSelect(order.key)}><span>{order.order_id} · {replayOwnership(order, executionId).label}</span><span>{order.direction === 'BUY' ? '买' : order.direction === 'SELL' ? '卖' : '方向未知'} · 剩余 {quantityText(order.remaining_volume)}{order.cancel_pending ? ' · 待撤' : ''}</span></button>)}</div><p>价格档位不代表排队顺序</p></div>}>{button}</Tooltip> : <Tooltip content={title}>{grouped || bucket.orders.length ? button
            : <div className="replay-ladder-row" style={{ top: bucket.labelY - 10 }}>{content}</div>}</Tooltip>}
        </div>
      })}
    </div>
    {!quote && <p className="replay-ladder-empty">盘口未记录／已失效<br />自有委托独立显示</p>}
    {(aggregated || outside > 0) && <p className="replay-ladder-note">{aggregated ? '按价区间聚合 · 峰值 · 点击展开' : ''}{outside ? ` · 视野外 ${outside} 档` : ''}</p>}
  </div>
}
