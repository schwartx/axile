import { useEffect, useRef, useState } from 'react'
import { Bitcoin, ChartCandlestick, CircleHelp, Landmark, Monitor, RadioTower, type LucideIcon } from 'lucide-react'
import { useNavigate } from '@/components/ui/nav'
import { ExecutionTimeoutInput } from '@/features/account/ExecutionTimeoutInput'
import { executionTimeoutError } from '@/features/account/executionTimeout'
import { LeverageInput } from '@/features/account/LeverageInput'
import { leverageError } from '@/features/account/leverage'
import { WizardHeading, WizardPage, WizardNav } from '@/features/setup/WizardNav'
import { Segmented } from '@/components/ui/Segmented'
import { ConditionalReveal } from '@/components/ui/ConditionalReveal'
import { Skeleton } from '@/components/ui/Skeleton'
import { WeightBars } from '@/components/viz/WeightBars'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { CreatePortfolioModal } from '@/features/portfolio/CreatePortfolioModal'
import { PortfolioCodePreview } from '@/features/portfolio/PortfolioCodePreview'
import { PortfolioChoiceCard } from '@/features/setup/PortfolioChoiceCard'
import { DirectoryPicker } from '@/components/ui/DirectoryPicker'
import type { ClipboardCandidate } from '@/components/ui/connectionFieldClipboard'
import {
  connectionValueError,
  normalizeMoneyValue,
  type ConnectionValidationContext,
} from '@/components/ui/connectionFieldValue'
import { refreshPortfolioTargetSnapshot } from '@/lib/api/portfolios'
import { createAccount } from '@/lib/api/accounts'
import { useDomainStore } from '@/stores/domain'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import { initialTimerForSchedule, useWizardStore } from '@/stores/wizard'
import { useToastStore } from '@/stores/ui'
import { resolveCronList, cronToExpr, describeCron, fmtFire, nextFires } from '@/features/setup/cron'
import { TimerEditor, type TimerEditorState } from '@/features/setup/TimerEditor'
import { algoLabel, validateAlgorithmRef } from '@/features/setup/algorithms'
import { AlgorithmEditor } from '@/features/setup/AlgorithmEditor'
import {
  channelAccountFieldVisible,
  conditionalRevealFields,
  isConditionalRevealField,
  updateChannelAccountConfig,
  visibleChannelAccountConfig,
} from '@/features/setup/channelAccountFields'
import type {
  PortfolioLite,
  ChannelAccountField,
  ChannelCapability,
} from '@/types/api'

function algorithmSummary(algo: { method: string; params: Record<string, unknown> }): string {
  return algoLabel(algo.method)
}

const CHANNEL_ICONS: Record<string, LucideIcon> = {
  bitcoin: Bitcoin,
  'chart-candlestick': ChartCandlestick,
  landmark: Landmark,
  monitor: Monitor,
  'radio-tower': RadioTower,
}

function ChannelIcon({ name }: { name: string }) {
  const Icon = CHANNEL_ICONS[name] ?? CircleHelp
  return <Icon aria-hidden="true" className="h-6 w-6" strokeWidth={1.8} />
}

function accountFields(descriptor?: ChannelCapability): ChannelAccountField[] {
  return descriptor?.account_form.fields ?? []
}

function accountConfigDefaults(fields: ChannelAccountField[]): Record<string, unknown> {
  return Object.fromEntries(fields.filter((field) => field.default !== undefined).map((field) => [field.name, field.default]))
}

function channelDraft(channel: ChannelCapability) {
  return {
    channel: channel.channel,
    config: accountConfigDefaults(accountFields(channel)),
    algorithm: channel.defaults.trade_algorithm,
    longLeverage: String(channel.defaults.long_leverage),
    shortLeverage: String(channel.defaults.short_leverage),
    executionTimeout: String(channel.defaults.execution_timeout),
    ...initialTimerForSchedule(channel.schedule.kind),
  }
}

