import { Plus, Trash2 } from 'lucide-react'
import { useId, useRef, useState, type Ref } from 'react'
import { Card } from '@/components/ui/Card'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Segmented } from '@/components/ui/Segmented'
import { amountMagnitude } from '@/components/ui/amountMagnitude'
import { isDecimalDraft, normalizeMoneyValue } from '@/components/ui/connectionFieldValue'
import { MOTION_LAYOUT } from '@/lib/viewTransition'
import { positionVolumePatch, signalEquityPatch, type PositionDraft, type SignalAccountErrors, type SignalDraft, type SignalPositionErrors } from './signalData'

const directions = [{ value: '多头' as const, label: '多头' }, { value: '空头' as const, label: '空头' }]

function magnitudeHint(value: string): string | undefined {
  const normalized = normalizeMoneyValue(value) ?? ''
  const magnitude = amountMagnitude(normalized.replace(/^[+-]/, ''))
  return magnitude ? `${normalized.startsWith('-') ? '-' : ''}${magnitude}` : undefined
}

function CellInput({ label, value, error, numeric, inputRef, onChange }: {
  label: string; value: string; error?: string; numeric?: boolean; inputRef?: Ref<HTMLInputElement>; onChange: (next: string) => void
}) {
  const messageId = useId()
  const valueHint = numeric && !error ? magnitudeHint(value) : undefined
  return <div className="grid min-w-0 gap-1">
    <label className={`flex h-8 min-w-0 items-center gap-2 rounded-md border bg-surface px-2.5 focus-within:border-accent ${error ? 'border-warn' : 'border-line'}`}>
      <input ref={inputRef} aria-label={label} aria-invalid={Boolean(error) || undefined} aria-describedby={error || valueHint ? messageId : undefined}
        inputMode={numeric ? 'decimal' : 'text'} autoComplete="off" spellCheck={false}
        className={`h-full min-w-0 flex-1 bg-transparent text-[13px] text-ink-1 outline-none ${numeric ? 'num' : ''}`}
        value={value} onChange={(event) => {
          const next = event.target.value
          if (!numeric || isDecimalDraft(next)) onChange(next)
        }} />
      {valueHint && <span id={messageId} title={valueHint} className="min-w-0 max-w-[40%] truncate text-right text-xs text-ink-3">
        {valueHint}
      </span>}
    </label>
    {error && <p id={messageId} className="text-xs text-warn">{error}</p>}
  </div>
}

