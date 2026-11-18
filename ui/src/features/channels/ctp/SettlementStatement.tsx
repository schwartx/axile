import { useEffect, useRef, useState } from 'react'

import { CenteredModal } from '@/components/ui/CenteredModal'
import { getSettlementStatement, type SettlementDocument } from '@/features/channels/ctp/accountToolsApi'

export function SettlementStatement({ accountId }: { accountId: number }) {
  const [open, setOpen] = useState(false)
  const [day, setDay] = useState('')
  const [documents, setDocuments] = useState<SettlementDocument[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  async function query() {
    if (request.current) return
    const controller = new AbortController()
    request.current = controller
    setDocuments(null)
    setError('')
    setBusy(true)
    try {
      const result = await getSettlementStatement(accountId, day, controller.signal)
      if (!controller.signal.aborted) setDocuments(result.documents)
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '结算单查询失败')
    } finally {
      request.current = null
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  return <>
    <button type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}
      className="inline-flex h-6 shrink-0 cursor-pointer items-center rounded-md px-1 text-xs text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent">
      结算单
    </button>
    {open && <CenteredModal open title="结算单" widthClassName="w-[880px]" onClose={() => setOpen(false)}>
      <form onSubmit={event => { event.preventDefault(); void query() }} className="flex flex-wrap items-center gap-2 text-sm">
        <label className="inline-flex items-center gap-2 text-ink-2">交易日
          <input type="date" value={day} disabled={busy}
            className="rounded-md border border-border-strong bg-surface px-2 py-1 text-ink-1 focus:border-accent"
            onChange={event => { setDay(event.target.value); setDocuments(null); setError('') }} />
        </label>
        <button type="submit" disabled={busy}
          className="cursor-pointer rounded-md border border-line px-2.5 py-1 hover:bg-fill disabled:cursor-not-allowed disabled:opacity-45">
          {busy ? '查询中…' : '查询结算单'}
        </button>
        <span className="text-xs text-ink-2">留空查询最近一份</span>
      </form>
      {error && <p role="alert" className="mt-3 break-words text-sm text-warn">{error}</p>}
      {documents?.length === 0 && <p role="status" className="mt-3 text-sm text-ink-2">柜台未返回结算单。</p>}
      {documents?.map((document, index) => (
        <section key={`${document.trading_day}-${document.settlement_id}-${document.currency}-${index}`} className="mt-4">
          <h3 className="text-sm font-medium">{document.trading_day.replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3')}{document.currency && ` · ${document.currency === 'CNY' ? '人民币' : document.currency}`}</h3>
          <pre role="region" tabIndex={0} aria-label="结算单原文" className="mt-2 overflow-x-auto rounded-md border border-line bg-bg p-3 text-xs leading-relaxed">
            {document.content}
          </pre>
        </section>
      ))}
    </CenteredModal>}
  </>
}
