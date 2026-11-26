import type { ReactNode } from 'react'
import { ConditionalReveal } from '@/components/ui/ConditionalReveal'
import { conditionalRevealFields } from '@/features/setup/channelAccountFields'
import type { ChannelAccountField } from '@/types/api'

/** 使用渠道描述的选项和从属字段渲染连接方式。 */
export function ChannelConditionalReveal({ field, fields, value, error, onChange, renderField }: {
  field: ChannelAccountField
  fields: ChannelAccountField[]
  value: string
  error?: string
  onChange: (value: string) => void
  renderField: (field: ChannelAccountField) => ReactNode
}) {
  return <ConditionalReveal label={field.label} help={field.help} value={value} options={field.options ?? []}
    error={error} onChange={onChange}
    renderPanel={(option) => conditionalRevealFields(fields, field.name, option).map(
      (dependent) => <div key={dependent.name}>{renderField(dependent)}</div>,
    )} />
}