/* -------- 1 选渠道 -------- */
export function AcctChannel() {
  const { acct, setAcct } = useWizardStore()
  const channels = useChannelCatalogStore((state) => state.channels)
  const loading = useChannelCatalogStore((state) => state.loading)
  const error = useChannelCatalogStore((state) => state.error)
  const refresh = useChannelCatalogStore((state) => state.refresh)

  const selectChannel = (channel: ChannelCapability) => {
    setAcct(channelDraft(channel))
  }

  // 目录就绪后，为空草稿或失效选择落到第一个可用渠道。
  useEffect(() => {
    if (!channels) return
    if (channels.some((channel) => channel.channel === acct.channel && channel.available)) return
    const firstAvailable = channels.find((channel) => channel.available)
    if (firstAvailable) setAcct(channelDraft(firstAvailable))
  }, [channels, acct.channel, setAcct])

  const selected = channels?.find((channel) => channel.channel === acct.channel)
  const nextDisabled = !selected?.available

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <WizardPage kicker="账户设置 · 1 / 6" title="连接哪个交易所 / 券商？" lead="不同渠道连接方式完全不同——选定后进入对应的连接页。">
          <div className="grid grid-cols-1 gap-3">
            {loading && channels === null && <p className="text-[15px] text-ink-2">加载渠道…</p>}
            <ErrorNotice title="渠道目录加载失败" error={channels === null ? error : null} onRetry={refresh} />
            {channels?.map((ch) => {
              const available = ch.available
              const isSelected = acct.channel === ch.channel
              return (
                <button
                  key={ch.channel}
                  disabled={!available}
                  className={`flex w-full items-start gap-4 rounded-[14px] border p-[18px] text-left transition ${
                    !available
                      ? 'cursor-not-allowed border-line bg-surface opacity-60'
                      : isSelected
                        ? 'border-accent bg-accent-soft shadow-[inset_0_0_0_1px_var(--color-accent)]'
                        : 'border-line bg-surface hover:border-ink-3/30'
                  }`}
                  onClick={() => available && selectChannel(ch)}
                >
                  <span className="grid h-11 w-11 flex-none place-items-center rounded-xl bg-fill">
                    <ChannelIcon name={ch.icon} />
                  </span>
                  <span>
                    <span className={`block text-[17px] font-[620] ${available ? '' : 'text-ink-3'}`}>{ch.label}</span>
                    <span className={`mt-0.5 block text-[14px] ${available ? 'text-ink-2' : 'text-warn'}`}>
                      {available ? ch.description : '暂不可用'}
                    </span>
                  </span>
                </button>
              )
            })}
          </div>
        </WizardPage>
      </div>
      <WizardNav nextTo="/setup/acct/connect" nextDisabled={nextDisabled} />
    </div>
  )
}

