import { CircleHelp, Plus, Trash2 } from 'lucide-react'
import { useId, useRef, useState, type Ref } from 'react'
import { Toggle } from '@/features/account/editUi'
import { Card } from '@/components/ui/Card'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
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

function CellInput({ label, value, error, numeric, inputRef, onChange, large = false, showMagnitude = true }: {
  label: string; value: string; error?: string; numeric?: boolean; large?: boolean; showMagnitude?: boolean; inputRef?: Ref<HTMLInputElement>; onChange: (next: string) => void
}) {
  const messageId = useId()
  const valueHint = numeric && showMagnitude && !error ? magnitudeHint(value) : undefined
  return <div className="grid min-w-0 gap-1">
    <label className={`flex min-w-0 items-center gap-2 rounded-md border bg-surface px-2.5 focus-within:border-accent ${large ? 'h-11' : 'h-8'} ${error ? 'border-warn' : 'border-line'}`}>
      <input ref={inputRef} aria-label={label} aria-invalid={Boolean(error) || undefined} aria-describedby={error || valueHint ? messageId : undefined}
        inputMode={numeric ? 'decimal' : 'text'} autoComplete="off" spellCheck={false}
        className={`h-full min-w-0 flex-1 bg-transparent text-ink-1 outline-none ${large ? 'text-lg font-semibold' : 'text-[13px]'} ${numeric ? 'num' : ''}`}
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

export function SignalManualAssets({ value, errors, onChange, stockFollow }: {
  stockFollow: boolean
  value: SignalDraft
  errors?: SignalAccountErrors
  onChange: (patch: Partial<SignalDraft>) => void
}) {
  const followDescription = useId()
  const followLedger = stockFollow && value.followTarget
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

  return <div className="grid gap-6">
    <section aria-label="跟随设置" className="grid gap-4">
      <div className="flex items-center justify-between gap-5">
        <div className="space-y-1.5">
          <div className="flex items-center gap-1">
            <h2 className="text-base font-semibold text-ink-1">跟随目标</h2>
            <Tooltip content="开启后，系统将定时目标结转为信号持仓。关闭后，按生成的信号手动调仓，持仓由你录入。" arrow>
              <button type="button" aria-label="跟随目标说明" onClick={(event) => event.currentTarget.focus()}
                className="inline-flex size-6 cursor-help items-center justify-center rounded text-ink-3 hover:text-ink-2 focus-visible:outline-accent">
                <CircleHelp size={14} aria-hidden />
              </button>
            </Tooltip>
          </div>
          <p id={followDescription} className="text-sm leading-relaxed text-ink-3">
            {value.followTarget ? '定时计算成功后，将目标记为信号持仓。' : '按生成的信号手动调仓。'}
          </p>
        </div>
        <Toggle on={value.followTarget} onClick={() => onChange({ followTarget: !value.followTarget })}
          ariaLabel="跟随目标" describedBy={followDescription} />
      </div>
    </section>
    <section aria-label="账户资金" className="@container rounded-card border border-line bg-surface px-4 py-4 sm:px-5">
      <div className="grid items-start gap-x-6 gap-y-4 @min-[52rem]:grid-cols-[max-content_minmax(0,1fr)]">
        <div className="flex items-center gap-1 @min-[52rem]:self-center">
          <h2 className="text-base font-semibold text-ink-1">账户资金</h2>
          {followLedger && <Tooltip content="按变动金额计费。权益由现金和持仓市值计算，未填写起始持仓时按空仓起步。" arrow>
            <button type="button" aria-label="账户资金说明" onClick={(event) => event.currentTarget.focus()}
              className="inline-flex size-6 cursor-help items-center justify-center rounded text-ink-3 hover:text-ink-2 focus-visible:outline-accent">
              <CircleHelp size={14} aria-hidden />
            </button>
          </Tooltip>}
        </div>
        <div className={`grid min-w-0 items-start gap-x-6 gap-y-4 ${followLedger
          ? '@min-[34rem]:grid-cols-[minmax(0,1fr)_8rem_8rem]'
          : '@min-[34rem]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_8rem]'}`}>
          {!followLedger && <ConnectionField kind="money" appearance="flat" label="账户权益" placeholder="输入金额" required value={value.equity} error={errors?.equity}
            valueHint={magnitudeHint(value.equity)} onChange={(equity) => onChange(signalEquityPatch(value, equity))} />}
          <ConnectionField kind="money" appearance="flat" label={followLedger ? '期初可用资金' : '可用资金'} placeholder="输入金额" required value={value.cash} error={errors?.cash}
            valueHint={magnitudeHint(value.cash)} onChange={(cash) => onChange({ cash, cashFromEquity: false })} />
          {followLedger && <div className="grid min-w-0 max-w-40 gap-1.5">
            <span className="text-[13px] leading-4 text-ink-2">手续费率（bp）</span>
            <CellInput label="手续费率（bp）" value={value.followFeeBps} large numeric showMagnitude={false}
              error={errors?.followFeeBps} onChange={(followFeeBps) => onChange({ followFeeBps })} />
          </div>}
          <div className="grid min-w-0 max-w-40 gap-1.5">
            <span className="text-[13px] leading-4 text-ink-2">计价币种</span>
            <CellInput label="计价币种" large value={value.currency} error={errors?.currency} onChange={(currency) => onChange({ currency })} />
          </div>
        </div>
      </div>
    </section>
    <section aria-label="持仓设置" className="border-t border-line pt-5">
      <div className="flex min-h-10 flex-wrap items-center justify-between gap-x-5 gap-y-3">
        <div className="space-y-1.5">
          <h2 className="text-base font-semibold text-ink-1">{followLedger ? "起始持仓" : "实际持仓"} <span className="text-[13px] font-normal text-ink-3">（选填）</span></h2>
          {!hasPositions && <p role="status" className="text-[13px] leading-5 text-ink-3">
            <InkRewrite text={value.positionsKnown ? '已确认当前无持仓' : '未提供持仓'} tone="label" />
          </p>}
        </div>
        <div className="ml-auto flex items-center gap-3">
          {!hasPositions && <button type="button" onClick={() => onChange({ positionsKnown: !value.positionsKnown })}
            className="h-10 cursor-pointer rounded-md px-3 text-sm text-ink-2 hover:bg-fill">
            {value.positionsKnown ? '撤销确认' : '确认空仓'}
          </button>}
          <button ref={addButton} type="button" onClick={addPosition}
            className="flex h-10 cursor-pointer items-center gap-2 rounded-md border border-line bg-surface px-4 text-sm text-ink-1 hover:bg-fill">
            <Plus size={14} aria-hidden />添加持仓
          </button>
        </div>
      </div>
      <div inert={!hasPositions} className={`grid transition-[grid-template-rows] ${MOTION_LAYOUT} ${hasPositions ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="min-h-0 overflow-clip">
          <Card className="mt-4 overflow-hidden">
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
