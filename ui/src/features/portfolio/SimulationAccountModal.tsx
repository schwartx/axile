import { Fragment, useEffect, useId, useRef, useState } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { AmountInput } from '@/components/ui/AmountInput'
import { Segmented } from '@/components/ui/Segmented'
import { Select } from '@/components/ui/Select'
import { LeverageInput } from '@/features/account/LeverageInput'
import { leverageError } from '@/features/account/leverage'
import type { ChannelCapability, SimulationAccount, SimulationPosition, SimulationQuote } from '@/types/api'

type Section = 'account' | 'positions' | 'quotes'
type PositionDraft = { id: string; symbol: string; direction: string; volume: string; available_volume: string; market_value: string; avg_price: string; extra: string }
type QuoteDraft = { id: string; symbol: string; last_price: string; volume: string; turnover: string; timestamp: string; update_time: string; bids: string; asks: string; extra: string }
export interface SimulationAccountDraft {
  account: { total_asset: string; available_cash: string; market_value: string; currency: string; source: string; update_time: string; extra: string }
  positions: PositionDraft[]
  quotes: QuoteDraft[]
  long_leverage: string
  short_leverage: string
}
interface Issue { field: string; section: Section; message: string; row?: string }
const text = (value: number | null | undefined) => value == null ? '' : String(value)
const json = (value: unknown) => value === undefined ? '' : JSON.stringify(value, null, 2)

/** 保留数值输入原文，直到保存才解析，避免空输入被静默转换成零。 */
// oxlint-disable-next-line react/only-export-components -- 表单模型同源，供独立测试直接验证。
export function simulationAccountDraft(value: SimulationAccount): SimulationAccountDraft {
  const account = value.account
  return {
    account: { total_asset: text(account.total_asset), available_cash: text(account.available_cash), market_value: text(account.market_value), currency: account.currency, source: account.source ?? '', update_time: account.update_time ?? '', extra: json(account.extra) },
    positions: account.positions.map((row) => ({ id: crypto.randomUUID(), symbol: row.symbol, direction: row.direction, volume: text(row.volume), available_volume: text(row.available_volume), market_value: text(row.market_value), avg_price: text(row.avg_price), extra: json(row.extra) })),
    quotes: value.quotes.map((row) => ({ id: crypto.randomUUID(), symbol: row.symbol, last_price: text(row.last_price), volume: text(row.volume), turnover: text(row.turnover), timestamp: text(row.timestamp), update_time: row.update_time, bids: json(row.bids), asks: json(row.asks), extra: json(row.extra) })),
    long_leverage: text(value.long_leverage), short_leverage: text(value.short_leverage),
  }
}

function finiteJson(value: unknown): boolean {
  if (typeof value === 'number') return Number.isFinite(value)
  if (Array.isArray(value)) return value.every(finiteJson)
  return value === null || typeof value !== 'object' || Object.values(value).every(finiteJson)
}

