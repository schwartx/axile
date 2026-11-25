import { useState } from 'react'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { DirectoryPicker } from '@/components/ui/DirectoryPicker'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { ChannelConditionalReveal } from '@/features/setup/ChannelConditionalReveal'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import type { SignalDraft, SignalQuoteErrors } from './signalData'

export function SignalGmConnection({ draft, errors, onChange }: {
  draft: SignalDraft
  errors?: SignalQuoteErrors
  onChange: (patch: Partial<SignalDraft>) => void
}) {
  const [browsing, setBrowsing] = useState(false)
  const descriptor = useChannelDescriptor('gm')
  const { error, refresh } = useChannelCatalogStore()
  const fields = descriptor?.account_form.fields ?? []
  const mode = fields.find((field) => field.name === 'connection_mode')

  return <div className="grid min-w-0 gap-3">
    <ConnectionField label="掘金 Token" kind="secret" required error={errors?.gmToken}
      value={draft.gmToken} onChange={(gmToken) => onChange({ gmToken })} />
    {mode ? <ChannelConditionalReveal field={mode} fields={fields} value={draft.gmMode}
      onChange={(gmMode) => onChange({ gmMode: gmMode as SignalDraft['gmMode'] })}
      renderField={(field) => {
        const directory = field.kind === 'directory'
        return <ConnectionField label={field.label} kind={directory ? 'directory' : 'endpoint'}
          required={field.required && (!directory || draft.gmTerminalPath !== null)}
          value={directory ? draft.gmTerminalPath ?? '' : draft.gmAddress}
          error={directory ? errors?.gmTerminalPath : errors?.gmAddress}
          placeholder={field.placeholder} fillablePlaceholder={field.fillable_placeholder}
          help={field.help} constraints={field.constraints} clipboard={field.clipboard}
          onBrowse={directory ? () => setBrowsing(true) : undefined}
          onChange={(value) => onChange(directory ? { gmTerminalPath: value } : { gmAddress: value })} />
      }} /> : <ErrorNotice title="掘金连接配置加载失败" intent="read" error={error} onRetry={refresh} />}
    <DirectoryPicker open={browsing} initialPath={draft.gmTerminalPath ?? ''} onClose={() => setBrowsing(false)}
      onSelect={(gmTerminalPath) => onChange({ gmTerminalPath })} />
  </div>
}
