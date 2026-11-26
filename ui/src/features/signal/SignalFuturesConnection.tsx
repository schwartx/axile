import { ConnectionField } from '@/components/ui/ConnectionField'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import type { SignalDraft, SignalQuoteErrors } from './signalData'

const fieldsByChannel = {
  ctp: ['md_front'],
  tq: ['tq_username', 'tq_password'],
} as const

export function SignalFuturesConnection({ channel, draft, errors, onChange }: {
  channel: keyof typeof fieldsByChannel
  draft: SignalDraft
  errors?: SignalQuoteErrors
  onChange: (patch: Partial<SignalDraft>) => void
}) {
  const descriptor = useChannelDescriptor(channel)
  const { error, refresh } = useChannelCatalogStore()
  if (!descriptor) return <ErrorNotice title="行情配置加载失败" intent="read" error={error} onRetry={refresh} />

  return <div className="grid min-w-0 gap-3">
    {fieldsByChannel[channel].map((name) => {
      const field = descriptor.account_form.fields.find((candidate) => candidate.name === name)
      if (!field || field.kind === 'boolean' || field.kind === 'select') return null
      return <ConnectionField key={name} label={field.label} kind={field.kind} required
        constraints={field.constraints} clipboard={field.clipboard}
        placeholder={field.placeholder} fillablePlaceholder={field.fillable_placeholder}
        help={channel === 'ctp'
          ? '权重目标需要共享合约目录中的乘数。启用定时执行前，请用此账户试跑组合，检查能否换算手数。'
          : field.help}
        value={draft[name]} error={errors?.[name]}
        onChange={(value) => onChange({ [name]: value })} />
    })}
  </div>
}
