import { useEffect, useMemo, useRef, useState } from 'react'
import { replayRemainingQuantity, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import { recordedDepth, replayDepthBuckets, replayDepthOrderGroups } from '@/features/replay/depthGeometry'
import { replayOwnership } from '@/features/replay/ownership'
import { Tooltip } from '@/components/ui/Tooltip'
import type { ReplayOrder, ReplayQuote } from '@/features/replay/types'

export function replayDepthScrollTarget(top: number, bottom: number, scroll: number, height: number) {
  const margin = Math.min(60, Math.max(0, (height - (bottom - top)) / 4))
  const target = top < scroll + margin ? Math.max(0, top - margin) : bottom > scroll + height - margin ? bottom - height + margin : null
  return target === scroll ? null : target
}

export function ReplayDepthLadder({ quote, future = false, orders, view, layout, height, executionId, selected, onSelect, priceText, quantityText, hoverPrice }: {
  quote: ReplayQuote | null; future?: boolean; orders: ReplayOrder[]; view: ReplayPriceView; layout: ReplayChartLayout; height: number
  executionId: string; selected: string | null; onSelect: (key: string) => void
  priceText: (price: number | null) => string; quantityText: (quantity: number | null) => string
  hoverPrice?: number
}) {
  const levels = useMemo(() => recordedDepth(quote), [quote])
  const buckets = useMemo(() => replayDepthBuckets(levels, orders, view, layout), [levels, orders, view, layout])
  const [scrollTop, setScrollTop] = useState(0)
  const [rowHover, setRowHover] = useState<number | null>(null)
  const list = useRef<HTMLDivElement>(null)
  const initialPositioned = useRef(false)
  useEffect(() => {
    const element = list.current
    if (initialPositioned.current || !element || !buckets.length) return
    initialPositioned.current = true
    const offsets = buckets.map((bucket, index) => 38 + index * 20 - bucket.y).sort((a, b) => a - b)
    element.scrollTop = Math.max(0, offsets[Math.floor(offsets.length / 2)])
  }, [buckets])
  const pendingScroll = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (hoverPrice == null) return
    pendingScroll.current = setTimeout(() => {
      const element = list.current, row = element?.querySelector<HTMLElement>('[data-hovered="true"]')
      if (!element || !row) return
      const top = row.offsetTop - 28, target = replayDepthScrollTarget(top, top + row.offsetHeight, element.scrollTop, element.clientHeight - 28)
      if (target != null) element.scrollTo({ top: target, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })
    }, 150)
    return () => { if (pendingScroll.current != null) clearTimeout(pendingScroll.current) }
  }, [hoverPrice])
  const stopAutoScroll = () => {
    if (pendingScroll.current != null) clearTimeout(pendingScroll.current)
    const element = list.current
    if (element) element.scrollTo({ top: element.scrollTop, behavior: 'instant' })
  }
  const max = Math.max(1, ...levels.map(level => level.quantity ?? 0))
  return <div className="replay-depth-ladder num" data-future={future} style={{ left: layout.priceX + 16, height }} aria-label={future ? '未到达的首个已录盘口' : '当前盘口'}>
    <svg className="replay-ladder-lines" width="48" height={height} aria-hidden="true">
      {buckets.map((bucket, index) => {
        const rowY = 38 + index * 20 - scrollTop
        const highlighted = rowHover === bucket.minPrice || (hoverPrice != null && Math.abs(hoverPrice - bucket.minPrice) <= view.tick * 1e-6)
        return rowY - 10 >= 28 && rowY + 10 <= height ? <path key={bucket.minPrice} d={`M 0 ${bucket.y} H 8 C 22 ${bucket.y}, 26 ${rowY}, 40 ${rowY} H 48`} fill="none" stroke={highlighted ? 'var(--color-accent)' : 'var(--color-ink-3)'} strokeWidth={highlighted ? 1.5 : .7} /> : null
      })}
    </svg>
    <div ref={list} role="list" className="replay-ladder-list" aria-label={future ? '未到达的已录价位' : '当前已录价位'} tabIndex={0} onWheel={stopAutoScroll} onTouchStart={stopAutoScroll} onPointerDown={stopAutoScroll} onKeyDown={stopAutoScroll} onScroll={event => setScrollTop(event.currentTarget.scrollTop)}>
      <div className="replay-ladder-head"><span>档位</span><Tooltip content="价格旁的买／卖标签为回放游标时刻，账户在该价位的有效委托未成交剩余量，包含其他执行；不代表持仓或排队位置。"><button type="button" className="text-right" aria-label="价格与账户挂单说明">价格</button></Tooltip><span>市场量</span></div>
      {buckets.map(bucket => {
        const level = bucket.levels[0], price = priceText(bucket.minPrice)
        const rank = level ? `${level.side}${level.rank ?? '·0'}` : quote ? '档外' : '未知'
        const amount = bucket.quantity == null ? '未知' : quantityText(bucket.quantity)
        const groups = replayDepthOrderGroups(bucket.orders)
        const current = bucket.orders.filter(order => replayOwnership(order, executionId).kind === 'current')
        const title = `${rank} · ${price} · 市场挂量 ${amount}${bucket.unknown ? '，含未知量' : ''}${groups.map(group => ` · 自有${group.label}剩余 ${quantityText(replayRemainingQuantity(group.orders))}`).join('')}${current.length ? `，本次 ${quantityText(replayRemainingQuantity(current))}` : ''}；价位不代表排队顺序`
        const content = <><span className={level?.side === '买' ? 'replay-depth-bid' : 'replay-depth-ask'}>{rank}</span><span className="replay-ladder-price"><span>{price}</span>{groups.map(group => <span key={group.direction ?? 'unknown'} className={`replay-ladder-order-tag ${group.direction === 'BUY' ? 'replay-depth-bid' : group.direction === 'SELL' ? 'replay-depth-ask' : 'text-ink-2'}`} data-pending={group.orders.some(order => order.cancel_pending) || undefined}>{group.label} {quantityText(replayRemainingQuantity(group.orders))}{group.orders.some(order => order.cancel_pending) ? ' 待撤' : ''}</span>)}</span><span className="replay-ladder-amount"><i aria-hidden="true" className={level?.side === '买' ? 'replay-depth-bid' : 'replay-depth-ask'} style={{ width: `${(bucket.quantity ?? 0) / max * 100}%` }} /><span>{amount}</span></span></>
        const multiple = bucket.orders.length > 1
        const button = <button type="button" aria-label={multiple ? `${title}；展开 ${bucket.orders.length} 张同价挂单` : title} className={`replay-ladder-row has-order ${bucket.orders.some(order => order.key === selected) ? 'is-selected' : ''}`} onClick={event => { if (multiple) event.currentTarget.focus(); else onSelect(bucket.orders[0].key) }}>{content}</button>
        return <div role="listitem" key={bucket.minPrice} data-price={`${bucket.minPrice}:${bucket.maxPrice}`} data-hovered={hoverPrice != null && Math.abs(hoverPrice - bucket.minPrice) <= view.tick * 1e-6 || undefined} onPointerEnter={() => setRowHover(bucket.minPrice)} onPointerLeave={() => setRowHover(null)}>
          {bucket.orders.length ? <Tooltip interactive delay={0} content={<div aria-label={`${price} 的账户挂单`}><p>{price} · {bucket.orders.length} 张挂单</p><div className="replay-ladder-orders">{bucket.orders.map(order => <button key={order.key} type="button" aria-pressed={order.key === selected} onClick={() => onSelect(order.key)}><span>{order.order_id} · {replayOwnership(order, executionId).label}</span><span>{order.direction === 'BUY' ? '买' : order.direction === 'SELL' ? '卖' : '方向未知'} · 剩余 {quantityText(order.remaining_volume)}{order.cancel_pending ? ' · 待撤' : ''}</span></button>)}</div><p>价格档位不代表排队顺序</p></div>}>{button}</Tooltip> : <div className="replay-ladder-row">{content}</div>}
        </div>
      })}
    </div>
    {!quote && <p className="replay-ladder-empty">盘口未记录／已失效<br />自有委托独立显示</p>}
  </div>
}
