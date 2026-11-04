import { replayCommandFailed, replayObject } from '@/features/replay/model'
import type { ReplayContext, ReplayData, ReplayDisplayField, ReplayGap, ReplayInstrument, ReplayRange, ReplayRecording, ReplayWindow } from '@/features/replay/types'

const dateParts = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })

export function replayTime(timeUs: number, date = false, micros = true): string {
  if (!Number.isSafeInteger(timeUs)) return '时间未知'
  const values = Object.fromEntries(dateParts.formatToParts(new Date(Math.floor(timeUs / 1000))).map(part => [part.type, part.value]))
  const clock = `${values.hour}:${values.minute}:${values.second}`
  const fraction = micros ? `.${String(((timeUs % 1_000_000) + 1_000_000) % 1_000_000).padStart(6, '0')}` : ''
  return `${date ? `${values.year}-${values.month}-${values.day} ` : ''}${clock}${fraction}`
}

export function replayNumber(value: number | null | undefined, decimals: number | null | undefined = 6): string {
  return value == null || !Number.isFinite(value) ? '未知' : value.toLocaleString('zh-CN', { maximumFractionDigits: Math.max(0, Math.min(12, decimals ?? 6)) })
}

/** 价格尺按品种精度补齐尾零，元数据未知时不伪造精度。 */
export function replayAxisPrice(value: number | null, instrument: ReplayInstrument): string {
  if (value == null || !Number.isFinite(value)) return '未知'
  const tick = instrument.price_tick
  const decimals = instrument.price_decimals ?? (tick != null && Number.isFinite(tick) && tick > 0
    ? (tick.toLocaleString('en-US', { useGrouping: false, maximumFractionDigits: 12 }).split('.')[1]?.length ?? 0) : null)
  if (decimals == null) return replayNumber(value)
  const digits = Math.max(0, Math.min(12, decimals))
  return value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function replayFieldText(value: unknown, field: ReplayDisplayField): string | null {
  if (value == null) return null
  if (field.kind === 'number') return typeof value === 'number' && Number.isFinite(value) ? `${replayNumber(value, field.decimals)}${field.unit ? ` ${field.unit}` : ''}` : null
  if (field.kind === 'bool') return typeof value === 'boolean' ? value ? '是' : '否' : null
  if (field.kind === 'enum') return typeof value === 'string' || typeof value === 'number' ? field.values?.[String(value)] ?? `未识别（${String(value)}）` : null
  return typeof value === 'string' ? value : null
}

export function replayCommandPresentation(data: ReplayData): { failed: boolean; text: string } {
  const failed = replayCommandFailed(data), quality = replayObject(data.field_quality).phase
  const phase = quality === 'missing' || quality === 'invalid' ? null : data.phase
  const text = failed ? '观察到请求错误，订单状态仍以回报为准'
    : phase === 'request' || phase === 'native_request' ? '请求已发出，确认另见订单回报'
    : ['returned', 'result', 'native_return', 'native_returned'].includes(String(phase)) ? '同步结果已返回，场内状态以回报为准'
    : '操作结果未知，订单状态以回报为准'
  return { failed, text }
}

export function replayStream(context: ReplayContext, symbol: string, name: string) {
  const actual = context.streams?.[symbol]?.[name]
  if (actual) return actual
  const declared = context.stream_defaults?.[name]
  return declared ? { ...declared, availability: declared.availability === 'unavailable' ? 'unavailable' as const : 'partial' as const,
    reason: declared.reason ?? '仅有渠道能力声明，具体覆盖以记录为准' } : undefined
}

export function replayInstrument(context: ReplayContext, symbol: string): ReplayInstrument {
  return { quantity_label: context.units?.quantity_label, quantity_decimals: context.units?.quantity_max_decimals,
    price_label: context.units?.price_label, ...context.instrument?.[symbol] }
}

/** 目录与窗口只提供品种名称；历史价格和状态仍只能按游标投影。 */
export function replaySymbols(source: ReplayRecording | ReplayWindow, known: readonly string[] = []): string[] {
  const symbols = new Set(Array.isArray(known) ? known : [])
  for (const symbol of Object.keys(source.context.instrument ?? {})) symbols.add(symbol)
  if ('symbols' in source) for (const symbol of source.symbols ?? []) symbols.add(symbol)
  if ('messages' in source) {
    const observations: unknown[] = source.messages.map(event => event.data)
    for (const field of ['quotes', 'orders', 'commands', 'connections', 'observed_positions', 'derived_positions', 'executions'] as const) {
      const values = source.state_before_start?.[field]
      if (values && typeof values === 'object') observations.push(...Object.values(values))
    }
    for (const raw of observations) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
      const data = raw as Record<string, unknown>
      for (const value of [data, ...(Array.isArray(data.orders) ? data.orders : []), ...(Array.isArray(data.positions) ? data.positions : [])]) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) continue
        const row = value as Record<string, unknown>
        if (typeof row.symbol === 'string') symbols.add(row.symbol)
        if (Array.isArray(row.symbols)) for (const symbol of row.symbols) if (typeof symbol === 'string') symbols.add(symbol)
        const scope = row.scope && typeof row.scope === 'object' && !Array.isArray(row.scope) ? row.scope as Record<string, unknown> : null
        const scopedSymbols = Array.isArray(row.scope) ? row.scope : scope?.symbols
        if (Array.isArray(scopedSymbols)) for (const symbol of scopedSymbols) if (typeof symbol === 'string') symbols.add(symbol)
        if (row.by_symbol && typeof row.by_symbol === 'object' && !Array.isArray(row.by_symbol)) for (const symbol of Object.keys(row.by_symbol)) symbols.add(symbol)
      }
    }
  }
  return [...symbols].filter(Boolean)
}