/* -------- 2 连接 -------- */
export function AcctConnect() {
  const { acct, setAcct } = useWizardStore()
  const descriptor = useChannelDescriptor(acct.channel)
  const navigate = useNavigate()
  const toast = useToastStore((state) => state.toast)
  const fields = accountFields(descriptor)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [directoryPickerField, setDirectoryPickerField] = useState<string | null>(null)
  const fieldRefs = useRef<Record<string, HTMLInputElement | null>>({})
  const setField = (key: string, val: unknown) => {
    const config = updateChannelAccountConfig(fields, acct.config, key, val)
    setAcct({ config })
    setErrors((current) => {
      const visible = new Set(fields.filter((field) => channelAccountFieldVisible(field, config)).map((field) => field.name))
      return Object.fromEntries(Object.entries(current).filter(([name]) => name === 'name' || visible.has(name)))
    })
  }

  const fieldValidationContext = (
    field: Pick<ChannelAccountField, 'kind' | 'required' | 'label' | 'placeholder' | 'constraints'>,
    value: string,
  ): ConnectionValidationContext | null => {
    if (field.kind === 'boolean' || field.kind === 'select') return null
    return {
      kind: field.kind,
      value,
      required: field.required,
      label: field.label,
      placeholder: field.placeholder,
      constraints: field.constraints,
    }
  }

  const validateScalarField = (
    field: Pick<ChannelAccountField, 'kind' | 'required' | 'label' | 'placeholder' | 'constraints'>,
    value: string,
  ): string | null => {
    const context = fieldValidationContext(field, value)
    return context ? connectionValueError(context) : null
  }

  const setFieldError = (name: string, error: string | null) => {
    setErrors((current) => {
      if (error) return current[name] === error ? current : { ...current, [name]: error }
      if (!(name in current)) return current
      const next = { ...current }
      delete next[name]
      return next
    })
  }

  const validateAccountName = (value: string) => {
    setFieldError('name', connectionValueError({
      kind: 'text',
      value,
      required: true,
      label: '账户名称',
    }))
  }

  const validateDynamicField = (field: ChannelAccountField, value: string) => {
    if (!channelAccountFieldVisible(field, acct.config)) {
      setFieldError(field.name, null)
      return
    }
    const error = validateScalarField(field, value)
    setFieldError(field.name, error)
    if (!error && field.kind === 'money') {
      const normalized = normalizeMoneyValue(value)
      if (normalized !== null && normalized !== value) setField(field.name, normalized)
    }
  }

  const fillMatchedEndpoints = (source: ChannelAccountField, candidates: ClipboardCandidate[]) => {
    const group = source.clipboard?.group
    if (!group) return false
    const nextConfig = { ...acct.config }
    const filled: string[] = []
    const targets = fields.filter((field) => (
      field.kind === 'endpoint'
      && field.clipboard?.group === group
      && channelAccountFieldVisible(field, nextConfig)
    ))
    for (const target of targets) {
      if (String(nextConfig[target.name] ?? '').trim()) continue
      const candidate = candidates.find((item) => item.role === target.clipboard?.role)
      if (!candidate) continue
      nextConfig[target.name] = candidate.value
      filled.push(target.name)
    }
    if (filled.length === 0) {
      toast('没有可自动填入的空地址字段')
      return false
    }
    setAcct({ config: nextConfig })
    setErrors((current) => Object.fromEntries(Object.entries(current).filter(([name]) => !filled.includes(name))))
    return true
  }

  const scalarOrder = () => [
    'name',
    ...fields
      .filter((field) => channelAccountFieldVisible(field, acct.config) && field.kind !== 'boolean' && field.kind !== 'select')
      .map((field) => field.name),
  ]

  const moveFieldFocus = (name: string, direction: 1 | -1) => {
    const order = scalarOrder()
    const next = order[order.indexOf(name) + direction]
    if (next) fieldRefs.current[next]?.focus()
  }

  const validateAndContinue = async () => {
    if (!descriptor) {
      toast('渠道描述尚未加载，请稍后重试')
      return
    }

    const nextErrors: Record<string, string> = {}
    const nameError = connectionValueError({
      kind: 'text', value: acct.name, required: true, label: '账户名称',
    })
    if (nameError) nextErrors.name = nameError

    for (const field of fields) {
      if (!channelAccountFieldVisible(field, acct.config)) continue
      const value = String(acct.config[field.name] ?? field.default ?? '')
      if (field.kind === 'boolean') {
        if (field.required && typeof acct.config[field.name] !== 'boolean') nextErrors[field.name] = `请选择${field.label}`
        continue
      }
      if (field.kind === 'select') {
        const options = field.options ?? []
        if ((field.required && !value.trim()) || (value && !options.some((option) => option.value === value))) {
          nextErrors[field.name] = `请选择${field.label}`
        }
        continue
      }
      const error = validateScalarField(field, value)
      if (error) {
        nextErrors[field.name] = error
      }
    }

    setErrors(nextErrors)
    const first = scalarOrder().find((name) => nextErrors[name])
    if (Object.keys(nextErrors).length > 0) {
      if (first) {
        fieldRefs.current[first]?.focus({ preventScroll: false })
        fieldRefs.current[first]?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      }
      return
    }
    navigate('/setup/acct/portfolio')
  }

  const renderFieldControl = (field: ChannelAccountField) => {
    const value = acct.config[field.name] ?? field.default ?? ''
    if (field.kind === 'boolean') {
      return (
        <>
          <div className="mb-2 text-[14px] text-ink-2">{field.label}</div>
          <Segmented
            value={value === true || value === 'true' ? 'true' : 'false'}
            options={[
              { value: 'false', label: '关闭' },
              { value: 'true', label: '启用' },
            ]}
            onChange={(next) => setField(field.name, next === 'true')}
          />
          {field.help && <div className="mt-1 text-[13px] text-ink-3">{field.help}</div>}
          {errors[field.name] && <div className="mt-1 text-[13px] text-warn">{errors[field.name]}</div>}
        </>
      )
    }
    if (field.kind === 'select') {
      return (
        <>
          <div className="mb-2 text-[14px] text-ink-2">{field.label}</div>
          <Segmented<string>
            value={String(value)}
            onChange={(next) => setField(field.name, next)}
            options={field.options ?? []}
          />
          {field.help && <div className="mt-1 text-[13px] text-ink-3">{field.help}</div>}
          {errors[field.name] && <div className="mt-1 text-[13px] text-warn">{errors[field.name]}</div>}
        </>
      )
    }
    return (
      <ConnectionField
        ref={(node) => { fieldRefs.current[field.name] = node }}
        label={field.label}
        kind={field.kind}
        clipboard={field.clipboard}
        constraints={field.constraints}
        onPasteBatchMatch={field.clipboard?.group
          ? (candidates) => fillMatchedEndpoints(field, candidates)
          : undefined}
        value={String(value)}
        required={field.required}
        placeholder={field.placeholder}
        help={field.help}
        error={errors[field.name]}
        onChange={(next) => setField(field.name, next === '' ? undefined : next)}
        onBlur={(fieldValue) => validateDynamicField(field, fieldValue)}
        onNavigate={(direction) => moveFieldFocus(field.name, direction)}
        onBrowse={field.kind === 'directory' ? () => setDirectoryPickerField(field.name) : undefined}
      />
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <WizardPage
          kicker="账户设置 · 2 / 6"
          title={`连接 ${descriptor?.label ?? acct.channel}`}
          lead={descriptor?.ui.account_connect_lead || '填好凭据即可；连通性在创建账户时由后端用真实凭据校验。'}
        >
          <div className="max-w-[720px]">
            {descriptor?.account_form.notices.map((notice, index) => (
              <div
                key={`${notice.tone}-${index}`}
                className={`mt-4 rounded-xl border px-4 py-3.5 text-[15px] ${
                  notice.tone === 'warning' ? 'border-warn/30 bg-warn-soft text-warn' : 'border-line bg-fill text-ink-2'
                }`}
              >
                {notice.text}
              </div>
            ))}
            <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <ConnectionField
                  key={`account-name-${acct.channel}`}
                  ref={(node) => { fieldRefs.current.name = node }}
                  label="账户名称"
                  kind="text"
                  value={acct.name}
                  required
                  placeholder="给这个账户起个名字"
                  error={errors.name}
                  onChange={(value) => {
                    setAcct({ name: value })
                    setErrors((current) => {
                      if (!current.name) return current
                      const next = { ...current }
                      delete next.name
                      return next
                    })
                  }}
                  onBlur={validateAccountName}
                  onNavigate={(direction) => moveFieldFocus('name', direction)}
                />
              </div>
              {fields.map((field) => {
              if (isConditionalRevealField(fields, field)) return null
              const value = acct.config[field.name] ?? field.default ?? ''
              const visible = channelAccountFieldVisible(field, acct.config)
              return (
                <div
                  key={`${acct.channel}:${field.name}`}
                  inert={!visible}
                  className={`${field.width === 'full' ? 'sm:col-span-2' : ''} grid transition-[grid-template-rows] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${
                    visible ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
                  }`}
                >
                  <div className="min-h-0 overflow-hidden">
                    {field.kind === 'select' && field.presentation === 'conditional_reveal' ? (
                      <ConditionalReveal
                        label={field.label}
                        help={field.help}
                        value={String(value)}
                        options={field.options ?? []}
                        error={errors[field.name]}
                        onChange={(next) => setField(field.name, next)}
                        renderPanel={(optionValue) => conditionalRevealFields(fields, field.name, optionValue).map(
                          (dependentField) => <div key={dependentField.name}>{renderFieldControl(dependentField)}</div>,
                        )}
                      />
                    ) : renderFieldControl(field)}
                  </div>
                </div>
              )
              })}
            </div>
          </div>
        </WizardPage>
      </div>
      <WizardNav
        prevTo="/setup/acct/channel"
        onNext={validateAndContinue}
      />
      <DirectoryPicker
        open={directoryPickerField !== null}
        initialPath={String(directoryPickerField ? acct.config[directoryPickerField] ?? '' : '')}
        onClose={() => setDirectoryPickerField(null)}
        onSelect={(path) => {
          if (!directoryPickerField) return
          setField(directoryPickerField, path)
          setFieldError(directoryPickerField, null)
        }}
      />
    </div>
  )
}

/* -------- 3 绑定组合 -------- */
export function AcctPortfolio() {
  const { acct, setAcct } = useWizardStore()
  const portfolios = useDomainStore((s) => s.portfolios)
  const list = portfolios ?? []
  const [createOpen, setCreateOpen] = useState(false)
  const [preview, setPreview] = useState<PortfolioLite | null>(null)
  const error = useDomainStore((s) => s.portfoliosError)
  const refresh = useDomainStore((s) => s.refreshPortfolios)

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <WizardPage kicker="账户设置 · 3 / 6" title="用哪个组合？" lead="选择账户跟随的组合，可查看目标持仓和策略代码。">
          <ErrorNotice title="组合列表加载失败" error={error} onRetry={refresh} />
          <div className="grid grid-cols-1 gap-3">
            {portfolios == null && !error && <p className="text-[15px] text-ink-2">加载组合…</p>}
            {portfolios?.length === 0 && <p className="text-[15px] text-ink-2">还没有组合，可以先新建一个。</p>}
            {list.map((p) => (
              <PortfolioChoiceCard key={p.id} portfolio={p} selected={acct.portfolioId === p.id && p.id != null}
                onSelect={() => setAcct({ portfolioId: p.id })} onPreview={() => setPreview(p)} />
            ))}
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className="flex w-full cursor-pointer items-center gap-4 rounded-[14px] border border-dashed border-line p-[18px] text-left text-ink-2 hover:border-ink-3/30"
            >
              <span className="grid h-11 w-11 flex-none place-items-center rounded-xl bg-fill text-[23px]">＋</span>
              <span>
                <span className="block text-[17px] font-[620]">新建组合</span>
                <span className="mt-0.5 block text-[14px] text-ink-2">定名称和市场，代码之后在编辑器里写</span>
              </span>
            </button>
          </div>
        </WizardPage>
      </div>
      <WizardNav prevTo="/setup/acct/connect" nextTo="/setup/acct/trade" nextDisabled={!list.some((p) => p.id != null && p.id === acct.portfolioId)} />
      {preview && <PortfolioCodePreview portfolio={preview} selected={acct.portfolioId === preview.id && preview.id != null}
        onClose={() => setPreview(null)} onSelect={() => { setAcct({ portfolioId: preview.id }); setPreview(null) }} />}
      <CreatePortfolioModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => {
          setCreateOpen(false)
          if (created.id != null) setAcct({ portfolioId: created.id })
        }}
      />
    </div>
  )
}

