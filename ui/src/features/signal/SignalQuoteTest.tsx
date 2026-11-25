import { ChevronRight, TriangleAlert } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { Card } from '@/components/ui/Card'
import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { appendUniqueSymbols } from '@/components/ui/symbolTags'
import { SymbolTagInput } from '@/components/ui/SymbolTagInput'
import { ValidationCardHeader } from '@/features/account/connectionValidation/ValidationCardHeader'
import { errorInfo, shortErrorReason, type ErrorInfo } from '@/lib/errorInfo'
import { withCurrency } from '@/lib/format'
import { testSignalQuote, type SignalQuoteTestResult } from './signalApi'
import { isSignalQuoteExample, signalQuoteConfig, type SignalDraft } from './signalData'

const MAX_SYMBOLS = 10
const MAX_DEPTH_LEVELS = 5
const quoteTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

function QuoteDepthSide({ levels, side, maxQuantity }: {
  levels: [number, number][]; side: '卖盘' | '买盘'; maxQuantity: number
}) {
  return <tbody aria-label={side}>
    {levels.length === 0 ? <tr>
      <td colSpan={2} className="px-1.5 py-1 text-ink-2">未提供{side}</td>
    </tr> : levels.map(([price, quantity], index) => <tr key={index}>
      <td className="px-1.5 py-0.5">{price}</td>
      <td className="relative px-1.5 py-0.5 text-right">
        <span aria-hidden className={`pointer-events-none absolute inset-y-px right-0 opacity-[.18] ${side === '买盘' ? 'bg-up' : 'bg-down'}`}
          style={{ width: `${quantity / maxQuantity * 100}%` }} />
        <span className="relative">{quantity}</span>
      </td>
    </tr>)}
  </tbody>
}

