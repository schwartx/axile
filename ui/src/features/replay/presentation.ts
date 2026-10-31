import type { ReplayContext, ReplayDisplayField, ReplayGap, ReplayInstrument, ReplayRange, ReplayWindow } from '@/features/replay/types'

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

export function replayFieldText(value: unknown, field: ReplayDisplayField): string | null {
  if (value == null) return null
  if (field.kind === 'number') return typeof value === 'number' && Number.isFinite(value) ? `${replayNumber(value, field.decimals)}${field.unit ? ` ${field.unit}` : ''}` : null
  if (field.kind === 'bool') return typeof value === 'boolean' ? value ? '是' : '否' : null
  if (field.kind === 'enum') return typeof value === 'string' || typeof value === 'number' ? field.values?.[String(value)] ?? `未识别（${String(value)}）` : null
  return typeof value === 'string' ? value : null
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

/** 品种名称可从窗口发现；历史价格和状态仍只能按游标投影。 */
export function replaySymbols(window: ReplayWindow): string[] {
  const symbols = new Set(Object.keys(window.context.instrument ?? {}))
  for (const event of window.messages) {
    if (typeof event.data.symbol === 'string') symbols.add(event.data.symbol)
    if (Array.isArray(event.data.symbols)) for (const symbol of event.data.symbols) if (typeof symbol === 'string') symbols.add(symbol)
  }
  for (const field of ['quotes', 'orders', 'observed_positions'] as const) {
    const values = window.state_before_start?.[field]
    if (values && typeof values === 'object') for (const raw of Object.values(values)) {
      if (raw && typeof raw === 'object' && typeof raw.symbol === 'string') symbols.add(raw.symbol)
    }
  }
  return [...symbols].filter(Boolean)
}

/** 只取同一已覆盖段，跨缺口时另选窗口，不将 UTC 拼成一条连续录制。 */
export function replayRange(coverage: ReplayRange[], start?: number, durationUs = 20_000_000): ReplayRange | null {
  const range = start == null ? coverage[0] : coverage.find(item => start >= item.start_us && start < item.end_us)
  if (!range) return null
  const from = start ?? range.start_us
  return { start_us: from, end_us: Math.min(range.end_us, from + durationUs) }
}

export function replayGapAt(gaps: ReplayGap[], timeUs: number, symbol?: string): ReplayGap | undefined {
  return gaps.find(gap => timeUs >= gap.start_us && timeUs < gap.end_us && (!gap.symbol || !symbol || gap.symbol === symbol))
}
