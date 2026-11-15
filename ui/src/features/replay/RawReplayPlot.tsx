import type { KeyboardEvent, ReactNode } from 'react'
import type { RawBusiness } from './raw'

type Quote = { time_us: number; observed_us: number; until_us: number; capture_seq: number; bids: number[][]; asks: number[][] }
type Bin = { index: number; count: number; quantity: number; vwap: number | null; unknown: number }
type OrderPoint = { time_us: number; capture_seq: number; price: number | null; quantity: number | null }
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const labelTime = (us: number) => new Date(us / 1000).toLocaleTimeString('zh-CN', { hour12: false }) + `.${String(us % 1_000_000).padStart(6, '0')}`

/** Only WASM's bounded display buckets enter the renderer. Exact navigation re-evaluates raw evidence. */
export function RawReplayPlot({ kind, subjects, start, end, onSeek }: { kind: string; subjects: RawBusiness[]; start: number; end: number; onSeek: (time: number, seq?: number) => void }) {
  const data = subjects[0]?.data
  const quotes = kind === 'interval' ? ((data?.viewport as { quotes?: Quote[] } | null)?.quotes ?? []).slice(0,128).filter(q=>q.until_us>q.time_us) : []
  const sources = kind === 'interval' ? (['own','market'] as const).map(source => ({ source, bins: ((data?.[source] as { timeline?: Bin[] } | undefined)?.timeline ?? []).slice(0,128) })) : []
  const orders = kind === 'order' ? subjects.slice(0,8).map(subject => ({ key: subject.key, points: ((subject.data.history ?? []) as OrderPoint[]).slice(0,16) })) : []
  const prices = [...quotes.flatMap(q => [...q.bids.slice(0,8),...q.asks.slice(0,8)].map(l=>l[0])),...sources.flatMap(s=>s.bins.map(b=>b.vwap)),...orders.flatMap(o=>o.points.map(p=>p.price))].filter(finite)
  if (!prices.length || start >= end) return <p className="mt-3 text-sm text-ink-3">此视口没有可绘制的已知价格。</p>
  const low = Math.min(...prices), high = Math.max(...prices), span = Math.max(high-low, Math.abs(high)*0.001, 1e-9)
  if (!Number.isFinite(span)) return <p className="mt-3 text-sm text-ink-3">价格跨度超出图形范围，精确数值见表格。</p>
  const x = (t:number) => 70+Math.max(0,Math.min(1,(t-start)/(end-start)))*860
  const y = (p:number) => 230-(p-low+span*.05)/(span*1.1)*200
  const quantityMax = Math.max(1,...quotes.flatMap(q=>[...q.bids,...q.asks].map(l=>l[1])).filter(finite))
  const tradeMax = Math.max(1,...sources.flatMap(s=>s.bins.map(b=>b.quantity)))
  const interactive = (key:string,time:number,seq:number|undefined,title:string,children:ReactNode) => {
    const navigate = () => onSeek(time,seq)
    const keyboard = (event:KeyboardEvent<SVGGElement>) => { if (event.key==='Enter'||event.key===' ') { event.preventDefault();navigate() } }
    return <g key={key} role="button" tabIndex={0} aria-label={title} className="cursor-pointer focus-visible:outline-2 focus-visible:outline-accent" onClick={navigate} onKeyDown={keyboard}><title>{title}</title>{children}</g>
  }
  return <figure className="mt-3 rounded-card border border-line p-3">
    <figcaption className="text-sm text-ink-2">{kind==='interval'?'区间盘口与成交':'当前订单页的历史观察'}</figcaption>
    <p className="mt-1 text-xs text-ink-3">{kind==='interval'?'历史盘口每侧最多 8 档、128 个时间格，格内保留最后一条观察；成交圆点为格内已确认数量与 VWAP，空白不表示零。':'每页最多 8 个订单、每单最多 16 个历史抽样点；点之间不推断订单状态。'} 点击图形或聚焦后按回车，重新求值该时点的精确状态。</p>
    <svg viewBox="0 0 1000 280" role="group" aria-label={kind==='interval'?'有界区间盘口及成交图':'有界订单观察图'} className="mt-2 w-full text-ink-3">
      <path d="M70 20V240H930" fill="none" stroke="currentColor" opacity=".35" />
      {[low,low+(high-low)/2,high].map((p,i)=><g key={i}><text x="64" y={y(p)+4} textAnchor="end" fontSize="11" fill="currentColor">{p.toPrecision(6)}</text><path d={`M70 ${y(p)}H930`} stroke="currentColor" opacity=".1" /></g>)}
      {quotes.map((q,i)=>interactive(`quote:${i}`,q.observed_us,q.capture_seq,`${labelTime(q.observed_us)} 盘口观察 ${q.capture_seq}`,
        <g>{(['bids','asks'] as const).flatMap(side=>q[side].slice(0,8).map((l,j)=><rect key={`${side}:${j}`} x={x(q.time_us)} y={y(l[0])-2} width={Math.max(1,x(q.until_us)-x(q.time_us))} height="4" fill={side==='bids'?'var(--color-accent)':'var(--color-warn)'} opacity={.15+.65*Math.log1p(l[1])/Math.log1p(quantityMax)} />))}</g>))}
      {sources.flatMap(({source,bins})=>bins.map(b=>{
        const t=Math.min(end-1,start+Math.floor((end-start)*(b.index+.5)/128))
        return interactive(`${source}:${b.index}`,t,undefined,`${labelTime(t)} ${source==='own'?'自有':'市场'} ${b.count} 个已确认身份，数量 ${b.quantity}，未知 ${b.unknown}`,
          finite(b.vwap)?<circle cx={x(t)} cy={y(b.vwap)} r={3+8*Math.sqrt(b.quantity/tradeMax)} fill={source==='own'?'var(--color-accent)':'var(--color-warn)'} fillOpacity=".6" stroke="currentColor" strokeWidth=".5" />:<text x={x(t)} y="235" fontSize="12" fill="currentColor">?</text>)
      }))}
      {orders.flatMap((order,i)=>order.points.filter(p=>finite(p.price)).map(p=>interactive(`${i}:${p.capture_seq}`,p.time_us,p.capture_seq,`${order.key} ${labelTime(p.time_us)} 报价 ${p.price}，数量 ${p.quantity??'未知'}，观察 ${p.capture_seq}`,
        <circle cx={x(p.time_us)} cy={y(p.price!)} r="4" fill="var(--color-accent)" stroke="currentColor" strokeWidth=".5" />)))}
      <text x="70" y="266" fontSize="11" fill="currentColor">{labelTime(start)}</text><text x="930" y="266" textAnchor="end" fontSize="11" fill="currentColor">{labelTime(end-1)}</text>
    </svg>
  </figure>
}
