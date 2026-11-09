import { useRef } from 'react'
import { StepperNumberInput } from '@/components/ui/StepperNumberInput'
import { ExecutionTimeoutHelp } from '@/features/account/ExecutionTimeoutHelp'
import { stepExecutionTimeout } from '@/features/account/executionTimeout'

interface ExecutionTimeoutInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  describedBy?: string
  invalid?: boolean
  size?: 'sm' | 'md'
}

/**
 * 可直接输入、亦可按 30 秒步进的账户执行超时控件。
 * 交互外壳是 :func:`StepperNumberInput`；此处只保留业务参数（30 秒步进、1..540 秒、
 * 秒单位、滚动数字展示最近合法值）与帮助入口。
 */
export function ExecutionTimeoutInput({
  id,
  value,
  onChange,
  describedBy,
  invalid = false,
  size,
}: ExecutionTimeoutInputProps) {
  const parsedValue = Number(value)
  const validValue =
    value.trim() !== '' && Number.isInteger(parsedValue) && parsedValue >= 1 && parsedValue <= 540
      ? parsedValue
      : null
  const lastValidValue = useRef(validValue ?? 1)
  if (validValue !== null) lastValidValue.current = validValue

  return (
    <div className="flex items-center gap-1.5">
      <StepperNumberInput
        size={size}
        id={id}
        value={value}
        onChange={onChange}
        step={30}
        min={1}
        max={540}
        unit="秒"
        invalid={invalid}
        describedBy={describedBy}
        onStep={stepExecutionTimeout}
        displayValue={lastValidValue.current}
      />
      <ExecutionTimeoutHelp />
    </div>
  )
}
