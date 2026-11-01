import { Pin, X } from 'lucide-react'
import { Select } from '@/components/ui/Select'
import { replayInspectionObjects, type InspectionRecord } from '@/features/replay/inspection'
import { replayTime } from '@/features/replay/presentation'

export function ReplayInspector({ record, records, pinned, width, height, onChoose, onPin, onView, onSeek, onZoom, onClose, onEnter, onLeave }: {
  record: InspectionRecord; records: InspectionRecord[]; pinned: boolean; width: number; height: number
  onChoose: (record: InspectionRecord) => void; onPin: () => void; onView: () => void; onSeek: () => void; onZoom: (() => void) | undefined
  onClose: () => void; onEnter: () => void; onLeave: () => void
}) {
  const objects = replayInspectionObjects(records), related = records.filter(item => item.object === record.object && !(item.kind === 'market' && item.id === item.object)).sort((a, b) => a.time - b.time || (a.seq ?? 0) - (b.seq ?? 0))
  const maxHeight = Math.min(pinned ? 370 : 220, height - 16), cardWidth = Math.min(332, width - 8)
  const left = record.x + cardWidth + 16 < width ? record.x + 4 : record.x - cardWidth - 4
  return <div role="group" aria-label="图中详情" className={`replay-chart-tooltip num ${pinned ? 'is-pinned' : ''}`}
    style={{ width: cardWidth, left: Math.max(0, Math.min(left, width - cardWidth)), top: Math.max(0, Math.min(record.y - 48, height - maxHeight - 8)) }}
    onPointerEnter={onEnter} onPointerLeave={onLeave} onFocusCapture={onEnter} onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) onLeave() }}
    onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }}>
    <div className="replay-chart-tooltip-card shadow-md" style={{ maxHeight }}>
      <header className="replay-inspector-header"><span>{record.title}</span><button type="button" aria-label="关闭图中详情" onClick={onClose}><X size={14} aria-hidden /></button></header>
      {objects.length > 1 && <Select ariaLabel="选择命中对象" appearance="inline" className="w-full justify-between px-1 py-1 text-xs" value={record.object}
        selectedValue={<span>{record.name ? `委托 ${record.name}` : objects.find(item => item.object === record.object)?.status}</span>}
        options={objects.map(item => ({ value: item.object, label: `${item.title}${item.name ? ' · ' + item.name : ''} · ${replayTime(item.time)}` }))}
        onChange={value => { const chosen = objects.find(item => item.object === value); if (chosen) onChoose(chosen) }} />}
      <div className="replay-inspector-summary"><strong>{record.headline}</strong><p>{record.future && <span className="text-accent">后续已录 · </span>}{record.status}</p><time>{replayTime(record.time)}</time></div>
      <div inert={!pinned} className={`replay-inspector-reveal grid transition-[grid-template-rows] duration-150 motion-reduce:transition-none ${pinned ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden">
        <div className="replay-hover-records" role="group" aria-label={record.kind === 'order' ? '委托过程' : record.kind === 'market' ? '市场原始记录' : '盘口观察'}>
          {related.map(item => <button key={item.id} type="button" aria-pressed={item.id === record.id} onClick={() => onChoose(item)}><span>{item.label}</span><time>{replayTime(item.time)}{item.future ? ' · 后续' : ''}</time></button>)}
        </div>
        <details className="replay-inspector-raw"><summary>原始记录</summary><p>{record.raw}</p><p>{replayTime(record.time)}{record.seq != null ? ` · #${record.seq}` : ''}</p></details>
      </div></div>
      <footer className="replay-hover-actions"><button type="button" aria-label={pinned ? '取消固定详情' : '固定详情'} aria-pressed={pinned} onClick={onPin}><Pin size={12} aria-hidden />{pinned ? '已固定' : '固定详情'}</button>
        {pinned && <><button type="button" onClick={onSeek}>定位此时刻</button>{onZoom && <button type="button" onClick={onZoom}>放大过程</button>}{(record.order || record.trade) && <button type="button" onClick={onView}>查看{record.trade ? '成交' : '委托'}</button>}</>}
      </footer>
    </div>
  </div>
}
