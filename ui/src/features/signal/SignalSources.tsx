import { useId, useState, type ReactNode } from 'react'
import { AmountInput } from '@/components/ui/AmountInput'
import { ChoiceGroup } from '@/components/ui/ChoiceGroup'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { PythonFunctionEditor } from '@/components/ui/PythonFunctionEditor'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import { Segmented } from '@/components/ui/Segmented'
import { Select } from '@/components/ui/Select'
import { Skeleton, SkeletonLines } from '@/components/ui/Skeleton'
import { Toggle } from '@/features/account/editUi'
import { useRemountFade } from '@/lib/viewTransition'
import { useChannelCatalogStore } from '@/stores/channels'
import { SignalFuturesConnection } from './SignalFuturesConnection'
import { SignalGmConnection } from './SignalGmConnection'
import { SignalQuoteTest } from './SignalQuoteTest'
import { SignalSetupColumns } from './SignalSetupColumns'
import { signalEquityPatch, signalQuoteOptions, TUSHARE_API_URL, TUSHARE_URL_CONSTRAINTS, type PositionDraft, type SignalDraft, type SignalQuoteErrors } from './signalData'

export const signalAction = 'cursor-pointer rounded-[8px] border border-line bg-surface px-3 py-2 text-[13px] text-ink-1 enabled:hover:border-border-strong disabled:cursor-default disabled:opacity-50'

export function SignalCodeEditor({ label, identity, code, onChange }: {
  label: string
  identity: string
  code: string
  onChange: (code: string) => void
}) {
  return <PythonFunctionEditor kind="portfolio" draftKey={identity} localOnly code={code} onChange={onChange}
    layout="workbench" workbenchTitle={label} height="180px" result={null} running={false} runDisabled onRun={() => {}} />
}

function QuoteSourceSkeleton({ kind, onDone }: { kind: SignalDraft['quoteKind']; onDone: () => void }) {
  return <div aria-hidden onAnimationEnd={onDone}
    className="pointer-events-none absolute inset-0 z-10 overflow-hidden bg-canvas motion-safe:animate-[panel-fade-in_180ms_ease-out_reverse_both] motion-reduce:hidden">
    {kind === 'custom' ? (
      <div className="flex h-full flex-col overflow-hidden rounded-[8px] border border-line bg-code-bg">
        <div className="flex h-8 shrink-0 items-center border-b border-line bg-surface px-3"><Skeleton className="h-3 w-20" /></div>
        <SkeletonLines rows={5} className="w-2/3 p-4" />
        <div className="mt-auto flex h-9 shrink-0 items-center border-t border-line bg-surface px-3"><Skeleton className="h-3 w-24" /></div>
      </div>
    ) : (
      <div className="grid gap-3">
        <Skeleton className="h-[67px]" />
        {(kind === 'tushare' || kind === 'tq') && <Skeleton className="h-[67px]" />}
        {kind === 'gm' && <>
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-44" />
          <Skeleton className="h-20" />
        </>}
      </div>
    )}
  </div>
}

