import { useEffect, useMemo, useRef, useState } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { getValidationTable, type ValidationTable, type ValidationTableSummary } from '@/lib/api/connectionValidation'
import { selectValidationRows, validationCellText } from '@/features/account/connectionValidation/validationTable'

export interface ValidationTableSelection {
  validationId: string
  stepId: string
  details: ValidationTableSummary
}
const pageSize = 25

/** 同一验证的各表按需读取并在内存复用；关闭或换任务时中止未完成请求。 */
export function ValidationRecordsModal({ selection, onClose }: {
  selection: ValidationTableSelection | null
  onClose: () => void
}) {
  const lastSelection = useRef(selection)
  if (selection) lastSelection.current = selection
  const shown = selection ?? lastSelection.current
  const cache = useRef(new Map<string, ValidationTable>())
  const cacheJob = useRef<string | null>(null)
  const [loaded, setLoaded] = useState<{ key: string; table: ValidationTable } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const key = shown ? `${shown.validationId}/${shown.stepId}` : ''
  useEffect(() => {
    if (!selection) return
    if (cacheJob.current !== selection.validationId) { cache.current.clear(); cacheJob.current = selection.validationId }
    setError(null)
    const cached = cache.current.get(key)
    if (cached) { setLoaded({ key, table: cached }); return }
    setLoaded(null)
    const controller = new AbortController()
    void getValidationTable(selection.validationId, selection.stepId, controller.signal).then((table) => {
      if (controller.signal.aborted) return
      cache.current.set(key, table)
      setLoaded({ key, table })
    }, (failure) => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '无法读取查询记录')
    })
    return () => controller.abort()
  }, [selection, key, attempt])
  const table = loaded?.key === key ? loaded.table : null
  return <ValidationRecordsView key={key} table={table} details={shown?.details ?? null} open={selection !== null}
    error={error} onRetry={() => setAttempt((value) => value + 1)} onClose={onClose} />
}

/** 表头与筛选保持在原位；只翻页切换记录，长字段自然换行。 */
function ValidationRecordsView({ table, details, open, error, onRetry, onClose }: {
  table: ValidationTable | null
  details: ValidationTableSummary | null
  open: boolean
  error: string | null
  onRetry: () => void
  onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [sort, setSort] = useState<{ key: string; descending: boolean } | null>(null)
  const [page, setPage] = useState(1)
  useEffect(() => {
    if (open) { setQuery(''); setFilters({}); setSort(null); setPage(1) }
  }, [open])
  const rows = useMemo(() => table ? selectValidationRows(table, query, filters, sort) : [], [table, query, filters, sort])
  const pages = Math.max(1, Math.ceil(rows.length / pageSize))
  const currentPage = Math.min(page, pages)
  const options = useMemo(() => table?.columns.flatMap((column, index) => {
    if (!column.filterable) return []
    const values = [...new Set(table.rows.map((row) => validationCellText(row[index])))].sort()
    return values.length > 1 ? [{ column, values }] : []
  }) ?? [], [table])
  return <CenteredModal open={open} title={details?.title ?? '查询记录'} widthClassName="w-[1120px]"
    subtitle="本次连接验证的查询快照" onClose={onClose}
    footer={<div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-[22px] py-4 text-[12px] text-ink-3">
      <span aria-live="polite">{table ? `共 ${table.rows.length} 条${rows.length !== table.rows.length ? `，筛选后 ${rows.length} 条` : ''}` : ''}</span>
      <div className="flex items-center gap-3">
        {table && <>
          <button type="button" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)} className="cursor-pointer text-ink-2 disabled:cursor-default disabled:opacity-40">上一页</button>
          <span aria-live="polite">{currentPage} / {pages}</span>
          <button type="button" disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)} className="cursor-pointer text-ink-2 disabled:cursor-default disabled:opacity-40">下一页</button>
        </>}
        <button type="button" onClick={onClose} className="cursor-pointer rounded-lg bg-ink-1 px-4 py-2 text-[13px] text-surface">关闭</button>
      </div>
    </div>}>
    {table ? <>
      {table.description && <p className="mb-3 text-[12px] leading-relaxed text-ink-3">{table.description}</p>}
      <div className="mb-3 flex flex-wrap gap-2">
        <input type="search" aria-label="搜索查询记录" value={query} placeholder="搜索全部字段"
          onChange={(event) => { setQuery(event.target.value); setPage(1) }}
          className="min-w-40 flex-1 rounded-lg border border-line bg-fill px-3 py-2 text-[13px] text-ink-1 outline-none focus:border-accent" />
        {options.map(({ column, values }) => <select key={column.key} aria-label={`筛选${column.label}`} value={filters[column.key] ?? ''}
          onChange={(event) => { setFilters((current) => ({ ...current, [column.key]: event.target.value })); setPage(1) }}
          className="max-w-[200px] rounded-lg border border-line bg-fill px-2 py-2 text-[12px] text-ink-2">
          <option value="">全部{column.label}</option>
          {values.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>)}
      </div>
      <div role="region" aria-label="查询记录表格" tabIndex={0} className="min-h-[300px] max-h-[52vh] overflow-auto rounded-lg border border-line outline-none focus-visible:ring-1 focus-visible:ring-accent">
        <table className="w-full table-fixed border-separate border-spacing-0 text-left text-[12px]" style={{ minWidth: Math.max(600, table.columns.length * 132) }}>
          <thead className="text-ink-2"><tr>
            {/* 暗色 fill 为半透明；固定表头先铺 surface，遮住滚动正文，分隔线随单元格固定。 */}
            {table.columns.map((column) => <th key={column.key} scope="col" className="sticky top-0 z-1 border-b border-line bg-surface bg-[linear-gradient(var(--color-fill),var(--color-fill))] px-3 py-2 font-normal"
              aria-sort={sort?.key === column.key ? sort.descending ? 'descending' : 'ascending' : 'none'}>
              <button type="button" onClick={() => { setSort({ key: column.key, descending: sort?.key === column.key ? !sort.descending : false }); setPage(1) }}
                className="flex w-full cursor-pointer items-center gap-2 text-left">
                {column.label}<span aria-hidden="true" className={`w-3 shrink-0 text-center ${sort?.key === column.key ? 'text-accent' : 'text-ink-3'}`}>{sort?.key === column.key ? sort.descending ? '↓' : '↑' : '↕'}</span>
              </button>
            </th>)}
          </tr></thead>
          <tbody className="text-ink-1">
            {rows.slice((currentPage - 1) * pageSize, currentPage * pageSize).map((index) => <tr key={index} className="[&:last-child>td]:border-b-0">
              {table.columns.map((column, columnIndex) => <td key={column.key} className="border-b border-line whitespace-pre-wrap px-3 py-2 align-top tabular-nums [overflow-wrap:anywhere]">{validationCellText(table.rows[index][columnIndex])}</td>)}
            </tr>)}
          </tbody>
        </table>
        {rows.length === 0 && <p className="sticky left-0 py-12 text-center text-[13px] text-ink-3">{table.rows.length === 0 ? '本次查询无记录' : '没有匹配的记录'}</p>}
      </div>
    </> : <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 text-[13px] text-ink-3" aria-live="polite">
      <p role={error ? 'alert' : 'status'}>{error ?? '正在读取查询记录…'}</p>
      {error && <button type="button" onClick={onRetry} className="cursor-pointer text-accent">重新读取</button>}
    </div>}
  </CenteredModal>
}
