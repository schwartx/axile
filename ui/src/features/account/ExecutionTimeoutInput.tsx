import { DurationInput } from '@/components/ui/DurationInput'
import { ExecutionTimeoutHelp } from '@/features/account/ExecutionTimeoutHelp'

interface ExecutionTimeoutInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  describedBy?: string
  invalid?: boolean
  size?: 'sm' | 'md'
}

/**
 * 账户执行超时：时分秒编辑，保留 30 秒步进、至少 1 秒与帮助入口。
 */
export function ExecutionTimeoutInput({
  id,
  value,
  onChange,
  describedBy,
  invalid = false,
  size,
}: ExecutionTimeoutInputProps) {
  return (
    <div className="flex items-center gap-1.5">
      <DurationInput
        size={size}
        id={id}
        value={value}
        onChange={onChange}
        step={30}
        min={1}
        ariaLabel="执行超时"
        invalid={invalid}
        describedBy={describedBy}
      />
      <ExecutionTimeoutHelp />
    </div>
  )
}
