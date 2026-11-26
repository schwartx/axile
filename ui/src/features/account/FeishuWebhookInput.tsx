import { TEXT } from '@/features/account/editUi'
import { extractFeishuKey } from '@/features/account/feishuUpdate'

export function FeishuWebhookInput({ value, onChange, configured, disabled, required, label = '飞书 Webhook', className = '' }: {
  value: string
  onChange: (value: string) => void
  configured?: boolean
  disabled?: boolean
  required?: boolean
  label?: string
  className?: string
}) {
  return <div className={`min-w-0 flex-1 ${className}`}>
    <input aria-label={label} type="text" className={TEXT}
      value={value} disabled={disabled} required={required}
      placeholder={configured ? '已配置 · 留空保持不变' : '可粘贴整条 webhook 链接'}
      spellCheck={false} autoComplete="off" onChange={(event) => onChange(event.target.value)}
      onBlur={() => {
        const key = extractFeishuKey(value)
        if (key !== value) onChange(key)
      }} />
  </div>
}
