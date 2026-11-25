import { ChevronRight } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { Card } from '@/components/ui/Card'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { ValidationCardHeader } from '@/features/account/connectionValidation/ValidationCardHeader'
import { testSignalAccount } from './signalApi'
import { isSignalAccountExample, type SignalAssetSnapshot } from './signalData'

const amountFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 20 })

export function holdingsSummary(count: number | null, prefix = ''): string {
  if (count === null) return '未提供持仓'
  return count === 0 ? '确认空仓' : `${prefix}${count} 条`
}

export function SignalAccountTestCard({ code, name, market, onMissingCode }: {
  code: string; name: string; market: string; onMissingCode: () => void
}) {
  const holdingsId = useId()
  const [run, setRun] = useState<{ source: string; example: boolean; snapshot: SignalAssetSnapshot } | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [showHoldings, setShowHoldings] = useState(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  const source = JSON.stringify([code, name, market])
  const stale = run !== null && run.source !== source
  const snapshot = run?.snapshot
  const positions = snapshot?.positions
  const status = running ? '测试中…' : error ? '测试失败'
    : run ? `${stale ? '上次 · ' : ''}${run.example ? '示例快照' : '测试成功'}` : '尚未测试'

  const test = async () => {
    if (request.current) return
    if (!code.trim()) { onMissingCode(); return }
    const controller = new AbortController()
    request.current = controller
    setRunning(true)
    setError(null)
    setRun(null)
    setShowHoldings(false)
    try {
      const snapshot = await testSignalAccount(code, name, market, controller.signal)
      if (!controller.signal.aborted) setRun({ source, example: isSignalAccountExample(code), snapshot })
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      request.current = null
      if (!controller.signal.aborted) setRunning(false)
    }
  }

  return <aside aria-label="账户快照">
    <Card className="p-3.5">
      <ValidationCardHeader title="账户快照" status={status} statusBadge action="测试函数"
        disabled={running} loading={running} onAction={() => void test()} />
      {snapshot && <>
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 border-t border-line pt-3 text-[13px]">
          <dt className="text-ink-2">账户权益</dt><dd className="text-right tabular-nums">{amountFormat.format(snapshot.total_asset)} {snapshot.currency}</dd>
          <dt className="text-ink-2">可用资金</dt><dd className="text-right tabular-nums">{amountFormat.format(snapshot.available_cash)} {snapshot.currency}</dd>
          {!positions?.length && <>
            <dt className="text-ink-2">实际持仓</dt><dd className="text-right">{holdingsSummary(positions?.length ?? null)}</dd>
          </>}
        </dl>
        {positions && positions.length > 0 && <>
          <button type="button" aria-expanded={showHoldings} aria-controls={holdingsId}
            onClick={() => setShowHoldings((open) => !open)}
            className="group flex w-full cursor-pointer items-center gap-2 py-2 text-left text-[13px] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent">
            <span className="flex-1 text-ink-2 group-hover:text-ink-1">实际持仓</span>
            <span>{holdingsSummary(positions.length)}</span>
            <ChevronRight size={14} aria-hidden className="shrink-0 text-ink-2 transition-transform duration-200 group-aria-expanded:rotate-90 motion-reduce:transition-none" />
          </button>
          <div id={holdingsId} inert={!showHoldings} aria-hidden={!showHoldings}
            className={`grid transition-[grid-template-rows] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${showHoldings ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
            <div className="min-h-0 overflow-hidden">
              <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="持仓明细">
                <table className="w-full text-left text-[13px] whitespace-nowrap">
                  <thead className="border-y border-line text-ink-2"><tr>
                    <th scope="col" className="py-2 pr-2">标的</th><th scope="col" className="pr-2">方向</th>
                    <th scope="col" className="pr-2 text-right">持仓数量</th><th scope="col" className="text-right">可用数量</th>
                  </tr></thead>
                  <tbody className="divide-y divide-line">
                    {positions.map((row, index) => <tr key={`${row.symbol}-${row.direction}-${index}`}>
                      <td className="py-2 pr-2 font-mono">{row.symbol}</td><td className="pr-2">{row.direction}</td>
                      <td className="pr-2 text-right tabular-nums">{row.volume}</td><td className="text-right tabular-nums">{row.available_volume}</td>
                    </tr>)}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </>}
      </>}
      <ErrorNotice title="账户函数测试失败" intent="mutation" variant="compact" error={error} />
    </Card>
  </aside>
}
