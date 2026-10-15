import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import {
  quantityText,
  sizingFormulas,
  sizingReasonText,
  weightText,
} from '@/features/account/sizingEvidenceModel'
import type { TargetSizingRow } from '@/types/api'

export function SizingEvidence({
  row,
  quantityLabel = '',
  currency = '',
  defaultExpanded = false,
}: {
  row: TargetSizingRow
  quantityLabel?: string
  currency?: string
  defaultExpanded?: boolean
}) {
  const [open, setOpen] = useState(defaultExpanded)
  const detailId = useId()
  const reason = sizingReasonText(row, quantityLabel)
  const input = row.sizing_mode === 'quantity' ? quantityText(row.account_weight, quantityLabel) : weightText(row.account_weight)
  const summary = `${input} → ${quantityText(row.target_quantity, quantityLabel)} · ${reason}`
  const formulas = sizingFormulas(row, quantityLabel, currency)

  return (
    <div>
      <button
        type="button"
        className="group flex min-h-8 w-full items-center gap-2 text-left"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="num min-w-0 flex-1 text-[13.5px] text-ink-2">{summary}</span>
        <ChevronDown
          size={15}
          aria-hidden
          className={`flex-none text-ink-3 transition-transform duration-200 motion-reduce:transition-none ${open ? 'rotate-180' : ''}`}
        />
      </button>
      <div
        inert={!open}
        className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}
      >
        <div id={detailId} className="min-h-0 overflow-hidden">
          <div className="num border-l border-line py-1.5 pl-3 text-[12.5px] leading-5 text-ink-3">
            {formulas.map((formula) => <div key={formula}>{formula}</div>)}
          </div>
        </div>
      </div>
    </div>
  )
}
