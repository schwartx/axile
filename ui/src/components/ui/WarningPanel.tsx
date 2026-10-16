import { useId } from 'react'
import { createPortal } from 'react-dom'
import { TriangleAlert, X } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { OverflowText } from '@/components/ui/OverflowText'
import { InkRewrite } from '@/components/ui/InkRewrite'

export interface WarningPanelItem {
  id: string
  title: string
  status: string
  used: number
  limit: number
  remaining: number
  threshold: string | null
  stale: boolean
  href: string
}
export interface WarningPanelAnnouncement { id: number; text: string }

/** 非阻塞、手动关闭的预警面板；触发及去重由调用方负责。 */
export function WarningPanel({ items, announcement, onDismiss }: {
  items: WarningPanelItem[]
  announcement: WarningPanelAnnouncement | null
  onDismiss: (id?: string) => void
}) {
  const titleId = useId()
  return createPortal(<>
    <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">{announcement && <span key={announcement.id}>{announcement.text}</span>}</div>
    {items.length > 0 && <section aria-labelledby={titleId}
      className="fixed right-3 top-16 z-30 flex max-h-[60dvh] w-[380px] max-w-[calc(100vw-24px)] flex-col overflow-hidden rounded-[14px] border border-line bg-surface text-ink-1 shadow-[0_12px_40px_rgba(0,0,0,0.18)] sm:right-5">
      <header className="flex shrink-0 items-center gap-2 border-b border-line px-4 py-3">
        <TriangleAlert size={17} className="shrink-0 text-warn" aria-hidden />
        <h2 id={titleId} className="flex-1 text-[14px] font-semibold">流控预警{items.length > 1 ? ` · ${items.length}` : ''}</h2>
        {items.length > 1 ? <button type="button" className="cursor-pointer text-[12px] text-ink-2 hover:text-ink-1" onClick={() => onDismiss()}>全部关闭</button>
          : <button type="button" aria-label="关闭流控预警" className="cursor-pointer rounded p-1 text-ink-3 hover:text-ink-1" onClick={() => onDismiss(items[0].id)}><X size={16} aria-hidden /></button>}
      </header>
      <div className="min-h-0 overflow-y-auto overscroll-contain divide-y divide-line">
        {items.map(item => <article key={item.id} aria-label={item.title} className="px-4 py-3.5">
          <div className="flex items-start gap-2">
            <OverflowText text={item.title} className="min-w-0 flex-1 text-[14px] font-medium" />
            {items.length > 1 && <button type="button" aria-label={`关闭${item.title}预警`} className="cursor-pointer rounded p-1 text-ink-3 hover:text-ink-1" onClick={() => onDismiss(item.id)}><X size={14} aria-hidden /></button>}
          </div>
          <p className="mt-1 text-[13px] font-medium text-warn"><InkRewrite text={item.status} /></p>
          {item.threshold && <p className="mt-1 text-xs text-ink-3">{item.threshold}</p>}
          <div className="num mt-3 flex flex-wrap justify-between gap-1 text-[13px]">
            <span className="text-ink-2">已用 {item.used} / {item.limit}</span>
            <span className="font-medium text-warn">剩余 {item.remaining} 次</span>
          </div>
          <div role="progressbar" aria-label={`${item.title}额度用量`} aria-valuemin={0} aria-valuemax={item.limit} aria-valuenow={Math.min(item.used, item.limit)}
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
            <div className="h-full rounded-full bg-warn" style={{ width: `${Math.min(100, item.used / item.limit * 100)}%` }} />
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-ink-3">{item.stale ? '数据待更新 · 最后已知用量' : '今日额度 · 本地放行次数'}</span>
            <Link to={item.href} onClick={() => onDismiss(item.id)} className="rounded-md border border-line px-2.5 py-1 text-[12px] font-medium text-accent hover:border-border-strong">查看流控 →</Link>
          </div>
        </article>)}
      </div>
    </section>}
  </>, document.body)
}