/* -------- 4 交易方式 -------- */
export function AcctTrade() {
  const { acct, setAcct } = useWizardStore()
  const descriptor = useChannelDescriptor(acct.channel)
  const leverageLimits = descriptor?.leverage
  const showShortLeverage = descriptor?.ui.show_short_leverage ?? true
  const timeoutErr = executionTimeoutError(acct.executionTimeout)
  const longLeverageErr = leverageError(acct.longLeverage, leverageLimits)
  const shortLeverageErr = showShortLeverage ? leverageError(acct.shortLeverage, leverageLimits) : null
  const leverageErr = longLeverageErr ?? shortLeverageErr
  const [algorithmError, setAlgorithmError] = useState<string | null>(null)

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <WizardPage kicker="账户设置 · 4 / 6" title="怎么交易？" lead="选择执行算法，按需调整参数。">
          <label className="mb-2 block text-[14px] text-ink-2">交易方式</label>
          <AlgorithmEditor
            onValidationError={setAlgorithmError}
            slot="trade"
            channel={acct.channel}
            value={acct.algorithm}
            onChange={(v) => setAcct({ algorithm: v ?? descriptor?.defaults.trade_algorithm ?? acct.algorithm })}
          />

          <div className="mt-6 max-w-[560px]">
            <div className="grid grid-cols-1 gap-3 border-t border-line py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start sm:gap-6">
              <div className="min-w-0">
                <label htmlFor="account-execution-timeout" className="text-[16px] text-ink-1">
                  执行超时
                </label>
                <div
                  id="account-execution-timeout-help"
                  className={`mt-0.5 text-[13px] ${timeoutErr ? 'text-warn' : 'text-ink-3'}`}
                >
                  {timeoutErr ?? '限制一次执行最多运行多久；超时后停止开新单。'}
                </div>
              </div>
              <ExecutionTimeoutInput
                id="account-execution-timeout"
                describedBy="account-execution-timeout-help"
                invalid={Boolean(timeoutErr)}
                value={acct.executionTimeout}
                onChange={(executionTimeout) => setAcct({ executionTimeout })}
              />
            </div>
          </div>

          <div className="mt-6 max-w-[560px]">
            <label className="mb-1.5 block text-[14px] text-ink-2">
              {descriptor?.ui.leverage_title ?? '杠杆'}{' '}
              <span className="text-ink-3">（{descriptor?.ui.leverage_note ?? '多空可分设'}）</span>
            </label>
            <div className="grid grid-cols-1 gap-3 border-t border-line py-3 text-[16px] sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start sm:gap-6">
              <div>
                <label htmlFor="account-long-leverage">{descriptor?.ui.long_leverage_label ?? '做多杠杆'}</label>
                <div
                  id="account-long-leverage-help"
                  className={`mt-0.5 text-[13px] ${longLeverageErr ? 'text-warn' : 'text-ink-3'}`}
                >
                  {longLeverageErr ?? `${leverageLimits?.min ?? 0}–${leverageLimits?.max ?? '不限'}，步进 ${leverageLimits?.step ?? 0.1}`}
                </div>
              </div>
              <LeverageInput
                id="account-long-leverage"
                describedBy="account-long-leverage-help"
                invalid={Boolean(longLeverageErr)}
                limits={leverageLimits}
                value={acct.longLeverage}
                onChange={(longLeverage) => setAcct({ longLeverage })}
              />
            </div>
            {showShortLeverage && (
              <div className="grid grid-cols-1 gap-3 border-t border-line py-3 text-[16px] sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start sm:gap-6">
                <div>
                  <label htmlFor="account-short-leverage">{descriptor?.ui.short_leverage_label ?? '做空杠杆'}</label>
                  <div
                    id="account-short-leverage-help"
                    className={`mt-0.5 text-[13px] ${shortLeverageErr ? 'text-warn' : 'text-ink-3'}`}
                  >
                    {shortLeverageErr ?? '0 = 不做空'}
                  </div>
                </div>
                <LeverageInput
                  id="account-short-leverage"
                  describedBy="account-short-leverage-help"
                  invalid={Boolean(shortLeverageErr)}
                  limits={leverageLimits}
                  value={acct.shortLeverage}
                  onChange={(shortLeverage) => setAcct({ shortLeverage })}
                />
              </div>
            )}
          </div>
        </WizardPage>
      </div>
      <WizardNav
        prevTo="/setup/acct/portfolio"
        nextTo="/setup/acct/timer"
        nextDisabled={Boolean(timeoutErr || leverageErr || algorithmError)}
      />
    </div>
  )
}

