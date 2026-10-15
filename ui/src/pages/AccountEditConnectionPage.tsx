/** 账户连接设置：按渠道描述渲染字段，支持已保存凭据回显。 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useParams } from 'react-router'
import { ConnectionValidationAside } from '@/features/account/connectionValidation/ConnectionValidationAside'

import { ConditionalReveal } from '@/components/ui/ConditionalReveal'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { Segmented } from '@/components/ui/Segmented'
import { channelAccountFieldVisible, conditionalRevealFields, isConditionalRevealField, updateChannelAccountConfig } from '@/features/setup/channelAccountFields'
import { initialConnectionDraft, mergedConnectionConfig, sameConnectionConfig } from '@/features/account/connectionConfig'
import { connectionValueError } from '@/components/ui/connectionFieldValue'
import { EditError, EditLoading, EditSaveBar, Section } from '@/features/account/editUi'
import { AccountPageTitle } from '@/features/account/pageHead'
import { getAccount, updateAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import type { ChannelAccountField } from '@/types/api'

export function AccountEditConnectionPage() {
  const accountId = Number(useParams().id)
  const toast = useToastStore((state) => state.toast)
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const catalogLoading = useChannelCatalogStore((state) => state.loading)
  const account = usePolling(useCallback((signal: AbortSignal) => getAccount(accountId, signal), [accountId]), {
    queryKey: `account:${accountId}:connection`, intervalMs: 0,
  })
  const acc = account.data
  const accounts = useDomainStore((state) => state.accounts)
  const cachedAccount = accounts?.find((item) => item.account_id === accountId) ?? null
  const channelDescriptor = useChannelDescriptor(acc?.trade_channel)
  const fields = useMemo(() => channelDescriptor?.account_form.fields ?? [], [channelDescriptor])
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saveError, setSaveError] = useState<Error | null>(null)

  // 首帧用仪表盘缓存名/渠道，加载态也渲染真实标题：新侧快照缺名会让账户名
  // 原地淡出再随数据闪现，读起来像「位移一下又回来」。
  const title = (
    <div className="flex flex-wrap items-center gap-3">
      <AccountPageTitle
        accountId={accountId}
        page="连接设置"
        name={acc?.name ?? cachedAccount?.name}
        channel={acc?.trade_channel ?? cachedAccount?.trade_channel}
        market={acc?.market ?? cachedAccount?.market}
      />
    </div>
  )

  useEffect(() => {
    if (acc && channelDescriptor && draft === null) setDraft(initialConnectionDraft(acc, fields))
  }, [acc, channelDescriptor, draft, fields])

  if (account.error && !acc) return <EditError error={account.error} onRetry={account.refresh} />
  if (!acc || !draft || (catalogLoading && !channelDescriptor))
    return (
      <section className="pb-24">
        {title}
        <EditLoading bare />
      </section>
    )

  const setField = (field: ChannelAccountField, value: unknown) => {
    setSaveError(null)
    setDraft((current) => current ? updateChannelAccountConfig(fields, current, field.name, value) : current)
    setErrors((current) => {
      if (!(field.name in current)) return current
      const next = { ...current }
      delete next[field.name]
      return next
    })
  }
  const nextConfig = mergedConnectionConfig(fields, draft)
  const changed = !sameConnectionConfig(draft, initialConnectionDraft(acc, fields))

  const validate = () => {
    const nextErrors: Record<string, string> = {}
    for (const field of fields) {
      if (!channelAccountFieldVisible(field, draft)) continue
      const value = nextConfig[field.name]
      if (field.kind === 'select') {
        if (field.required && !field.options?.some((option) => option.value === value)) nextErrors[field.name] = `请选择${field.label}`
      } else if (field.kind !== 'boolean') {
        const error = connectionValueError({ kind: field.kind, value: String(value ?? ''), required: field.required, label: field.label, placeholder: field.placeholder, constraints: field.constraints })
        if (error) nextErrors[field.name] = error
      }
    }
    setErrors(nextErrors)
    return Object.keys(nextErrors).length === 0
  }

  /** 取消：草稿回到服务端当前配置，清掉字段校验错误。 */
  const cancelEdit = () => {
    setDraft(initialConnectionDraft(acc, fields))
    setErrors({})
    setSaveError(null)
  }

  const save = async () => {
    if (!changed) return toast('没有改动')
    if (!validate()) return toast('请检查连接设置')
    setSaveError(null)
    try {
      const updated = await updateAccount(accountId, { account_config: nextConfig })
      toast('连接设置已更新')
      void refreshAccounts()
      account.refresh()
      setDraft(initialConnectionDraft(updated, fields))
    } catch (caught) {
      setSaveError(caught instanceof Error ? caught : new Error(String(caught)))
    }
  }

  const renderControl = (field: ChannelAccountField) => {
    const raw = draft[field.name] ?? field.default ?? ''
    if (field.kind === 'boolean') return <Segmented value={raw === true ? 'true' : 'false'} options={[{ value: 'false', label: '关闭' }, { value: 'true', label: '启用' }]} onChange={(value) => setField(field, value === 'true')} />
    if (field.kind === 'select') return <Segmented value={String(raw)} options={field.options ?? []} onChange={(value) => setField(field, value)} />
    return <ConnectionField label={field.label} kind={field.kind} value={String(raw)} required={field.required} placeholder={field.placeholder} help={field.help} error={errors[field.name]} constraints={field.constraints} clipboard={field.clipboard} onChange={(value) => setField(field, value)} />
  }

  const renderForm = (action?: ReactNode) => (
      <div className="min-w-0 [&>div]:mt-0">
      <Section label={`${channelDescriptor?.label ?? acc.trade_channel} 连接`} action={action}>
        {fields.map((field) => {
          if (isConditionalRevealField(fields, field)) return null
          const visible = channelAccountFieldVisible(field, draft)
          return <div key={field.name} inert={!visible} className={`${field.width === 'full' ? 'md:col-span-2' : ''} grid transition-[grid-template-rows] duration-200 ${visible ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden">
            {field.kind === 'select' && field.presentation === 'conditional_reveal' ? <ConditionalReveal label={field.label} help={field.help} value={String(draft[field.name] ?? '')} options={field.options ?? []} error={errors[field.name]} onChange={(value) => setField(field, value)} renderPanel={(option) => conditionalRevealFields(fields, field.name, option).map((dependent) => <div key={dependent.name} className="mt-3">{renderControl(dependent)}</div>)} /> : <>{(field.kind === 'boolean' || field.kind === 'select') && <div className="mb-2 text-[14px] text-ink-2">{field.label}</div>}{renderControl(field)}</>}
          </div></div>
        })}
      </Section>
      </div>
  )

  return (
    <section>
      {title}
      <div className={`relative mt-7 ${channelDescriptor?.connection_validation ? 'xl:grid xl:grid-cols-[minmax(0,1fr)_320px] xl:items-start xl:gap-8' : ''}`}>
      {channelDescriptor?.connection_validation ? <ConnectionValidationAside
        descriptor={channelDescriptor} config={nextConfig} accountId={accountId}
      >{renderForm}</ConnectionValidationAside> : renderForm()}
      </div>
      <EditSaveBar changes={changed ? ['连接设置已改'] : []} blocked={Object.keys(errors).length > 0} onCancel={cancelEdit} onSave={() => void save()} error={saveError} />
    </section>
  )
}
