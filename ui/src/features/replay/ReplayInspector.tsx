import { X } from 'lucide-react'
import { Select } from '@/components/ui/Select'
import { replayInspectionObjects, type InspectionRecord } from '@/features/replay/inspection'
import { replayTime } from '@/features/replay/presentation'

export function ReplayInspector({ record, records, onChoose, onView, onSeek, onClose }: {
  record: InspectionRecord; records: InspectionRecord[]
  onChoose: (record: InspectionRecord) => void; onView: () => void; onSeek: () => void; onClose: () => void
}) {
  const objects = replayInspectionObjects(records), related = records.filter(item => item.object === record.object && !(item.kind === 'market' && item.id === item.object)).sort((a, b) => a.time - b.time || (a.seq ?? 0) - (b.seq ?? 0))
  const detail = record.kind === 'market' && record.id === record.object ? related.length === 1 ? related[0] : null : record
  return <div role="group" aria-label="图中记录详情" className="replay-selected-inspection num" onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }}>
      <header className="replay-inspector-header"><span>{record.title}</span><div className="replay-inspector-actions">{(record.order || record.trade) && <button type="button" onClick={onView}>查看{record.trade ? '成交' : '委托'}</button>}<button type="button" onClick={onSeek}>定位此时刻</button><button type="button" aria-label="关闭记录详情" onClick={onClose}><X size={14} aria-hidden /></button></div></header>
      {objects.length > 1 && <Select ariaLabel="选择命中对象" appearance="inline" className="w-full justify-between px-1 py-1 text-xs" value={record.object}
        selectedValue={<span>{record.name ? `委托 ${record.name}` : objects.find(item => item.object === record.object)?.status}</span>}
        options={objects.map(item => ({ value: item.object, label: `${item.title}${item.name ? ' · ' + item.name : ''} · ${replayTime(item.time)}` }))}
        onChange={value => { const chosen = objects.find(item => item.object === value); if (chosen) onChoose(chosen) }} />}
      <div className="replay-inspector-summary"><strong>{record.headline}</strong><p>{record.future && <span className="text-ink-2">未到达 · </span>}{record.status}</p><time>{replayTime(record.time)}</time></div>

      <div className="replay-inspector-details">
        {related.length > 1 && <details><summary>{record.kind === 'order' ? '委托过程' : '市场记录'} · {related.length} 条</summary><div className="replay-hover-records" role="group" aria-label={record.kind === 'order' ? '委托过程' : '市场原始记录'}>
          {related.map(item => <button key={item.id} type="button" aria-pressed={item.id === record.id} onClick={() => onChoose(item)}><span>{item.label}</span><time>{replayTime(item.time)}{item.future ? ' · 未到达' : ''}</time></button>)}
        </div></details>}
        {detail && <details className="replay-inspector-raw"><summary>记录详情</summary><p>{detail.headline}</p><p>{detail.status}</p><p>{replayTime(detail.time)}{detail.future ? ' · 未到达' : ''}</p><p>{detail.raw}</p></details>}
      </div>

  </div>
}