export function rememberReplaySymbols(previous: Record<string, string[]>, window: ReplayWindow): Record<string, string[]> {
  const known = Object.hasOwn(previous, window.recording_id) ? previous[window.recording_id] : []
  const symbols = replaySymbols(window, known)
  return symbols.length === known.length ? previous : { ...previous, [window.recording_id]: symbols }
}

export function replayWindowRequest(recording: ReplayRecording, range: ReplayRange, symbol: string) {
  return { ...range, recording_id: recording.recording_id,
    ...(recording.symbols_complete === true && symbol ? { symbols: [symbol] } : {}) }
}

/** 全程时间轴保留真实缺口，不把缺口变成可用覆盖。 */
export function replayExecutionTimeline(coverage: readonly ReplayRange[], start?: number | null, end?: number | null): { range: ReplayRange; gaps: ReplayGap[] } | null {
  if (!coverage.length) return null
  const first = coverage[0].start_us, last = coverage[coverage.length - 1].end_us
  const from = start ?? first
  const to = end == null ? last : Math.min(Number.MAX_SAFE_INTEGER, end + 1)
  if (from >= to) return null
  const gaps: ReplayGap[] = []
  let cursor = from
  for (const interval of coverage) {
    if (interval.end_us <= from || interval.start_us >= to) continue
    if (interval.start_us > cursor) gaps.push({ start_us: cursor, end_us: Math.min(interval.start_us, to), reason: '录制未覆盖' })
    cursor = Math.max(cursor, Math.min(interval.end_us, to))
  }
  if (cursor < to) gaps.push({ start_us: cursor, end_us: to, reason: '录制未覆盖' })
  return { range: { start_us: from, end_us: to }, gaps }
}

/** 每个覆盖段保留原录制身份和基线，不能按时间把不同录制段的序号拼接。 */
export function replayExecutionPlan(manifest: { recordings: ReplayRecording[]; execution_start_us?: number | null; execution_end_us?: number | null }): { range: ReplayRange; gaps: ReplayGap[]; segments: { recording: ReplayRecording; range: ReplayRange }[] } | null {
  const coverage = manifest.recordings.flatMap(recording => recording.coverage).toSorted((a, b) => a.start_us - b.start_us || a.end_us - b.end_us)
  const timeline = replayExecutionTimeline(coverage, manifest.execution_start_us, manifest.execution_end_us)
  if (!timeline) return null
  const segments = manifest.recordings.flatMap(recording => recording.coverage.flatMap(interval => {
    const start_us = Math.max(interval.start_us, timeline.range.start_us)
    const end_us = Math.min(interval.end_us, timeline.range.end_us)
    return start_us < end_us ? [{ recording, range: { start_us, end_us } }] : []
  })).toSorted((a, b) => a.range.start_us - b.range.start_us || a.range.end_us - b.range.end_us)
  return { ...timeline, segments }
}

/** 只取同一已覆盖段，跨缺口时另选窗口，不将 UTC 拼成一条连续录制。 */
export function replayRange(coverage: ReplayRange[], start?: number, durationUs = 20_000_000): ReplayRange | null {
  const range = start == null ? coverage[0] : coverage.find(item => start >= item.start_us && start < item.end_us)
  if (!range) return null
  const from = start ?? range.start_us
  return { start_us: from, end_us: Math.min(range.end_us, from + durationUs) }
}

/** 优先定位本次执行开始后的覆盖段，保留选择其他历史时段的能力。 */
export function replayExecutionRange(coverage: ReplayRange[], startUs: number | null | undefined, durationUs: number): ReplayRange | null {
  if (startUs == null) return replayRange(coverage, undefined, durationUs)
  const range = coverage.find(item => item.end_us > startUs)
  return range ? replayRange(coverage, Math.max(range.start_us, startUs), durationUs) : null
}

export function replayWindowDuration(value: string): number {
  const seconds = Number(value)
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1_000_000 : 10_000_000
}

/** 缩小时保留用户要定位的时刻；单个微秒不能继续缩小。 */
export function smallerReplayRange(range: ReplayRange, target?: number | null): ReplayRange | null {
  const duration = Math.floor((range.end_us - range.start_us) / 2)
  if (duration < 1) return null
  const anchor = target != null && target >= range.start_us && target < range.end_us ? target : range.start_us
  const start = Math.min(anchor, range.end_us - duration)
  return { start_us: start, end_us: start + duration }
}

export function replayGapAt(gaps: ReplayGap[], timeUs: number, symbol?: string): ReplayGap | undefined {
  return gaps.find(gap => timeUs >= gap.start_us && timeUs < gap.end_us && (!gap.symbol || !symbol || gap.symbol === symbol))
}

export const replayStatusLabels: Record<string, string> = { pending: '请求中', working: '已受理', partially_filled: '部分成交', pending_cancel: '撤单待确认', filled: '已成交', canceled: '已撤销', rejected: '已拒绝', expired: '已过期', unknown: '状态未知', trigger_wait: '等待触发', activated: '已激活，待确认场内状态' }
