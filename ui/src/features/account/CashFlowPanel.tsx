import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { Select } from '@/components/ui/Select'
import { Segmented } from '@/components/ui/Segmented'
import { AmountInput } from '@/components/ui/AmountInput'
import { OverflowText } from '@/components/ui/OverflowText'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { getCashFlows, resolveCashFlow, saveCashFlow, syncCashFlows, type CashFlow } from '@/lib/api/cashFlows'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'
import { amount, shanghaiLabel } from '@/features/history/costs'
import { CashFlowDateTime } from '@/features/account/CashFlowDateTime'
import { cashFlowTimeError, cashFlowTimestamp } from '@/features/account/cashFlowTime'
import { useRemountFade } from '@/lib/viewTransition'

const button = 'rounded-md border border-line px-3 py-1.5 text-sm text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-45'
const input = 'w-full rounded-md border border-line bg-surface px-3 py-2 text-sm text-ink-1 focus-visible:outline-2 focus-visible:outline-accent'

/** 账户权益旁的出入金入口与流水弹窗；资金搬家保持中性色。 */
export function CashFlowPanel({ accountId }: { accountId: number }) {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [offset, setOffset] = useState(0)
  const fetcher = useCallback((signal: AbortSignal) => getCashFlows(accountId, offset, signal), [accountId, offset])
  const page = usePolling(fetcher, { queryKey: `cash-flows:${accountId}:${offset}`, intervalMs: 5000, enabled: dialogOpen, retainDataWhenDisabled: true })
  const [open, setOpen] = useState(false)
  const [now, setNow] = useState(Date.now)
  const formId = useId()
  const recordButton = useRef<HTMLButtonElement>(null)
  const fade = useRemountFade(open)
  const [editing, setEditing] = useState<CashFlow | null>(null)
  const [direction, setDirection] = useState<'deposit' | 'withdraw'>('deposit')
  const [value, setValue] = useState('')
  const [date, setDate] = useState('')
  const [time, setTime] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const lock = useRef(false)
  const [error, setError] = useState<Error | null>(null)
  const [feedback, setFeedback] = useState('')
  const [voidId, setVoidId] = useState<number | null>(null)
  const refreshAccounts = useDomainStore(s => s.refreshAccounts)
  useEffect(() => {
    if (dialogOpen && !open && !busy) recordButton.current?.focus()
  }, [dialogOpen, open, busy])
  useEffect(() => {
    if (!dialogOpen || !open) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [dialogOpen, open])
  const begin = (flow: CashFlow | null) => {
    setEditing(flow); setDirection(flow?.direction ?? 'deposit'); setValue(flow ? String(flow.amount) : '')
    const localTime = flow ? shanghaiLabel(flow.occurred_at) : ''
    setDate(localTime.slice(0, 10)); setTime(localTime.slice(11, 19))
    setNote(flow?.note ?? ''); setError(null); setFeedback('')
    setNow(Date.now())
    setOpen(true)
  }
  const mutate = async (operation: () => Promise<unknown>, message = '流水已保存，收益正在重新计算') => {
    if (lock.current) return
    lock.current = true; setBusy(true); setError(null); setFeedback('')
    try {
      await operation()
      setFeedback(message); setVoidId(null)
      await page.refresh()
      void refreshAccounts()
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally { lock.current = false; setBusy(false) }
  }
  const rows = page.data?.data ?? []
  const currencyUnit = page.data?.currency === 'CNY' ? '元' : page.data?.currency ?? '账户币种'
  const timeError = cashFlowTimeError(date, time, now)
  return <>
    <button type="button" aria-haspopup="dialog" aria-expanded={dialogOpen}
      className="inline-flex h-6 shrink-0 cursor-pointer items-center rounded-md px-1 text-xs text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"
      onClick={() => { setOpen(false); setError(null); setDialogOpen(true) }}>出入金</button>
    <CenteredModal open={dialogOpen} title={open ? editing ? '修改流水' : '录入流水' : '出入金'} onClose={() => setDialogOpen(false)} closeDisabled={busy}
      widthClassName={open ? 'w-[560px]' : 'w-[880px]'}
      footer={open && <div className="flex justify-end gap-2 border-t border-line px-[22px] py-3">
        <button type="button" className={button} disabled={busy} onClick={() => { setOpen(false); setError(null) }}>取消</button>
        <button type="submit" form={formId} disabled={busy || !!timeError} className="cursor-pointer rounded-md border border-ink-1 bg-ink-1 px-3 py-1.5 text-sm font-medium text-surface focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-45">{busy ? '保存中…' : editing ? '保存修改' : '保存流水'}</button>
      </div>}>
    <section key={open ? 'form' : 'list'} aria-label="账户出入金" className={`pb-3 ${fade ? 'panel-fade-in' : ''}`}>
    {open ? <form id={formId} aria-label={editing ? '修改出入金流水' : '录入出入金流水'} onSubmit={event => {
      event.preventDefault()
      const submittedAt = Date.now()
      setNow(submittedAt)
      if (cashFlowTimeError(date, time, submittedAt)) return
      const occurredAt = cashFlowTimestamp(date, time)
      if (!occurredAt) { setError(new Error('请填写有效的发生日期和时、分、秒')); return }
      void mutate(async () => {
        await saveCashFlow(accountId, { direction, amount: Number(value), occurred_at: occurredAt, note }, editing)
        setOpen(false)
      })
    }}>
      <fieldset disabled={busy} className="grid min-w-0 gap-y-4 sm:grid-cols-[max-content_minmax(0,1fr)] sm:items-center sm:gap-x-5">
        <legend className="sr-only">{editing ? '修改流水' : '录入流水'}</legend>
        <div role="group" aria-label="出入金方向" className="grid gap-y-1.5 sm:col-span-2 sm:grid-cols-subgrid sm:items-center sm:gap-x-5">
          <span className="text-xs text-ink-2">方向</span>
          <Segmented className="justify-self-start" value={direction} onChange={setDirection} ariaLabel="出入金方向" size="sm" options={[{ value: 'deposit', label: '入金' }, { value: 'withdraw', label: '出金' }]} />
        </div>
        <label className="grid gap-y-1.5 text-xs text-ink-2 sm:col-span-2 sm:grid-cols-subgrid sm:items-start sm:gap-x-5">
          <span className="sm:pt-2.5">金额<span className="sr-only">（{currencyUnit}）</span></span>
          <AmountInput value={value} onChange={setValue} unit={currencyUnit} ariaLabel={`金额（${currencyUnit}）`} autoFocus required />
        </label>
        <CashFlowDateTime date={date} time={time} onDateChange={setDate} onTimeChange={setTime} now={now} />
        <label className="grid gap-y-1.5 text-xs text-ink-2 sm:col-span-2 sm:grid-cols-subgrid sm:items-center sm:gap-x-5">
          <span>备注<span className="ml-1 text-ink-3">（可选）</span></span>
          <input className={input} maxLength={500} value={note} onChange={e => setNote(e.target.value)} />
        </label>
      </fieldset>
    </form> : <>
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <h3 className="text-sm font-semibold text-ink-2">流水记录</h3>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={button} disabled={busy || page.loading} onClick={() => { void page.refresh() }}>刷新记录</button>
        {page.data?.sync_supported && <button type="button" className={button} disabled={busy} onClick={() => { void mutate(() => syncCashFlows(accountId), '渠道流水已同步，收益将按最新流水重算') }}>同步渠道流水</button>}
        <button ref={recordButton} type="button" className={button} disabled={busy} onClick={() => begin(null)}>录入流水</button>
      </div>
    </div>
      <ErrorNotice accountId={accountId} intent="read" title="流水读取失败" error={page.error} hasData={page.data != null} onRetry={page.refresh} variant="compact" />
      <p role="status" className={feedback ? 'mt-2 text-xs text-ink-2' : undefined}>{feedback}</p>
      {page.loading ? <p className="py-3 text-sm text-ink-3">正在读取流水…</p> : !page.error && rows.length === 0 ? <p className="py-3 text-sm text-ink-3">暂无出入金记录。</p> : null}
      {rows.length > 0 && <div className="overflow-x-auto"><table className="w-full text-left text-xs tabular-nums">
        <caption className="sr-only">出入金流水，待核对记录优先显示</caption>
        <thead className="text-ink-2"><tr className="border-b border-line">{['发生时间（北京时间）', '方向', '金额', '来源与状态', '备注', '操作'].map(label => <th key={label} className="whitespace-nowrap py-2 pr-3 font-normal">{label}</th>)}</tr></thead>
        <tbody>{rows.map(flow => <CashFlowRow key={flow.id} flow={flow} busy={busy} voiding={voidId === flow.id} onEdit={() => begin(flow)} onVoid={() => setVoidId(flow.id)} onCancelVoid={() => setVoidId(null)} onResolve={(action, manual) => {
          void mutate(() => resolveCashFlow(accountId, flow, action, manual))
        }} />)}</tbody>
      </table></div>}
      {(page.data?.total ?? 0) > 20 && <div className="mt-3 flex items-center justify-end gap-3 text-xs text-ink-3"><span>第 {offset + 1}–{Math.min(offset + 20, page.data!.total)} 条，共 {page.data!.total} 条</span><button type="button" className={button} disabled={busy || offset === 0} onClick={() => setOffset(offset - 20)}>上一页</button><button type="button" className={button} disabled={busy || offset + 20 >= page.data!.total} onClick={() => setOffset(offset + 20)}>下一页</button></div>}
    </>}
    <ErrorNotice accountId={accountId} intent="mutation" title="流水操作未完成" error={error} variant="compact" />
    </section>
    </CenteredModal>
  </>
}

function CashFlowRow({ flow, busy, voiding, onEdit, onVoid, onCancelVoid, onResolve }: {
  flow: CashFlow; busy: boolean; voiding: boolean; onEdit: () => void; onVoid: () => void; onCancelVoid: () => void
  onResolve: (action: 'void' | 'confirm' | 'link', manual?: { id: number; revision: number }) => void
}) {
  const [manualId, setManualId] = useState<number | null>(null)
  const candidates = flow.manual_candidates ?? []
  return <tr className="border-b border-line last:border-0">
    <td className="whitespace-nowrap py-3 pr-3 text-ink-2">{shanghaiLabel(flow.occurred_at)}</td>
    <td className="whitespace-nowrap pr-3">{flow.direction === 'deposit' ? '入金' : '出金'}</td>
    <td className="whitespace-nowrap pr-3">{amount(flow.amount)}</td>
    <td className="whitespace-nowrap pr-3 text-ink-3">{flow.source === 'manual' ? '手工' : '渠道'} · <span className={flow.status === 'pending' ? 'text-warn' : undefined}>{flow.status === 'pending' ? '待核对，未计入' : flow.status === 'void' ? '已撤销' : '已计入'}</span></td>
    <td className="max-w-48 pr-3"><OverflowText text={flow.note || '—'} /></td>
    <td className="py-2"><div className="flex flex-wrap items-center gap-2">
      {flow.status === 'pending' && <>
        {candidates.length > 0 && <><Select value={manualId} onChange={setManualId} disabled={busy} ariaLabel="关联的手工流水" options={[{ value: null, label: '选择对应手工流水' }, ...candidates.map(c => ({ value: c.id, label: `#${c.id} · ${shanghaiLabel(c.occurred_at)}` }))]} /><button type="button" className={button} disabled={busy || !candidates.some(c => c.id === manualId)} onClick={() => onResolve('link', candidates.find(c => c.id === manualId))}>关联</button></>}
        <button type="button" className={button} disabled={busy} onClick={() => onResolve('confirm')}>确认为另一笔</button>
      </>}
      {flow.status === 'active' && flow.source === 'manual' && <button type="button" className={button} disabled={busy} onClick={onEdit}>修改</button>}
      {flow.status !== 'void' && (voiding ? <><span className="text-ink-2">撤销后重算收益</span><button type="button" className={button} disabled={busy} onClick={() => onResolve('void')}>确认撤销</button><button type="button" className={button} disabled={busy} onClick={onCancelVoid}>取消</button></> : <button type="button" className={button} disabled={busy} onClick={onVoid}>撤销</button>)}
    </div></td>
  </tr>
}