export function QuoteDetails({ quote, error, currency }: SignalQuoteTestResult['items'][number] & { currency: string }) {
  if (!quote) return <p className="wrap-anywhere px-1 pt-1 pb-2 text-xs text-warn">{error}</p>

  const bids = quote.bids.slice(0, MAX_DEPTH_LEVELS)
  const asks = quote.asks.slice(0, MAX_DEPTH_LEVELS).reverse()
  const hasDepth = bids.length > 0 || asks.length > 0
  const maxQuantity = Math.max(0, ...bids.map(([, quantity]) => quantity), ...asks.map(([, quantity]) => quantity)) || 1
  const timestamp = new Date(quote.timestamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
  return <div className="px-1 pt-1 pb-2 text-xs">
    {hasDepth ? (
        <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="盘口档位">
          <table className="w-full border-collapse whitespace-nowrap tabular-nums">
            <caption className="sr-only">盘口档位</caption>
            <thead className="border-b border-line text-ink-2">
              <tr>
                <th scope="col" className="w-1/3 px-1.5 py-1 text-left font-medium">
                  价格 <span className="font-normal">· 上卖下买</span>
                </th>
                <th scope="col" className="px-1.5 py-1 text-right font-medium">挂量</th>
              </tr>
            </thead>
            <QuoteDepthSide levels={asks} side="卖盘" maxQuantity={maxQuantity} />
            <tbody className="border-y border-line">
              <tr><td colSpan={2} className="px-1.5 py-1">
                <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                  <div>
                    <strong className="text-sm">{withCurrency(String(quote.last_price), currency)}</strong>
                    <span className="ml-2 text-ink-2">最新成交</span>
                  </div>
                  <span className="text-ink-2">北京时间 {timestamp}</span>
                </div>
              </td></tr>
            </tbody>
            <QuoteDepthSide levels={bids} side="买盘" maxQuantity={maxQuantity} />
          </table>
        </div>
      ) : <div className="space-y-1 text-ink-2">
        <p className="tabular-nums">北京时间 {timestamp}</p>
        <p>此次报价未包含盘口档位。</p>
      </div>}
  </div>
}

export function SignalQuoteTest({ draft, onValidate }: { draft: SignalDraft; onValidate?: () => boolean }) {
  const symbolInputId = useId()
  const custom = draft.quoteKind === 'custom'
  const example = custom && isSignalQuoteExample(draft.quoteCode)
  const currency = custom ? '' : 'CNY'
  const [symbols, setSymbols] = useState(['600000.SH', '000001.SZ', '510300.SH'])
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<SignalQuoteTestResult | null>(null)
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [details, setDetails] = useState<ErrorInfo | null>(null)
  const [tested, setTested] = useState<{ version: string; example: boolean } | null>(null)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  // 连接草稿只在内存中比较，避免把令牌写入 DOM 或持久化。
  const target = draft.gmMode === 'service' ? draft.gmAddress.trim() : draft.gmTerminalPath?.trim()
  const config = custom ? draft.quoteCode : draft.quoteKind === 'tushare'
    ? [draft.tushareToken.trim(), draft.tushareUrl.trim()] : [draft.gmToken.trim(), draft.gmMode, target]
  const version = JSON.stringify([config, symbols])
  const stale = tested !== null && tested.version !== version
  const rows = result?.items ?? []
  const returned = rows.filter((row) => row.quote !== null).length
  const tooMany = symbols.length > MAX_SYMBOLS

  const run = async () => {
    if (request.current || !symbols.length || tooMany || (!example && onValidate?.() === false)) return
    const controller = new AbortController()
    request.current = controller
    setResult(null)
    setSelectedSymbol(null)
    setError(null)
    setTested({ version, example })
    setRunning(true)
    try {
      const source = example ? { kind: 'custom' as const, code: draft.quoteCode } : signalQuoteConfig(draft)
      const result = await testSignalQuote(source, symbols, controller.signal)
      if (!controller.signal.aborted) setResult(result)
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      request.current = null
      if (!controller.signal.aborted) setRunning(false)
    }
  }

  const resultContext = `${stale ? '上次 · ' : ''}${tested?.example ? '示例 · ' : ''}`
  const status = running
    ? '正在读取…'
    : rows.length ? `${resultContext}${returned} / ${rows.length} 有报价` : error ? '测试失败' : '尚未测试'
  const reason = error ? shortErrorReason(error) : ''

  return (
    <aside aria-label="行情测试" className="min-w-0">
      <Card className="p-3.5">
        <form className="grid gap-1.5" onSubmit={(event) => { event.preventDefault(); void run() }}>
          <div className="border-b border-line pb-[7px]">
            <ValidationCardHeader title="行情测试" status={status} statusBadge action={running ? '测试中…' : '测试行情'}
              disabled={running || !symbols.length || tooMany} loading={running} />
          </div>
          <div className="grid gap-1">
            <div className="flex items-baseline justify-between gap-2">
              <label htmlFor={symbolInputId} className="shrink-0 text-[13px] text-ink-2">测试标的</label>
              <span id={`${symbolInputId}-help`} className={`text-right text-xs ${tooMany ? 'text-warn' : 'text-ink-3'}`}>
                最多 {MAX_SYMBOLS} 个 · 空格、逗号或换行分隔
              </span>
            </div>
            <SymbolTagInput id={symbolInputId} value={symbols} compact invalid={tooMany}
              describedBy={`${symbolInputId}-help`} placeholder="输入或粘贴标的"
              onChange={(next) => setSymbols(custom ? next : appendUniqueSymbols([], next.map((symbol) => symbol.toUpperCase())))} />
          </div>
        </form>
        {rows.length > 0 && <div aria-label="测试结果 · 行情时间为北京时间" className="mt-2.5 divide-y divide-line border-t border-line">
          {rows.map((row, index) => {
            const { symbol, quote } = row
            const expanded = symbol === selectedSymbol
            const detailsId = `${symbolInputId}-quote-${index}`
            return <div key={symbol}>
              <button type="button" onClick={() => setSelectedSymbol((current) => current === symbol ? null : symbol)}
                aria-label={`${symbol} 行情详情`} aria-expanded={expanded} aria-controls={detailsId}
                className="group grid w-full cursor-pointer grid-cols-[minmax(0,1fr)_auto_auto_12px] items-center gap-x-2 px-1 py-2 text-left text-xs hover:bg-fill focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent">
                <span className="truncate font-mono">{symbol}</span>
                {quote ? <>
                  <span className="font-mono">{withCurrency(String(quote.last_price), currency)}</span>
                  <span className="font-mono text-ink-2">{quoteTime.format(quote.timestamp)}</span>
                </> : <span className="col-span-2 justify-self-end text-warn">未取到</span>}
                <ChevronRight size={12} aria-hidden className="transition-transform duration-200 group-aria-expanded:rotate-90 motion-reduce:transition-none" />
              </button>
              <div id={detailsId} inert={!expanded} aria-hidden={!expanded}
                className={`grid transition-[grid-template-rows] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
                <div className="min-h-0 overflow-hidden"><QuoteDetails {...row} currency={currency} /></div>
              </div>
            </div>
          })}
        </div>}
        {error && <div className="mt-2.5 flex items-center gap-1.5 border-t border-line pt-2.5 text-xs">
          <TriangleAlert className="shrink-0 text-warn" size={14} aria-hidden />
          <p role="alert" title={reason} className="min-w-0 flex-1 truncate text-warn">{stale ? '上次：' : ''}{reason}</p>
          <button type="button" onClick={() => setDetails(errorInfo(error))}
            className="shrink-0 cursor-pointer rounded-sm px-1 py-0.5 text-ink-2 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent">
            详情
          </button>
        </div>}
      </Card>
      <ErrorDetailsModal selection={details ? { title: '行情测试错误详情', info: details } : null} onClose={() => setDetails(null)} />
    </aside>
  )
}
