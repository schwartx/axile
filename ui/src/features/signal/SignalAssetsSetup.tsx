import { useState, type ReactNode } from 'react'
import { Card } from '@/components/ui/Card'
import { ChoiceGroup } from '@/components/ui/ChoiceGroup'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import { holdingsSummary, SignalAccountTestCard } from './SignalAccountTestCard'
import { SignalManualAssets } from './SignalManualAssets'
import { SignalSetupColumns } from './SignalSetupColumns'
import { initialSignalDraft, type SignalAccountErrors, type SignalDraft } from './signalData'

export function SignalAssetsSetup({ value, errors, showErrors, name, market, heading, footer, onChange, onMissingCode,
  identity = 'signal:setup:account', savedCode = initialSignalDraft.accountCode }: {
  value: SignalDraft
  errors: SignalAccountErrors
  showErrors: boolean
  identity?: string
  savedCode?: string
  name: string
  market: string
  heading: ReactNode
  footer: ReactNode
  onChange: (patch: Partial<SignalDraft>) => void
  onMissingCode: () => void
}) {
  const [status, setStatus] = useState<HTMLDivElement | null>(null)
  const manual = value.mode === 'manual'
  const incompleteFunds = Boolean(errors.equity || errors.cash || errors.currency)
  const incompletePositions = Object.keys(errors.positions ?? {}).length
  const completePositions = value.positions.length - incompletePositions
  const positionSummary = incompletePositions > 0
    ? [completePositions > 0 ? `已录入 ${completePositions} 条` : '', `${incompletePositions} 条待完善`].filter(Boolean).join(' · ')
    : holdingsSummary(value.positionsKnown ? value.positions.length : null, '已录入 ')
  const sidebar = manual ? <aside aria-label="录入检查">
    <Card className="p-3.5">
      <h2 className="text-[15px] font-semibold text-ink-1">录入检查</h2>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 border-t border-line pt-3 text-[13px]">
        <dt className="text-ink-2">账户资金</dt>
        <dd className="text-right">{incompleteFunds ? '待补全' : '资金字段已填写'}</dd>
        <dt className="text-ink-2">实际持仓</dt>
        <dd className="text-right">{positionSummary}</dd>
      </dl>
    </Card>
  </aside> : <SignalAccountTestCard code={value.accountCode} name={name} market={market} onMissingCode={onMissingCode} />

  return <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
    <div className="flex shrink-0 flex-wrap items-start justify-between gap-4">
      {heading}
      <ChoiceGroup variant="compact" ariaLabel="账户数据来源" value={value.mode} onChange={(mode) => onChange({ mode })}
        options={[{ value: 'manual', label: '手动设置' }, { value: 'function', label: '自定义函数' }]} />
    </div>
    <SignalSetupColumns sidebar={sidebar} footer={footer}>
      {manual ? <div className="min-h-0 flex-1 overflow-y-auto">
        <SignalManualAssets value={value} errors={showErrors ? errors : undefined} onChange={onChange} />
      </div> : <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_auto] overflow-hidden rounded-[8px] border border-line max-lg:min-h-72">
        <PythonWorkbenchPane kind="signal_account" title="账户函数" code={value.accountCode} savedCode={savedCode}
          draftKey={identity} storageKey="axile.signalAccount.editorSplit"
          onChange={(accountCode) => onChange({ accountCode })} editorRef={null} statusTarget={status} compactStatus={false}
          running={false} stale={false} result={null} inputError={showErrors ? errors.accountCode : undefined} />
        <div ref={setStatus} />
      </div>}
    </SignalSetupColumns>
  </div>
}
