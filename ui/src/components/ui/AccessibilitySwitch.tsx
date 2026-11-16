/** 默认与定时开关同尺寸；紧凑样式供状态栏使用，仍保留 24px 点击高度。 */
export function AccessibilitySwitch({ checked, labelledBy, describedBy, disabled = false, compact = false, onChange }: {
  checked: boolean
  disabled?: boolean
  compact?: boolean
  labelledBy: string
  describedBy: string
  onChange: (checked: boolean) => void
}) {
  const thumbPosition = compact ? (checked ? 'left-3.5' : 'left-0.5') : (checked ? 'left-[22px]' : 'left-[3px]')
  return <button type="button" role="switch" aria-checked={checked} disabled={disabled}
    aria-labelledby={labelledBy} aria-describedby={describedBy}
    onClick={() => onChange(!checked)}
    className={`relative shrink-0 rounded-full focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${compact ? 'h-6 w-8' : 'h-[27px] w-[46px]'}`}>
    <span aria-hidden="true" className={`absolute rounded-full ${compact ? 'left-0.5 top-1 h-4 w-7' : 'inset-0'} ${checked ? 'bg-accent' : 'bg-border-strong'}`}>
      <span className={`absolute rounded-full bg-surface shadow ${compact ? 'top-0.5 h-3 w-3' : 'top-[3px] h-[21px] w-[21px]'} ${thumbPosition}`} />
    </span>
  </button>
}
