import { useState, type ReactNode } from 'react'

export interface RecordColumn {
  key: string
  label: string
  /** 加载前也已知的完整值示例，用于首帧预留空间。 */
  sample?: string
  values?: string[]
  maxWidth?: number
  numeric?: boolean
  /** 主要列按权重分配视口剩余空间；短枚举与操作列保持内容宽度。 */
  grow?: number
}

type Props = {
  columns: RecordColumn[]
  kind: 'trade' | 'order' | 'event'
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
  const layout = JSON.stringify([props.kind, props.columns.map(({ key, label, sample, maxWidth, grow }) => [key, label, sample, maxWidth, grow])])
  return <SizedRecordTable key={layout} {...props} />
}

function SizedRecordTable({ columns, kind, loading, empty, emptyText, children }: Props) {
  const [widths] = useState(() => columnWidths(columns))
  const minimumWidth = widths.reduce((sum, width) => sum + width, 0)
  const totalGrow = columns.reduce((sum, column) => sum + (column.grow ?? 0), 0)
  return <div className="min-w-0" aria-busy={loading || undefined} aria-label={kind === 'trade' ? '成交记录' : kind === 'order' ? '委托记录' : '操作异常记录'}>
    {loading && <span className="sr-only" role="status">正在读取近期记录…</span>}
    <div className="overflow-x-auto" data-record-scroll>
      <table className="table-fixed border-collapse text-left" style={{ width: '100%', minWidth: minimumWidth }} data-trade-table={kind === 'trade' || undefined} data-order-table={kind === 'order' || undefined} data-event-table={kind === 'event' || undefined}>
        <colgroup>{columns.map((column, index) => {
          const share = totalGrow ? (column.grow ?? 0) / totalGrow : 0
          // CSS 按视口分配额外宽度；加载和轮询只替换内容，不重新测量或调整基础列宽。
          const width = share ? `calc(${share * 100}% + ${widths[index] - minimumWidth * share}px)` : widths[index]
          return <col key={column.key} style={{ width }} />
        })}</colgroup>
        <thead><tr className="border-b border-line text-xs text-ink-3 [&_th]:whitespace-nowrap [&_th]:px-3 [&_th]:py-2 [&_th]:font-normal [&_th:first-child]:pl-0 [&_th:last-child]:pr-0">
          {columns.map(column => <th key={column.key} className={`${column.numeric ? 'text-right' : ''} ${column.key === 'details' ? 'sticky right-0 bg-surface' : ''}`}>{column.key === 'details' ? <span className="sr-only">{column.label}</span> : column.label}</th>)}
        </tr></thead>
        {loading ? <tbody aria-hidden data-record-placeholder>{Array.from({ length: 5 }, (_, index) => <tr key={index} className="h-16 border-b border-line [&_td]:px-3 [&_td:first-child]:pl-0 [&_td:last-child]:pr-0">
          {columns.map(column => <td key={column.key} className={column.key === 'details' ? 'sticky right-0 bg-surface' : undefined}>{column.key !== 'details' && <div className={`h-3 w-3/5 rounded bg-fill ${column.numeric ? 'ml-auto' : ''}`} />}</td>)}
        </tr>)}</tbody> : empty ? <tbody /> : children}
      </table>
    </div>
    {!loading && empty && <p data-record-empty className="py-10 text-center text-sm text-ink-3">{emptyText}</p>}
  </div>
}