/* -------- 5 定时 -------- */
export function AcctTimer() {
  const { acct, setAcct } = useWizardStore()
  const descriptor = useChannelDescriptor(acct.channel)
  const scheduleKind = descriptor?.schedule.kind

  const timerValue: TimerEditorState = {
    autoOn: acct.autoOn,
    presetIds: acct.presetIds,
    nightOn: acct.nightOn,
    supN: acct.supN,
    supM: acct.supM,
    rawCron: acct.rawCron,
    timerTab: acct.timerTab,
    scheduleRules: acct.scheduleRules,
    selectedRuleId: acct.selectedRuleId,
  }

  const heading = (
    <WizardHeading
      kicker="账户设置 · 5 / 6"
      title="什么时候自动执行？"
      lead="开启后 axile 按下面的节奏自动调仓。时间均为北京时间。"
    />
  )

  // 与账户编辑定时页同一套 page 布局：标题进左列，预览从标题通高到导航条。
  // 宽视口撑高度链、两列各自滚；窄视口退回单列，本层滚动、底栏钉住。
  return (
    <div className="flex h-full min-h-0 flex-col min-[1120px]:overflow-hidden">
      <div className="min-h-0 flex-1 overflow-y-auto min-[1120px]:overflow-hidden">
        <div className="mx-auto h-full max-w-[1728px] px-5 pt-8 pb-6 sm:px-12 min-[1120px]:flex min-[1120px]:min-h-0 min-[1120px]:flex-col min-[1120px]:pb-4">
          {scheduleKind ? (
            <div className="min-[1120px]:min-h-0 min-[1120px]:flex-1">
              <TimerEditor
                tradeChannel={acct.channel}
                scheduleKind={scheduleKind}
                nightSchedule={descriptor?.schedule.night}
                value={timerValue}
                layout="page"
                leading={heading}
                onChange={(next) => {
                  const v = typeof next === 'function' ? next(timerValue) : next
                  setAcct({
                    autoOn: v.autoOn,
                    presetIds: v.presetIds,
                    nightOn: v.nightOn,
                    supN: v.supN,
                    supM: v.supM,
                    rawCron: v.rawCron,
                    timerTab: v.timerTab,
                    scheduleRules: v.scheduleRules,
                    selectedRuleId: v.selectedRuleId,
                  })
                }}
              />
            </div>
          ) : (
            <>
              {heading}
              <p className="mt-[18px] text-[15px] text-ink-2">渠道能力加载中…</p>
            </>
          )}
        </div>
      </div>
      <WizardNav prevTo="/setup/acct/trade" nextTo="/setup/acct/confirm" nextDisabled={!scheduleKind} />
    </div>
  )
}

