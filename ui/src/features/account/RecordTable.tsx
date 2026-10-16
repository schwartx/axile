import { useState, type ReactNode } from 'react'

export interface RecordColumn {
  key: string
  label: string
  /** 加载前也已知的完整值示例，用于首帧预留空间。 */
  sample?: string
  values?: string[]
  maxWidth?: number
  numeric?: boolean
}

type Props = {
  columns: RecordColumn[]
  kind: 'trade' | 'order'
  loading?: boolean
  empty: boolean
  emptyText: string
  children: ReactNode
}

function columnWidths(columns: RecordColumn[]): number[] {
  const context = document.createElement('canvas').getContext('2d')
  if (context) context.font = `13px ${getComputedStyle(document.body).fontFamily}`
  const measure = (text: string) => context?.measureText(text.replace(/\d/g, '8')).width ?? Array.from(text).reduce((width, char) => width + (char.charCodeAt(0) > 255 ? 13 : 8), 0)
  return columns.map((column, index) => {
    if (column.key === 'details') return 32
    const content = Math.max(...[column.label, column.sample ?? '', ...(column.values ?? [])].map(measure))
    // 两侧各 12px，首列贴齐内容区；留少量字体加载误差，避免数值挤到相邻列。
    const padding = index === 0 ? 12 : 24
    return Math.ceil(Math.min(content + 6, column.maxWidth ?? 220) + padding)
  })
}

/** 同一字段布局只在首帧分配列宽，加载完成、轮询和详情展开均不重排。 */
export function RecordTable(props: Props) {
  const layout = JSON.stringify([props.kind, props.columns.map(({ key, label, sample, maxWidth }) => [key, label, sample, maxWidth])])
  return <SizedRecordTable key={layout} {...props} />
}

function SizedRecordTable({ columns, kind, loading, empty, emptyText, children }: Props) {
  const [widths] = useState(() => columnWidths(columns))
  return <div className="min-w-0" aria-busy={loading || undefined} aria-label={kind === 'trade' ? '成交记录' : '委托记录'}>
    {loading && <span className="sr-only" role="status">正在读取近期记录…</span>}
    <div className="overflow-x-auto" data-record-scroll>
      <table className="table-fixed border-collapse text-left" style={{ width: widths.reduce((sum, width) => sum + width, 0) }} data-trade-table={kind === 'trade' || undefined} data-order-table={kind === 'order' || undefined}>
        <colgroup>{columns.map((column, index) => <col key={column.key} style={{ width: widths[index] }} />)}</colgroup>
        <thead><tr className="border-b border-line text-xs text-ink-3 [&_th]:whitespace-nowrap [&_th]:px-3 [&_th]:py-2 [&_th]:font-normal [&_th:first-child]:pl-0 [&_th:last-child]:pr-0">
          {columns.map(column => <th key={column.key} className={column.numeric ? 'text-right' : ''}>{column.key === 'details' ? <span className="sr-only">{column.label}</span> : column.label}</th>)}
        </tr></thead>
        {loading ? <tbody aria-hidden data-record-placeholder>{Array.from({ length: 5 }, (_, index) => <tr key={index} className="h-16 border-b border-line [&_td]:px-3 [&_td:first-child]:pl-0 [&_td:last-child]:pr-0">
          {columns.map(column => <td key={column.key}>{column.key !== 'details' && <div className={`h-3 w-3/5 rounded bg-fill ${column.numeric ? 'ml-auto' : ''}`} />}</td>)}
        </tr>)}</tbody> : empty ? <tbody /> : children}
      </table>
    </div>
    {!loading && empty && <p data-record-empty className="py-10 text-center text-sm text-ink-3">{emptyText}</p>}
  </div>
}
