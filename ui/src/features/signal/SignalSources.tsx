import { useId, useState, type ReactNode } from 'react'
import { ChoiceGroup } from '@/components/ui/ChoiceGroup'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import { Skeleton, SkeletonLines } from '@/components/ui/Skeleton'
import { useChannelCatalogStore } from '@/stores/channels'
import { SignalFuturesConnection } from './SignalFuturesConnection'
import { SignalGmConnection } from './SignalGmConnection'
import { SignalQuoteTest } from './SignalQuoteTest'
import { SignalSetupColumns } from './SignalSetupColumns'
import { signalQuoteOptions, TUSHARE_API_URL, TUSHARE_URL_CONSTRAINTS, type SignalDraft, type SignalQuoteErrors } from './signalData'

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

export function SignalSources({ value, onChange, identity, savedQuoteCode, market, exampleSymbols = [], quoteHeading, quoteFooter, quoteErrors, onValidateQuotes }: {
  value: SignalDraft
  onChange: (draft: SignalDraft) => void
  identity: string
  savedQuoteCode: string
  market: string
  exampleSymbols?: string[]
  quoteHeading?: ReactNode
  quoteFooter?: ReactNode
  quoteErrors?: SignalQuoteErrors
  onValidateQuotes?: () => boolean
}) {
  const [skeletonKind, setSkeletonKind] = useState<SignalDraft['quoteKind']>('')
  const [quoteEditorStatus, setQuoteEditorStatus] = useState<HTMLDivElement | null>(null)
  const channels = useChannelCatalogStore((state) => state.channels)
  const sourceErrorId = useId()
  const custom = value.quoteKind === 'custom'
  const update = (next: Partial<SignalDraft>) => onChange({ ...value, ...next })
  const quoteFields = value.quoteKind && (
    <div key={value.quoteKind} className={`relative isolate grid min-w-0 gap-3 min-h-0 flex-1 ${custom ? 'grid-rows-[minmax(0,1fr)] max-lg:min-h-72' : 'content-start overflow-y-auto'}`}>
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
        <div className="grid min-h-0 min-w-0 grid-rows-[minmax(0,1fr)_auto] overflow-hidden rounded-[8px] border border-line">
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
  return <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
    <div className="flex shrink-0 flex-wrap items-start justify-between gap-4">
      {quoteHeading}
      <div className="ml-auto grid min-w-0 justify-items-end">
        <ChoiceGroup variant="compact" ariaLabel="行情来源" value={value.quoteKind}
          className="flex-wrap justify-end" invalid={Boolean(quoteErrors?.quoteKind)}
          describedBy={quoteErrors?.quoteKind ? sourceErrorId : undefined}
          onChange={(quoteKind) => {
            if (quoteKind === value.quoteKind) return
            setSkeletonKind(window.matchMedia('(prefers-reduced-motion: reduce)').matches ? '' : quoteKind)
            update({ quoteKind })
          }}
          options={signalQuoteOptions(market, channels ?? [])} />
        {quoteErrors?.quoteKind && <p id={sourceErrorId} role="alert" className="mt-1 text-xs text-warn">{quoteErrors.quoteKind}</p>}
      </div>
    </div>
    <SignalSetupColumns footer={quoteFooter} sidebar={value.quoteKind && (
      <SignalQuoteTest key={value.quoteKind} draft={value} examples={exampleSymbols} onValidate={onValidateQuotes} />
    )}>{quoteFields}</SignalSetupColumns>
  </div>
}
