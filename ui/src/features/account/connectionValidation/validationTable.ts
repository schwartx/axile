import type { ValidationCell, ValidationTable, ValidationTableColumn } from '@/lib/api/connectionValidation'

const collator = new Intl.Collator('zh-CN', { numeric: true })
export const validationCellText = (value: ValidationCell | undefined): string => value == null || value === '' ? '未返回' : String(value)
const missing = (value: ValidationCell | undefined) => value == null || value === ''

/** 空值始终排在最后；编号列保持字符串，数值列按数值排序。 */
function compare(a: ValidationCell | undefined, b: ValidationCell | undefined, column: ValidationTableColumn, descending: boolean): number {
  if (missing(a) || missing(b)) return Number(missing(a)) - Number(missing(b))
  const result = column.type === 'number' ? Number(a) - Number(b) : collator.compare(String(a), String(b))
  return descending ? -result : result
}

/** 返回原始行索引，搜索、过滤、排序均不改变本次验证快照。 */
export function selectValidationRows(table: ValidationTable, query: string, filters: Record<string, string>, sort: { key: string; descending: boolean } | null): number[] {
  const needle = query.trim().toLocaleLowerCase()
  const activeFilters = table.columns.flatMap((column, index) => filters[column.key] ? [[index, filters[column.key]] as const] : [])
  const indices = table.rows.flatMap((row, index) => {
    if (needle && !row.some((cell) => cell != null && String(cell).toLocaleLowerCase().includes(needle))) return []
    if (!activeFilters.every(([column, value]) => validationCellText(row[column]) === value)) return []
    return [index]
  })
  const sortIndex = table.columns.findIndex((column) => column.key === sort?.key)
  if (sort && sortIndex >= 0) indices.sort((a, b) => compare(table.rows[a][sortIndex], table.rows[b][sortIndex], table.columns[sortIndex], sort.descending) || a - b)
  return indices
}
