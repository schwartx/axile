/** 输入框为空时可选填入的示例。父级负责定位，填入后直接回到输入框。 */
export function InputHint({ inputId, value, suggestion, onFill, disabled, className = 'left-0' }: {
  inputId: string
  value: string
  suggestion?: string
  onFill: (value: string) => void
  disabled?: boolean
  className?: string
}) {
  if (!suggestion || value || disabled) return null

  return (
    <button
      type="button"
      aria-label={`填入 ${suggestion}`}
      title="点击填入"
      className={`absolute inset-y-0 cursor-pointer rounded-sm text-ink-3 [&:hover]:text-ink-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${className}`}
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => {
        onFill(suggestion)
        document.getElementById(inputId)?.focus()
      }}
    >
      {suggestion}
    </button>
  )
}