export function AcctConfirm() {
  const { acct, resetAcct } = useWizardStore()
  const navigate = useNavigate()
  const toast = useToastStore((s) => s.toast)
  const refreshAccounts = useDomainStore((s) => s.refreshAccounts)
  const descriptor = useChannelDescriptor(acct.channel)
  const scheduleKind = descriptor?.schedule.kind
  const showShortLeverage = descriptor?.ui.show_short_leverage ?? true
  const [previewState, setPreviewState] = useState<
    { status: 'idle' | 'loading' } | { status: 'success'; rows: [string, number][] } | { status: 'error'; message: string }
  >({ status: 'idle' })
  const [createError, setCreateError] = useState<Error | null>(null)

  const cronList = scheduleKind ? resolveCronList(scheduleKind, { ...acct, supN: 0 }, descriptor?.schedule.night) : []
  const cronExpr = cronToExpr(cronList)
  const scheduleDescription = scheduleKind
    ? describeCron(scheduleKind, cronExpr, descriptor?.schedule.night)
    : null
  const scheduleText = !acct.autoOn
    ? '已关闭'
    : scheduleDescription ?? '自定义执行节奏'
  const scheduleFires = acct.autoOn && !scheduleDescription ? nextFires(cronList, 3) : []

  const intentText = algorithmSummary(acct.algorithm)
  const levText = showShortLeverage
    ? `做多 ${acct.longLeverage}x / 做空 ${acct.shortLeverage}x`
    : `${descriptor?.ui.long_leverage_label ?? '做多杠杆'} ${acct.longLeverage}`

  const preview = async () => {
    if (acct.portfolioId == null) {
      toast('请先绑定组合再试跑')
      return
    }
    try {
      setPreviewState({ status: 'loading' })
      const w = (await refreshPortfolioTargetSnapshot(acct.portfolioId)).weights
      setPreviewState({ status: 'success', rows: Object.entries(w).filter(([, v]) => Math.abs(v) > 1e-9).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])) })
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setPreviewState({ status: 'error', message })
    }
  }

  const submit = async () => {
    setCreateError(null)
    try {
      if (!descriptor) {
        toast('渠道描述尚未加载，请稍后重试')
        return
      }
      if (!cronExpr) {
        toast('定时规则尚未就绪，请返回上一步检查')
        return
      }
      const algo = acct.algorithm
      const paramErr = validateAlgorithmRef(algo)
      if (paramErr) {
        toast(`算法参数非法：${paramErr}`)
        return
      }
      const timeoutErr = executionTimeoutError(acct.executionTimeout)
      if (timeoutErr) {
        toast(`执行超时有误：${timeoutErr}`)
        return
      }
      const longLeverageErr = leverageError(acct.longLeverage, descriptor?.leverage)
      const shortLeverageErr = showShortLeverage ? leverageError(acct.shortLeverage, descriptor?.leverage) : null
      if (longLeverageErr || shortLeverageErr) {
        toast(`杠杆有误：${longLeverageErr ?? shortLeverageErr}`)
        return
      }
      const account = await createAccount({
        name: acct.name,
        market: descriptor.portfolio.market_label,
        trade_channel: acct.channel,
        account_control_preset: descriptor.defaults.account_control_preset,
        account_control_override: null,
        account_config: visibleChannelAccountConfig(accountFields(descriptor), acct.config),
        is_started: acct.autoOn,
        cron_expr: cronExpr,
        supplement: acct.supN > 0 ? { count: acct.supN, interval_minutes: acct.supM } : null,
        remark: null,
        brokerage: acct.channel,
        weight_precision: 0.01,
        long_leverage: Number(acct.longLeverage),
        short_leverage: showShortLeverage ? Number(acct.shortLeverage) : 0,
        algorithm: algo,
        empty_positions_algorithm: descriptor.defaults.empty_positions_algorithm,
        trade_rules: null,
        forbidden_symbols: null,
        risk_symbols: null,
        feishu_key: null,
        portfolio_id: acct.portfolioId,
        write_empty_record: null,
        execution_timeout: Number(acct.executionTimeout),
      })
      toast(`账户「${account.name}」已创建`)
      resetAcct()
      void refreshAccounts()
      navigate(account.id != null ? `/accounts/${account.id}` : '/')
    } catch (e) {
      setCreateError(e instanceof Error ? e : new Error(String(e)))
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <WizardPage kicker="账户设置 · 6 / 6" title="确认无误就开跑">
          <div className="rounded-2xl bg-ok-soft px-7 py-6 text-[20px] leading-loose">
            这个账户 <b className="font-[680]">{acct.name || '（未命名）'}</b> 在 <b className="font-[680]">{descriptor?.label ?? acct.channel}</b> 上，
            绑定组合 <b className="font-[680]">#{acct.portfolioId ?? '—'}</b>，用 <b className="font-[680]">{intentText}</b> 的方式，
            {acct.autoOn ? '自动' : '手动'}调仓，杠杆 <b className="font-[680]">{levText}</b>。
          </div>
          <div className="mt-2 text-xs text-ink-3">
            自动执行：{scheduleText} · 执行超时 {acct.executionTimeout} 秒
          </div>
          {scheduleFires.length > 0 && (
            <div className="mt-1 text-xs text-ink-3">
              接下来：{scheduleFires.map(fmtFire).join(' · ')}
            </div>
          )}

          <div className="mt-5">
            <button disabled={previewState.status === 'loading'} className="cursor-pointer rounded-[11px] border-0 bg-ink-1 px-[22px] py-2.5 text-[15px] font-[550] text-surface disabled:cursor-not-allowed disabled:opacity-45" onClick={preview}>
              {previewState.status === 'loading' ? '计算中…' : '▶ 计算目标'}
            </button>
            <div className="mt-[18px] max-w-[460px] rounded-[14px] border border-dashed border-line bg-surface p-5">
              {previewState.status === 'idle' && <div className="py-5 text-center text-[15px] text-ink-3">试跑看这套组合此刻会调成什么样</div>}
              {previewState.status === 'loading' && <div aria-busy="true"><Skeleton className="h-4 w-full" /><Skeleton className="mt-3 h-4 w-4/5" /><Skeleton className="mt-3 h-4 w-3/5" /></div>}
              <ErrorNotice title="试跑失败" error={previewState.status === 'error' ? previewState.message : null} onRetry={preview} />
              {previewState.status === 'success' && previewState.rows.length === 0 && <div className="py-5 text-center text-[15px] text-ink-3">无目标持仓。</div>}
              {previewState.status === 'success' && <WeightBars weights={previewState.rows} />}
            </div>
          </div>
          <ErrorNotice title="创建账户失败" error={createError} variant="mutation" onRetry={submit} />
        </WizardPage>
      </div>
      <WizardNav prevTo="/setup/acct/timer" nextLabel="创建并开跑" onNext={submit} nextDisabled={acct.portfolioId == null || !acct.name.trim()} />
    </div>
  )
}