export function SignalSources({ value, onChange, identity, savedQuoteCode, market, exampleSymbols = [], only, quoteHeading, quoteFooter, quoteErrors, onValidateQuotes }: {
  value: SignalDraft
  onChange: (draft: SignalDraft) => void
  identity: string
  savedQuoteCode: string
  market: string
  exampleSymbols?: string[]
  only?: 'account' | 'quote'
  quoteHeading?: ReactNode
  quoteFooter?: ReactNode
  quoteErrors?: SignalQuoteErrors
  onValidateQuotes?: () => boolean
}) {
  const [section, setSection] = useState<'account' | 'quote'>('account')
  const [skeletonKind, setSkeletonKind] = useState<SignalDraft['quoteKind']>('')
  const [quoteEditorStatus, setQuoteEditorStatus] = useState<HTMLDivElement | null>(null)
  const channels = useChannelCatalogStore((state) => state.channels)
  const sourceErrorId = useId()
  const quoteFade = useRemountFade(value.quoteKind)
  const quoteStep = only === 'quote'
  const fullHeightQuote = quoteStep && value.quoteKind === 'custom'
  const update = (next: Partial<SignalDraft>) => onChange({ ...value, ...next })
  const position = (id: string, next: Partial<PositionDraft>) => update({
    positions: value.positions.map((row) => row.id === id ? { ...row, ...next } : row),
  })
  const quoteFields = value.quoteKind && (
    <div key={value.quoteKind} className={`relative isolate grid min-w-0 gap-3 ${quoteStep ? `min-h-0 flex-1 ${fullHeightQuote ? 'grid-rows-[minmax(0,1fr)]' : 'content-start overflow-y-auto'}` : quoteFade ? 'panel-fade-in' : ''}`}>
      {value.quoteKind === 'gm' && <SignalGmConnection draft={value} errors={quoteErrors} onChange={update} />}
      {(value.quoteKind === 'ctp' || value.quoteKind === 'tq') && <SignalFuturesConnection
        channel={value.quoteKind} draft={value} errors={quoteErrors} onChange={update} />}
      {value.quoteKind === 'tushare' && <div className="grid min-w-0 gap-3">
        <ConnectionField label="Tushare Token" kind="secret" required error={quoteErrors?.tushareToken}
          value={value.tushareToken} onChange={(tushareToken) => update({ tushareToken })} />
        <ConnectionField label="API URL" kind="endpoint" placeholder={TUSHARE_API_URL} constraints={TUSHARE_URL_CONSTRAINTS}
          error={quoteErrors?.tushareUrl} value={value.tushareUrl} onChange={(tushareUrl) => update({ tushareUrl })} />
      </div>}
      {value.quoteKind === 'custom' && (
        <div className={`grid min-w-0 grid-rows-[minmax(0,1fr)_auto] overflow-hidden rounded-[8px] border border-line ${fullHeightQuote ? 'min-h-0' : 'h-[420px]'}`}>
          <PythonWorkbenchPane kind="signal_quotes" title="行情函数" docHref="/docs/signal-quotes" code={value.quoteCode} savedCode={savedQuoteCode}
            draftKey={`${identity}:quotes`} storageKey="axile.signalQuotes.editorSplit"
            onChange={(quoteCode) => update({ quoteCode })} editorRef={null} statusTarget={quoteEditorStatus} compactStatus={false}
            running={false} stale={false} result={null} inputError={quoteErrors?.quoteCode} />
          <div ref={setQuoteEditorStatus} />
        </div>
      )}
      {skeletonKind === value.quoteKind && <QuoteSourceSkeleton kind={value.quoteKind}
        onDone={() => setSkeletonKind((current) => current === value.quoteKind ? '' : current)} />}
    </div>
  )
  return <div className={`min-w-0 ${quoteStep ? 'flex min-h-0 flex-1 flex-col gap-3' : 'grid gap-4'}`}>
    {!only && <Segmented value={section} onChange={setSection} ariaLabel="数据来源设置"
      options={[{ value: 'account', label: '资金与持仓' }, { value: 'quote', label: '行情来源' }]} />}
    {(only ?? section) === 'account' ? <>
      <Segmented value={value.mode} onChange={(mode) => update({ mode })} ariaLabel="账户数据来源"
        options={[{ value: 'manual', label: '手动设置' }, { value: 'function', label: '自定义函数' }]} />
      {value.mode === 'function' ? <>
        <p className="text-[13px] text-ink-2">定时计算和刷新权益时调用。函数返回账户权益、可用资金、币种及可选持仓。</p>
        <SignalCodeEditor label="账户函数" identity={`${identity}:account`} code={value.accountCode} onChange={(accountCode) => update({ accountCode })} />
      </> : <>
        <div className="grid min-w-0 gap-3 sm:grid-cols-3">
          <label className="grid gap-1 text-[13px] text-ink-2">账户权益
            <AmountInput ariaLabel="账户权益" unit={value.currency} value={value.equity} onChange={(equity) => update(signalEquityPatch(value, equity))} />
          </label>
          <label className="grid gap-1 text-[13px] text-ink-2">可用资金
            <AmountInput ariaLabel="可用资金" unit={value.currency} value={value.cash} onChange={(cash) => update({ cash, cashFromEquity: false })} />
          </label>
          <ConnectionField label="计价币种" kind="text" value={value.currency} onChange={(currency) => update({ currency })} />
        </div>
        <div className="flex items-center gap-2 text-[13px] text-ink-2">
          <Toggle on={value.positionsKnown} onClick={() => update({ positionsKnown: !value.positionsKnown })} ariaLabel="提供实际持仓" />
          <span>提供实际持仓</span>
        </div>
        {value.positionsKnown && <>
          {value.positions.map((row) => <div key={row.id} className="grid min-w-0 gap-2 border-t border-line pt-2 sm:grid-cols-[2fr_1fr_1fr_1fr_auto]">
            <ConnectionField label="标的" kind="text" value={row.symbol} onChange={(symbol) => position(row.id, { symbol })} />
            <Select ariaLabel="持仓方向" value={row.direction} options={[{ value: '多头', label: '多头' }, { value: '空头', label: '空头' }]}
              onChange={(direction: PositionDraft['direction']) => position(row.id, { direction })} />
            <label className="grid gap-1 text-xs text-ink-2">持仓数量
              <AmountInput ariaLabel="持仓数量" unit="" value={row.volume} onChange={(volume) => position(row.id, { volume })} />
            </label>
            <label className="grid gap-1 text-xs text-ink-2">可用数量
              <AmountInput ariaLabel="可用数量" unit="" value={row.available} onChange={(available) => position(row.id, { available })} />
            </label>
            <button type="button" className={signalAction} onClick={() => update({ positions: value.positions.filter((item) => item.id !== row.id) })}>删除</button>
          </div>)}
          {value.positions.length === 0 && <p className="text-[13px] text-ink-3">当前空仓</p>}
          <button type="button" className={`${signalAction} justify-self-start`} onClick={() => update({
            positions: [...value.positions, { id: crypto.randomUUID(), symbol: '', direction: '多头', volume: '', available: '' }],
          })}>添加持仓</button>
        </>}
      </>}
    </> : <>
      <div className={`flex shrink-0 justify-between gap-4 ${quoteStep ? 'flex-wrap items-start' : 'items-center'}`}>
        {quoteHeading}
        <div className={quoteStep ? 'ml-auto grid min-w-0 justify-items-end' : 'w-full'}>
          <ChoiceGroup variant={quoteStep ? 'compact' : 'cards'} ariaLabel="行情来源" value={value.quoteKind}
            className={quoteStep ? 'flex-wrap justify-end' : undefined}
            invalid={Boolean(quoteErrors?.quoteKind)} describedBy={quoteErrors?.quoteKind ? sourceErrorId : undefined}
            onChange={(quoteKind) => {
              if (quoteKind === value.quoteKind) return
              if (quoteStep) setSkeletonKind(window.matchMedia('(prefers-reduced-motion: reduce)').matches ? '' : quoteKind)
              update({ quoteKind })
            }}
            options={signalQuoteOptions(market, channels ?? [])} />
          {quoteErrors?.quoteKind && <p id={sourceErrorId} role="alert" className="mt-1 text-xs text-warn">{quoteErrors.quoteKind}</p>}
        </div>
      </div>
      {quoteStep ? <SignalSetupColumns footer={quoteFooter} sidebar={value.quoteKind && (
        <SignalQuoteTest key={value.quoteKind} draft={value} examples={exampleSymbols} onValidate={onValidateQuotes} />
      )}>{quoteFields}</SignalSetupColumns> : quoteFields}
    </>}
  </div>
}
