import { useState } from 'react'
import { AmountInput } from '@/components/ui/AmountInput'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { PythonFunctionEditor } from '@/components/ui/PythonFunctionEditor'
import { Segmented } from '@/components/ui/Segmented'
import { Select } from '@/components/ui/Select'
import { Toggle } from '@/features/account/editUi'
import type { PositionDraft, SignalDraft } from './signalData'

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

export function SignalSources({ value, onChange, identity }: {
  value: SignalDraft
  onChange: (draft: SignalDraft) => void
  identity: string
}) {
  const [section, setSection] = useState<'account' | 'quote'>('account')
  const update = (next: Partial<SignalDraft>) => onChange({ ...value, ...next })
  const position = (id: string, next: Partial<PositionDraft>) => update({
    positions: value.positions.map((row) => row.id === id ? { ...row, ...next } : row),
  })
  return <div className="grid min-w-0 gap-4">
    <Segmented value={section} onChange={setSection} ariaLabel="数据来源设置"
      options={[{ value: 'account', label: '资金与持仓' }, { value: 'quote', label: '行情函数' }]} />
    {section === 'account' ? <>
      <Segmented value={value.mode} onChange={(mode) => update({ mode })} ariaLabel="账户数据来源"
        options={[{ value: 'manual', label: '手动设置' }, { value: 'function', label: '自定义函数' }]} />
      {value.mode === 'function' ? <>
        <p className="text-[13px] text-ink-2">定时计算和刷新权益时调用。函数返回账户权益、可用资金、币种及可选持仓。</p>
        <SignalCodeEditor label="账户函数" identity={`${identity}:account`} code={value.accountCode} onChange={(accountCode) => update({ accountCode })} />
      </> : <>
        <div className="grid min-w-0 gap-3 sm:grid-cols-3">
          <label className="grid gap-1 text-[13px] text-ink-2">账户权益
            <AmountInput ariaLabel="账户权益" unit={value.currency} value={value.equity} onChange={(equity) => update({ equity })} />
          </label>
          <label className="grid gap-1 text-[13px] text-ink-2">可用资金
            <AmountInput ariaLabel="可用资金" unit={value.currency} value={value.cash} onChange={(cash) => update({ cash })} />
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
      <p className="text-[13px] text-ink-2">选择自己的行情源。函数按标的返回参考价格和行情时间。</p>
      <SignalCodeEditor label="行情函数" identity={`${identity}:quotes`} code={value.quoteCode} onChange={(quoteCode) => update({ quoteCode })} />
    </>}
  </div>
}