export function SignalManualAssets({ value, errors, onChange }: {
  value: SignalDraft
  errors?: SignalAccountErrors
  onChange: (patch: Partial<SignalDraft>) => void
}) {
  const addButton = useRef<HTMLButtonElement>(null)
  const focusPosition = useRef<string | null>(null)
  const [closingPosition, setClosingPosition] = useState<{ row: PositionDraft; errors?: SignalPositionErrors } | null>(null)
  const hasPositions = value.positionsKnown && value.positions.length > 0
  const displayedRows = hasPositions ? value.positions : closingPosition ? [closingPosition.row] : []
  const updatePosition = (id: string, patch: Partial<PositionDraft>) => onChange({
    positions: value.positions.map((row) => row.id === id ? { ...row, ...patch } : row),
  })
  const addPosition = () => {
    const id = crypto.randomUUID()
    focusPosition.current = id
    setClosingPosition(null)
    onChange({ positionsKnown: true, positions: [...value.positions, { id, symbol: '', direction: '多头', volume: '', available: '' }] })
  }
  const removePosition = (row: PositionDraft, index: number) => {
    const positions = value.positions.filter((item) => item.id !== row.id)
    if (positions.length === 0) {
      // 保留最后一行的显示，直到表格沿原高度收起。
      setClosingPosition({ row, errors: errors?.positions?.[row.id] })
      addButton.current?.focus()
    } else {
      focusPosition.current = positions[Math.min(index, positions.length - 1)].id
    }
    onChange({ positionsKnown: positions.length > 0, positions })
  }

  return <div className="grid gap-4">
    <section aria-label="账户资金" className="grid gap-3">
      <div className="flex items-start justify-between gap-3">
        <h2 className="self-start pt-1.5 text-[15px] font-semibold text-ink-1">账户资金</h2>
        <div className="flex items-start gap-2 text-[13px] text-ink-2">
          <span className="pt-1.5">计价币种</span>
          <div className="min-w-20 max-w-36">
            <CellInput label="计价币种" value={value.currency} error={errors?.currency} onChange={(currency) => onChange({ currency })} />
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 items-start gap-3">
        <ConnectionField kind="money" label="账户权益" required value={value.equity} error={errors?.equity}
          valueHint={magnitudeHint(value.equity)} onChange={(equity) => onChange(signalEquityPatch(value, equity))} />
        <ConnectionField kind="money" label="可用资金" required value={value.cash} error={errors?.cash}
          valueHint={magnitudeHint(value.cash)} onChange={(cash) => onChange({ cash, cashFromEquity: false })} />
      </div>
    </section>
    <section aria-label="实际持仓" className="border-t border-line pt-3">
      <div className="flex min-h-8 flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div>
          <h2 className="text-[15px] leading-4 font-semibold text-ink-1">实际持仓 <span className="text-xs font-normal text-ink-3">（选填）</span></h2>
          {!hasPositions && <p role="status" className="text-xs leading-4 text-ink-3">
            <InkRewrite text={value.positionsKnown ? '已确认当前无持仓' : '未提供持仓'} tone="label" />
          </p>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {!hasPositions && <button type="button" onClick={() => onChange({ positionsKnown: !value.positionsKnown })}
            className="h-8 cursor-pointer rounded-md px-2 text-[13px] text-ink-2 hover:bg-fill">
            {value.positionsKnown ? '撤销确认' : '确认空仓'}
          </button>}
          <button ref={addButton} type="button" onClick={addPosition}
            className="flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-line bg-surface px-3 text-[13px] text-ink-1 hover:bg-fill">
            <Plus size={14} aria-hidden />添加持仓
          </button>
        </div>
      </div>
      <div inert={!hasPositions} className={`grid transition-[grid-template-rows] ${MOTION_LAYOUT} ${hasPositions ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="min-h-0 overflow-clip">
          <Card className="mt-2.5 overflow-hidden">
            <table className="w-full table-fixed text-left text-[13px]">
              <colgroup>
                <col className="sm:w-[22%]" />
                <col className="w-28" />
                <col className="w-1/5 sm:w-auto" />
                <col className="w-1/5 sm:w-auto" />
                <col className="w-11" />
              </colgroup>
              <thead className="border-b border-line text-ink-2">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">标的</th>
                  <th scope="col" className="px-2 py-2 font-medium">方向</th>
                  <th scope="col" className="px-2 py-2 font-medium">持仓数量</th>
                  <th scope="col" className="px-2 py-2 font-medium">可用数量</th>
                  <th scope="col"><span className="sr-only">操作</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {displayedRows.map((row, index) => {
                  const label = `第 ${index + 1} 行`
                  const fields = hasPositions ? errors?.positions?.[row.id] : closingPosition?.errors
                  return <tr key={row.id} className="align-top">
                    <td className="px-3 py-2"><CellInput label={`${label}标的`} value={row.symbol} error={fields?.symbol}
                      inputRef={(input) => {
                        if (!input || focusPosition.current !== row.id) return
                        focusPosition.current = null
                        input.focus()
                      }}
                      onChange={(symbol) => updatePosition(row.id, { symbol })} /></td>
                    <td className="px-2 py-2"><div role="group" aria-label={`${label}持仓方向`}>
                      <Segmented size="sm" className="h-8 whitespace-nowrap" value={row.direction} options={directions}
                        onChange={(direction) => updatePosition(row.id, { direction })} />
                    </div></td>
                    <td className="px-2 py-2"><CellInput numeric label={`${label}持仓数量`} value={row.volume} error={fields?.volume}
                      onChange={(volume) => updatePosition(row.id, positionVolumePatch(row, volume))} /></td>
                    <td className="px-2 py-2"><CellInput numeric label={`${label}可用数量`} value={row.available} error={fields?.available}
                      onChange={(available) => updatePosition(row.id, { available, availableFromVolume: false })} /></td>
                    <td className="py-2"><button type="button" aria-label={`删除${label}持仓`}
                      className="grid size-8 cursor-pointer place-items-center rounded-md text-ink-3 hover:bg-fill hover:text-ink-1"
                      onClick={() => removePosition(row, index)}><Trash2 size={15} aria-hidden /></button></td>
                  </tr>
                })}
              </tbody>
            </table>
          </Card>
        </div>
      </div>
    </section>
  </div>
}
