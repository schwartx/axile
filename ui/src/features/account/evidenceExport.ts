/** 证据范围与文件名：页面筛选和绩效快照不会被冒充为后端导出条件。 */
export type EvidenceTarget =
  | { kind: 'execution'; accountId: number; executionId: string }
  | { kind: 'account'; accountId: number }

export interface EvidenceDates { from: string; to: string }
export type EvidenceProfile = 'shareable' | 'internal'
export interface EvidenceOptions { profile: EvidenceProfile; includeLogs: boolean; dates: EvidenceDates }

/** 上海自然日，不受浏览器所在时区影响。 */
export function evidenceDay(timestamp: number): string {
  if (!Number.isFinite(timestamp)) return ''
  try { return new Date(timestamp + 8 * 3600000).toISOString().slice(0, 10) } catch { return '' }
}

/** 把列表的半开窗口变为首尾都包含的日期，不缩短超过 31 天的选择。 */
export function evidenceDatesFromWindow(window: { start: number; end: number } | null): EvidenceDates {
  return window ? { from: evidenceDay(window.start), to: evidenceDay(window.end - 1) } : { from: '', to: '' }
}

/** 绩效区间的起点不含、终点包含；不能确定日期时让用户显式选择。 */
export function evidenceDatesFromSnapshot(scope: { day?: string; start?: number; end?: number }): EvidenceDates {
  if (scope.day) return { from: scope.day, to: scope.day }
  if (scope.start != null && scope.end != null) {
    return { from: evidenceDay(scope.start + 1), to: evidenceDay(scope.end) }
  }
  return { from: '', to: '' }
}

function dayNumber(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const time = Date.parse(value + 'T00:00:00Z')
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value ? time / 86400000 : null
}

export function evidenceDateError(dates: EvidenceDates): string | null {
  if (!dates.from || !dates.to) return '请选择开始和结束日期。'
  const first = dayNumber(dates.from), last = dayNumber(dates.to)
  if (first == null || last == null) return '请选择有效日期。'
  if (last < first) return '结束日期不能早于开始日期。'
  if (last - first + 1 > 31) return '单次最多导出 31 天，请调整日期范围。'
  return null
}

export function evidenceUrl(target: EvidenceTarget, options: EvidenceOptions): string {
  const params = new URLSearchParams({ profile: options.profile, include_logs: String(options.includeLogs) })
  if (target.kind === 'execution') {
    return '/account/executions/' + encodeURIComponent(target.executionId) + '/evidence?' + params
  }
  params.set('start_date', options.dates.from)
  params.set('end_date', options.dates.to)
  params.set('timezone', 'Asia/Shanghai')
  return '/account/' + target.accountId + '/evidence?' + params
}

/** 分享副本的文件名不带真实账户名；内部副本标明类型，避免混淆。 */
export function evidenceFilename(target: EvidenceTarget, options: EvidenceOptions): string {
  const scope = target.kind === 'execution'
    ? 'execution-' + (target.executionId.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 64) || 'unknown')
    : options.dates.from + '_' + options.dates.to
  return 'axile-evidence-' + scope + (options.profile === 'internal' ? '-internal' : '') + '.zip'
}
