import { useEffect, useId, useRef, useState } from 'react'

import { DatePicker } from '@/components/ui/DatePicker'
import { parseDay } from '@/components/ui/datePickerModel'
import { getSettlementStatement, type SettlementDocument } from '@/features/channels/ctp/accountToolsApi'
import type { AccountToolProps } from '@/features/tools/ToolsButton'

export function SettlementStatement({ accountId }: AccountToolProps) {
  const dateHintId = useId()
  const [day, setDay] = useState('')
  const [documents, setDocuments] = useState<SettlementDocument[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  async function query() {
    if (request.current) return
    if (day && parseDay(day) == null) {
      setError('请输入有效的交易日，格式为 YYYY-MM-DD。')
      return
    }
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
    <form onSubmit={event => { event.preventDefault(); void query() }} className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-ink-2">交易日</span>
      <DatePicker ariaLabel="交易日" describedBy={dateHintId} value={day} disabled={busy} className="flex-[1_1_8.5rem]"
        onChange={value => { setDay(value); setDocuments(null); setError('') }} />
      <button type="submit" disabled={busy}
        className="cursor-pointer rounded-md border border-line px-2.5 py-1 hover:bg-fill disabled:cursor-not-allowed disabled:opacity-45">
        {busy ? '查询中…' : '查询结算单'}
      </button>
      <span id={dateHintId} className="w-full text-xs text-ink-2">留空查询最近一份</span>
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
  </>
}