/** 同时验证所有页签；每条错误携带所属字段和行，供界面切换并定位。 */
// oxlint-disable-next-line react/only-export-components -- 验证与表单同源，不增加独立模型文件。
export function validateSimulationAccount(draft: SimulationAccountDraft, channel: ChannelCapability): { value?: SimulationAccount; issues: Issue[]; marketValue: number | null } {
  const issues: Issue[] = []
  const issue = (field: string, message: string) => {
    const [section, row] = field.split('.')
    issues.push({ field, section: section === 'positions' || section === 'quotes' ? section : 'account', row, message })
  }
  const number = (raw: string, field: string, nullable = false): number | null => {
    if (!raw.trim() && nullable) return null
    if (!raw.trim() || !Number.isFinite(Number(raw))) { issue(field, '请输入有限数值'); return null }
    return Number(raw)
  }
  const nonnegative = (raw: string, field: string): number => {
    const value = number(raw, field)
    if (value !== null && value < 0) issue(field, '需为非负数')
    return value ?? 0
  }
  const required = (raw: string, field: string) => {
    const value = raw.trim()
    if (!value) issue(field, '不能为空')
    return value
  }
  const parseJson = (raw: string, field: string): unknown => {
    try {
      const value: unknown = JSON.parse(raw)
      if (!finiteJson(value)) throw new Error()
      return value
    } catch { issue(field, '请输入合法 JSON，所有数值必须有限'); return undefined }
  }
  const extra = (raw: string, field: string): Record<string, unknown> | undefined => {
    if (!raw.trim()) return undefined
    const value = parseJson(raw, field)
    if (value === undefined) return undefined
    if (!value || typeof value !== 'object' || Array.isArray(value)) { issue(field, '扩展字段需为 JSON 对象'); return undefined }
    return value as Record<string, unknown>
  }
  const levels = (raw: string, field: string): [number, number][] => {
    const value = parseJson(raw, field)
    if (value === undefined) return []
    if (!Array.isArray(value) || !value.every((pair: unknown) => Array.isArray(pair) && pair.length === 2 && pair.every((n: unknown) => typeof n === 'number' && Number.isFinite(n)) && pair[0] > 0 && pair[1] >= 0)) {
      issue(field, '盘口需为 [[价格, 非负数量], …]'); return []
    }
    return value as [number, number][]
  }
  const seenPositions = new Set<string>()
  const positions: SimulationPosition[] = draft.positions.map((row) => {
    const prefix = `positions.${row.id}`
    const symbol = required(row.symbol, `${prefix}.symbol`)
    const key = JSON.stringify([symbol, row.direction])
    if (seenPositions.has(key)) issue(`${prefix}.symbol`, '同标的同方向持仓不能重复')
    seenPositions.add(key)
    if (!['多头', '空头', '净'].includes(row.direction)) issue(`${prefix}.direction`, '请选择多头、空头或净')
    const volume = nonnegative(row.volume, `${prefix}.volume`)
    const available = nonnegative(row.available_volume, `${prefix}.available_volume`)
    if (available > volume) issue(`${prefix}.available_volume`, '可用量不能大于持仓量')
    const avgPrice = number(row.avg_price, `${prefix}.avg_price`, true)
    if (avgPrice !== null && avgPrice < 0) issue(`${prefix}.avg_price`, '成本价不能为负数')
    return { symbol, direction: row.direction as SimulationPosition['direction'], volume, available_volume: available, market_value: null, avg_price: avgPrice, extra: extra(row.extra, `${prefix}.extra`) }
  })
  const seenQuotes = new Set<string>()
  const quotes: SimulationQuote[] = draft.quotes.map((row) => {
    const prefix = `quotes.${row.id}`
    const symbol = required(row.symbol, `${prefix}.symbol`)
    if (seenQuotes.has(symbol)) issue(`${prefix}.symbol`, '行情标的不能重复')
    seenQuotes.add(symbol)
    const lastPrice = number(row.last_price, `${prefix}.last_price`) ?? 0
    if (lastPrice <= 0) issue(`${prefix}.last_price`, '最新价需为正数')
    const quoteExtra = extra(row.extra, `${prefix}.extra`)
    if (channel.units?.quantity_kind === 'contract' && ['volume_multiple', 'price_tick'].some((key) =>
      typeof quoteExtra?.[key] !== 'number' || !Number.isFinite(quoteExtra[key]) || quoteExtra[key] <= 0)) {
      issue(`${prefix}.extra`, '期货行情需配置正数 volume_multiple 和 price_tick')
    }
    return { symbol, last_price: lastPrice, volume: nonnegative(row.volume, `${prefix}.volume`), turnover: row.turnover.trim() ? nonnegative(row.turnover, `${prefix}.turnover`) : undefined, timestamp: nonnegative(row.timestamp, `${prefix}.timestamp`), update_time: required(row.update_time, `${prefix}.update_time`), bids: levels(row.bids, `${prefix}.bids`), asks: levels(row.asks, `${prefix}.asks`), extra: quoteExtra }
  })
  draft.positions.forEach((row) => {
    if (!seenQuotes.has(row.symbol.trim())) issue(`positions.${row.id}.symbol`, `缺少模拟行情：${row.symbol.trim()}`)
  })
  const currency = required(draft.account.currency, 'account.currency')
  if (channel.currency && currency !== channel.currency) issue('account.currency', `渠道币种需为 ${channel.currency}`)
  if (/\s/.test(currency)) issue('account.currency', '币种不能包含空白字符')
  for (const field of ['long_leverage', 'short_leverage'] as const) {
    const error = leverageError(draft[field], channel.leverage)
    if (error) issue(field, error)
  }
  for (const position of positions) {
    const quote = quotes.find((item) => item.symbol === position.symbol)
    const multiplier = channel.units?.quantity_kind === 'contract' ? quote?.extra?.volume_multiple : 1
    const marketValue = quote && typeof multiplier === 'number' ? position.volume * quote.last_price * multiplier : NaN
    position.market_value = Number.isFinite(marketValue) ? marketValue : null
  }
  const marketValue = positions.every((position) => position.market_value !== null)
    ? positions.reduce((sum, position) => sum + position.market_value!, 0) : null
  const value: SimulationAccount = {
    account: { total_asset: nonnegative(draft.account.total_asset, 'account.total_asset'), available_cash: nonnegative(draft.account.available_cash, 'account.available_cash'), market_value: marketValue, currency, source: draft.account.source || undefined, update_time: draft.account.update_time || undefined, extra: extra(draft.account.extra, 'account.extra'), positions },
    quotes, long_leverage: Number(draft.long_leverage), short_leverage: Number(draft.short_leverage),
  }
  return issues.length ? { issues, marketValue } : { value, issues, marketValue }
}

