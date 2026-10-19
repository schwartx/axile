/** 与定时开关保持相同尺寸；原生按钮提供 Space / Enter 操作。 */
export function AccessibilitySwitch({ checked, labelledBy, describedBy, disabled = false, onChange }: {
  checked: boolean
  disabled?: boolean
  labelledBy: string
  describedBy: string
  onChange: (checked: boolean) => void
}) {
  return <button type="button" role="switch" aria-checked={checked} disabled={disabled}
    aria-labelledby={labelledBy} aria-describedby={describedBy}
    onClick={() => onChange(!checked)}
    className={`relative h-[27px] w-[46px] shrink-0 rounded-full focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${checked ? 'bg-accent' : 'bg-border-strong'}`}>
    <span aria-hidden="true" className={`absolute top-[3px] h-[21px] w-[21px] rounded-full bg-surface shadow ${checked ? 'left-[22px]' : 'left-[3px]'}`} />
  </button>
}