const inputClass = 'min-w-0 w-full rounded-md border border-ink-3/30 bg-transparent px-2 py-1.5 text-[13px] outline-none focus:border-accent focus-visible:outline-2 focus-visible:outline-accent aria-invalid:border-warn'
const actionClass = 'shrink-0 cursor-pointer rounded px-2 py-1.5 text-xs text-accent hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:text-ink-3'
const positionColumns = [['symbol', '标的'], ['direction', '方向'], ['volume', '持仓量'], ['available_volume', '可用量'], ['avg_price', '均价']] as const
const quoteColumns = [['symbol', '标的'], ['last_price', '最新价'], ['volume', '成交量']] as const

/** 独立的模拟账户设置草稿；只有保存成功才关闭，不连接真实渠道。 */
export function SimulationAccountModal({ channel, value, onSave, onClose }: {
  channel: ChannelCapability
  value: SimulationAccount
  onSave: (value: SimulationAccount) => void
  onClose: () => void
}) {
  const [cashEdited, setCashEdited] = useState(value.account.available_cash !== value.account.total_asset)
  const [draft, setDraft] = useState(() => simulationAccountDraft(value))
  const [tab, setTab] = useState<'positions' | 'quotes'>('positions')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [issues, setIssues] = useState<Issue[]>([])
  const [saveError, setSaveError] = useState('')
  const [undo, setUndo] = useState<SimulationAccountDraft | null>(null)
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const focusField = useRef('')
  const rootRef = useRef<HTMLDivElement>(null)
  const id = useId()
  const panelId = `${id}-panel`
  useEffect(() => {
    if (!focusField.current) return
    const target = [...(rootRef.current?.querySelectorAll<HTMLElement>('[data-field]') ?? [])].find((node) => node.dataset.field === focusField.current)
    const details = target?.closest('details')
    if (details) details.open = true
    const control = target?.matches('input, textarea, button') ? target : target?.querySelector<HTMLElement>('input, button, textarea')
    control?.focus()
    control?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
    focusField.current = ''
  }, [issues, tab, expanded])
  const change = (next: SimulationAccountDraft) => { setDraft(next); setUndo(null); setSaveError('') }
  const accountChange = (field: keyof SimulationAccountDraft['account'], next: string) => {
    if (field === 'available_cash') setCashEdited(true)
    change({ ...draft, account: { ...draft.account, [field]: next,
      ...(field === 'total_asset' && !cashEdited ? { available_cash: next } : {}) } })
  }
  const marketValue = validateSimulationAccount(draft, channel).marketValue
  const positionChange = (row: PositionDraft, field: keyof PositionDraft, next: string) => change({ ...draft, positions: draft.positions.map((item) => item.id === row.id ? { ...item, [field]: next } : item) })
  const quoteChange = (row: QuoteDraft, field: keyof QuoteDraft, next: string) => change({ ...draft, quotes: draft.quotes.map((item) => item.id === row.id ? { ...item, [field]: next } : item) })
  const errorFor = (field: string) => issues.find((issue) => issue.field === field)
  const locate = (issue: Issue) => {
    focusField.current = issue.field
    if (issue.section !== 'account') setTab(issue.section)
    if (issue.row) setExpanded((current) => new Set([...current, issue.row!]))
    setIssues((current) => [...current])
  }
  const input = (field: string, label: string, raw: string, onChange: (next: string) => void, numeric = false) => <input
    aria-label={label} data-field={field} aria-invalid={!!errorFor(field)} aria-describedby={errorFor(field) ? `${id}-errors` : undefined}
    title={errorFor(field)?.message} value={raw} onChange={(event) => onChange(event.target.value)} inputMode={numeric ? 'decimal' : undefined}
    className={`${inputClass} ${numeric ? 'tabular-nums' : ''}`} />
  const jsonInput = (field: string, label: string, raw: string, onChange: (next: string) => void) => <label className="flex min-w-0 flex-col gap-1 text-xs text-ink-2">
    {label}<textarea aria-label={label} data-field={field} aria-invalid={!!errorFor(field)} aria-describedby={errorFor(field) ? `${id}-errors` : undefined}
      value={raw} onChange={(event) => onChange(event.target.value)} rows={4} spellCheck={false} placeholder="{}"
      className={`${inputClass} quiet-scrollbar resize-y font-mono`} />
  </label>
  const toggle = (rowId: string) => setExpanded((current) => {
    const next = new Set(current)
    if (next.has(rowId)) next.delete(rowId); else next.add(rowId)
    return next
  })
  const add = () => {
    const rowId = crypto.randomUUID()
    if (tab === 'positions') change({ ...draft, positions: [...draft.positions, { id: rowId, symbol: '', direction: '多头', volume: '0', available_volume: '0', market_value: '', avg_price: '', extra: '' }] })
    else change({ ...draft, quotes: [...draft.quotes, { id: rowId, symbol: '', last_price: '0', volume: '0', turnover: '', timestamp: '0', update_time: '', bids: '[]', asks: '[]', extra: '' }] })
    focusField.current = `${tab}.${rowId}.symbol`
    setIssues((current) => [...current])
  }
  const save = async () => {
    if (savingRef.current) return
    const result = validateSimulationAccount({ ...draft, account: { ...draft.account, currency: channel.currency } }, channel)
    setIssues(result.issues)
    setSaveError('')
    if (!result.value) { if (result.issues[0]) locate(result.issues[0]); return }
    savingRef.current = true
    setSaving(true)
    try { await onSave(result.value); onClose() }
    catch (error) { setSaveError(error instanceof Error && error.message ? error.message : '保存配置失败，请重试') }
    finally { savingRef.current = false; setSaving(false) }
  }
  const longError = leverageError(draft.long_leverage, channel.leverage)
  const shortError = leverageError(draft.short_leverage, channel.leverage)
  const rowLabel = (section: Section, rowId?: string) => section === 'account' ? '资金与杠杆' : `${section === 'positions' ? '持仓' : '行情'}第 ${(section === 'positions' ? draft.positions : draft.quotes).findIndex((row) => row.id === rowId) + 1} 行`

  return <CenteredModal open title="模拟账户设置" subtitle={channel.label} onClose={onClose} closeDisabled={saving}
    widthClassName="w-[1200px] h-[min(760px,86dvh)]" scrollBody={false}
    footer={<div className="mx-[22px] border-t border-line py-3">
      {(issues.length > 0 || saveError) && <div id={`${id}-errors`} role="alert" className="mb-2 text-xs text-warn">
        {saveError || <><p>有 {issues.length} 项需要修正，草稿尚未保存。</p><div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">{(['account', 'positions', 'quotes'] as const).map((section) => {
          const issue = issues.find((item) => item.section === section)
          return issue && <button key={section} type="button" className="cursor-pointer text-left underline underline-offset-2" onClick={() => locate(issue)}>定位{rowLabel(section, issue.row)}：{issue.message}</button>
        })}</div></>}
      </div>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <span className="mr-auto text-xs text-ink-3">保存后用于后续试跑</span>
        <button type="button" disabled={saving} onClick={onClose} className="rounded-[9px] border border-line px-4 py-2 text-sm text-ink-2 hover:bg-fill disabled:opacity-45">取消</button>
        <button type="button" disabled={saving} onClick={() => void save()} className="cursor-pointer rounded-[9px] bg-ink-1 px-4 py-2 text-sm font-semibold text-surface disabled:cursor-default disabled:opacity-45">{saving ? '保存中…' : '保存配置'}</button>
      </div>
    </div>}>
    <div ref={rootRef} className="quiet-scrollbar grid min-h-0 min-w-0 flex-1 auto-rows-max grid-cols-1 gap-4 overflow-y-auto overscroll-contain py-1 lg:auto-rows-auto lg:grid-cols-[340px_minmax(0,1fr)] lg:grid-rows-1 lg:overflow-hidden">
      <fieldset disabled={saving} className="quiet-scrollbar min-h-0 min-w-0 space-y-5 lg:overflow-y-auto lg:border-r lg:border-line lg:pr-4">
        <section aria-label="模拟账户资金" className="space-y-3">
          <h3 className="text-sm font-semibold">资金<span className="ml-2 text-xs font-normal text-ink-3">{channel.currency}</span></h3>
          {([['total_asset', '账户权益'], ['available_cash', '可用资金']] as const).map(([field, label]) => <label key={field} data-field={`account.${field}`} className="flex flex-col gap-1 text-xs text-ink-2">
            {label}<AmountInput value={draft.account[field]} onChange={(next) => accountChange(field, next)}
              unit={channel.currency === 'CNY' ? '元' : channel.currency} ariaLabel={label}
              invalid={!!errorFor(`account.${field}`)} describedBy={errorFor(`account.${field}`) ? `${id}-errors` : undefined} />
          </label>)}
          <div className="space-y-1 text-xs text-ink-2">
            <span>持仓市值</span>
            <p className="tabular-nums text-sm text-ink-1">{marketValue === null ? '—' : `${marketValue.toLocaleString()} ${channel.currency === 'CNY' ? '元' : channel.currency}`}</p>
          </div>
          <p className="text-xs leading-relaxed text-ink-3">持仓市值按持仓量、最新价及合约乘数自动计算。</p>
        </section>
        <section aria-label="执行杠杆" className="space-y-3 border-t border-line pt-4">
          <h3 className="text-sm font-semibold">执行杠杆</h3>
          {(['long_leverage', 'short_leverage'] as const).filter((field) => field === 'long_leverage' || channel.ui.show_short_leverage).map((field) => {
            const error = field === 'long_leverage' ? longError : shortError
            const label = field === 'long_leverage' ? channel.ui.long_leverage_label : channel.ui.short_leverage_label
            return <div key={field} data-field={field} className="space-y-1">
              <label htmlFor={`${id}-${field}`} className="text-xs text-ink-2">{label || (field === 'long_leverage' ? '多头杠杆' : '空头杠杆')}</label>
              <LeverageInput id={`${id}-${field}`} value={draft[field]} limits={channel.leverage} invalid={!!error} describedBy={error ? `${id}-${field}-error` : undefined} onChange={(next) => change({ ...draft, [field]: next })} />
              {error && <p id={`${id}-${field}-error`} className="text-xs text-warn">{error}</p>}
            </div>
          })}
        </section>
        <details className="border-t border-line pt-3">
          <summary className="cursor-pointer text-xs text-ink-2">账户来源与扩展字段</summary>
          <div className="mt-3 space-y-3">
            <label className="flex flex-col gap-1 text-xs text-ink-2">来源{input('account.source', '账户来源 source', draft.account.source, (next) => accountChange('source', next))}</label>
            <label className="flex flex-col gap-1 text-xs text-ink-2">更新时间{input('account.update_time', '账户更新时间 update_time', draft.account.update_time, (next) => accountChange('update_time', next))}</label>
            {jsonInput('account.extra', '账户扩展字段 extra JSON', draft.account.extra, (next) => accountChange('extra', next))}
          </div>
        </details>
        <div className="border-t border-line pt-3">
          <button type="button" disabled={!channel.simulation_account} className={actionClass} onClick={() => {
            if (!channel.simulation_account) return
            setUndo(draft); setCashEdited(false); setDraft(simulationAccountDraft(channel.simulation_account)); setIssues([]); setSaveError('')
          }}>恢复渠道预设</button>
          {undo && <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-ink-2" role="status">仅草稿已恢复<button type="button" className={actionClass} onClick={() => { setDraft(undo); setCashEdited(undo.account.available_cash !== undo.account.total_asset); setUndo(null); setIssues([]); setSaveError('') }}>撤回恢复</button></div>}
        </div>
      </fieldset>
      <fieldset disabled={saving} className="flex min-h-0 min-w-0 flex-col gap-3">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
          <Segmented value={tab} size="sm" ariaLabel="模拟账户数据" panelId={panelId} onChange={setTab} options={[
            { value: 'positions', label: `持仓 (${draft.positions.length})`, badge: issues.some((issue) => issue.section === 'positions') ? <span className="text-warn">需修正</span> : undefined },
            { value: 'quotes', label: `行情与合约 (${draft.quotes.length})`, badge: issues.some((issue) => issue.section === 'quotes') ? <span className="text-warn">需修正</span> : undefined },
          ]} />
          <button type="button" className={actionClass} onClick={add}>添加{tab === 'positions' ? '持仓' : '行情'}</button>
        </div>
        <p className="shrink-0 text-xs text-ink-3">{tab === 'positions' ? '同一标的可按不同方向分别配置；展开行可编辑合约扩展字段。' : '最新价始终可见；展开行配置盘口、时间及合约乘数 / 最小变动价位等 extra 字段。'}</p>
        <div id={panelId} role="tabpanel" aria-labelledby={`${panelId}-${tab}`} className="quiet-scrollbar isolate min-h-0 min-w-0 flex-1 overflow-auto overscroll-contain border-y border-line">
          {tab === 'positions' ? <table className="w-full min-w-[760px] table-fixed text-left text-xs">
            <thead className="sticky top-0 z-10 bg-surface text-ink-3"><tr>{positionColumns.map(([field, label]) => <th key={field} className="px-1 py-2 font-normal">{label}</th>)}<th className="w-[108px] px-1 py-2 font-normal">操作</th></tr></thead>
            <tbody>{draft.positions.map((row, index) => <Fragment key={row.id}>
              <tr className="border-t border-line">{positionColumns.map(([field, label]) => <td key={field} className="px-1 py-2">
                {field === 'direction' ? <div data-field={`positions.${row.id}.direction`}><Select value={row.direction} options={['多头', '空头', '净'].map((direction) => ({ value: direction, label: direction }))} ariaLabel={`持仓 ${index + 1} 方向 direction`} onChange={(next) => positionChange(row, 'direction', next)} className="w-full justify-between px-2 py-1.5 text-xs" /></div>
                  : input(`positions.${row.id}.${field}`, `持仓 ${index + 1} ${label} ${field}`, row[field], (next) => positionChange(row, field, next), field !== 'symbol')}
              </td>)}<td className="px-1 py-2 whitespace-nowrap"><button type="button" className={actionClass} aria-label={`持仓 ${index + 1} 扩展字段`} aria-expanded={expanded.has(row.id)} aria-controls={`${id}-${row.id}`} onClick={() => toggle(row.id)}>详情</button><button type="button" aria-label={`删除持仓 ${index + 1}`} className={`${actionClass} text-ink-2`} onClick={() => { change({ ...draft, positions: draft.positions.filter((item) => item.id !== row.id) }); setIssues([]) }}>删除</button></td></tr>
              <tr><td colSpan={6} className="p-0"><div id={`${id}-${row.id}`} inert={!expanded.has(row.id)} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded.has(row.id) ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><div className="px-2 pb-3">{jsonInput(`positions.${row.id}.extra`, `持仓 ${index + 1} 合约扩展字段 extra JSON`, row.extra, (next) => positionChange(row, 'extra', next))}</div></div></div></td></tr>
            </Fragment>)}</tbody>
          </table> : <table className="w-full min-w-[640px] table-fixed text-left text-xs">
            <thead className="sticky top-0 z-10 bg-surface text-ink-3"><tr>{quoteColumns.map(([field, label]) => <th key={field} className="px-1 py-2 font-normal">{label}</th>)}<th className="w-[140px] px-1 py-2 font-normal">盘口与合约</th></tr></thead>
            <tbody>{draft.quotes.map((row, index) => <Fragment key={row.id}>
              <tr className="border-t border-line">{quoteColumns.map(([field, label]) => <td key={field} className="px-1 py-2">{input(`quotes.${row.id}.${field}`, `行情 ${index + 1} ${label} ${field}`, row[field], (next) => quoteChange(row, field, next), field !== 'symbol')}</td>)}
                <td className="px-1 py-2 whitespace-nowrap"><button type="button" className={actionClass} aria-label={`行情 ${index + 1} 盘口与扩展字段`} aria-expanded={expanded.has(row.id)} aria-controls={`${id}-${row.id}`} onClick={() => toggle(row.id)}>盘口 / 合约</button><button type="button" aria-label={`删除行情 ${index + 1}`} className={`${actionClass} text-ink-2`} onClick={() => { change({ ...draft, quotes: draft.quotes.filter((item) => item.id !== row.id) }); setIssues([]) }}>删除</button></td>
              </tr>
              <tr><td colSpan={4} className="p-0"><div id={`${id}-${row.id}`} inert={!expanded.has(row.id)} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded.has(row.id) ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><div className="grid grid-cols-2 gap-3 px-2 pb-3">
                {([['turnover', '成交额'], ['timestamp', '时间戳'], ['update_time', '更新时间']] as const).map(([field, label]) => <label key={field} className="flex min-w-0 flex-col gap-1 text-xs text-ink-2">{label}{input(`quotes.${row.id}.${field}`, `行情 ${index + 1} ${label} ${field}`, row[field], (next) => quoteChange(row, field, next), field !== 'update_time')}</label>)}
                {jsonInput(`quotes.${row.id}.extra`, `行情 ${index + 1} 合约扩展字段 extra JSON`, row.extra, (next) => quoteChange(row, 'extra', next))}
                {jsonInput(`quotes.${row.id}.bids`, `行情 ${index + 1} 买盘 bids JSON`, row.bids, (next) => quoteChange(row, 'bids', next))}
                {jsonInput(`quotes.${row.id}.asks`, `行情 ${index + 1} 卖盘 asks JSON`, row.asks, (next) => quoteChange(row, 'asks', next))}
              </div></div></div></td></tr>
            </Fragment>)}</tbody>
          </table>}
          {(tab === 'positions' ? draft.positions.length : draft.quotes.length) === 0 && <p className="p-4 text-sm text-ink-3">暂无{tab === 'positions' ? '持仓' : '行情'}，可从右上方添加。</p>}
        </div>
      </fieldset>
    </div>
  </CenteredModal>
}
