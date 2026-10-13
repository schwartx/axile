/** 账户流控结构化编辑页。界面只展示中文业务语义，内部键仅用于数据索引。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FocusEvent as ReactFocusEvent } from 'react'
import { useParams } from 'react-router'
import { Pencil } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Segmented } from '@/components/ui/Segmented'
import { Select } from '@/components/ui/Select'
import { StepperNumberInput } from '@/components/ui/StepperNumberInput'
import {
  EditError,
  EditLoading,
  EditSaveBar,
  EditSynopsis,
  Section,
} from '@/features/account/editUi'
import {
  countAccountControlOverrides,
  countOperationOverrides,
  normalizedAccountControlOverride,
  normalizedOperationOverride,
  resolveAccountControlPolicy,
  resolveAccountControlRule,
  resolveAccountControlScope,
  sameAccountControlOverride,
} from '@/features/account/accountControlPolicy'
import { ControlScopeUsage, ControlSymbolUsage, ControlUsageNotice } from '@/features/account/ControlUsage'
import { useLiveControl, type ControlFrame } from '@/stores/liveControl'
import { AccountPageTitle } from '@/features/account/pageHead'
import { getAccount, getAccountControlPolicy, updateAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import type {
  AccountControlOperationMeta,
  AccountControlOperationOverride,
  AccountControlOperationPolicy,
  AccountControlOverride,
  AccountControlPolicyEditorModel,
  AccountControlRule,
  AccountControlRuleOverride,
  AccountControlScope,
  AccountControlScopeOverride,
  AccountControlTrigger,
} from '@/types/api'

type RuleKey = 'per_minute' | 'per_day' | 'min_interval_ms'
type ScopeKey = 'account' | 'symbol'

const RULES: { key: RuleKey; label: string; unit: string }[] = [
  { key: 'per_minute', label: '每分钟额度', unit: '次' },
  { key: 'per_day', label: '每日额度', unit: '次' },
  { key: 'min_interval_ms', label: '最小间隔', unit: '毫秒' },
]

/** 规则无值时开始编辑的起始草稿：取常见档位，避免从 1 起步。 */
const RULE_DRAFT_DEFAULT: Record<RuleKey, number> = {
  per_minute: 10,
  per_day: 100,
  min_interval_ms: 300,
}

/** 各规则的步进档位：按值域数量级分档（±1 对每日额度是酷刑）。 */
const RULE_STEP: Record<RuleKey, number> = {
  per_minute: 1,
  per_day: 10,
  min_interval_ms: 100,
}

/**
 * 排队优先四档：与后端 priority 实际使用的值一一对应。
 * 语义是「额度不足需要排队时谁先走」，不是连续谱。
 */
const PRIORITY_TIERS = [
  { key: 'fastest', value: 0, label: '最优先' },
  { key: 'high', value: 10, label: '高' },
  { key: 'mid', value: 20, label: '中' },
  { key: 'normal', value: 100, label: '普通' },
] as const
type PriorityTierKey = (typeof PRIORITY_TIERS)[number]['key']

/** 把任意 priority 吸附到最近档位（并列时取更优先的一档）；仅用于显示，不改写原值。 */
type PriorityTier = (typeof PRIORITY_TIERS)[number]
function priorityTierOf(priority: number): PriorityTier {
  let best: PriorityTier = PRIORITY_TIERS[0]
  for (const tier of PRIORITY_TIERS) {
    if (Math.abs(tier.value - priority) < Math.abs(best.value - priority)) best = tier
  }
  return best
}

const COMMON_KEYS = ['place_order', 'cancel_order', 'query_order']

function cloneOverride(value: AccountControlOverride | null): AccountControlOverride {
  return value
    ? structuredClone(value)
    : { operations: {}, groups: {} }
}

function ruleText(rule: AccountControlRule | null | undefined, key: RuleKey): string {
  if (!rule) return key === 'per_day' ? '不限每日' : '不限制'
  if (key === 'per_minute') return `${rule.limit} 次/分钟`
  if (key === 'per_day') return `${rule.limit} 次/日`
  return `间隔 ${rule.limit} 毫秒`
}

function scopeSummary(scope: AccountControlScope | null | undefined): string {
  return RULES.map(({ key }) => ruleText(scope?.[key], key)).join(' · ')
}

function ruleError(key: RuleKey, rule: AccountControlRuleOverride | null | undefined): string | null {
  if (!rule || rule.unlimited) return null
  if (!Number.isInteger(rule.limit) || (rule.limit ?? -1) < 0) return '请输入不小于 0 的整数'
  if (key === 'min_interval_ms' && rule.limit === 0) return '最小间隔必须大于 0'
  return null
}

/** 「已改」行内标记：默认值提示 + 恢复。只在偏离时出现，正常态不加任何标签。 */
function ChangedMark({
  defaultText,
  onRestore,
  restoreLabel,
}: {
  defaultText: string
  onRestore: () => void
  restoreLabel: string
}) {
  return (
    <span className="flex items-baseline gap-2 text-[12px]">
      <span className="text-accent">已改</span>
      <span className="text-ink-3">默认 {defaultText}</span>
      <button
        type="button"
        className="cursor-pointer border-0 bg-transparent p-0 text-[12px] font-semibold text-accent"
        onClick={onRestore}
        aria-label={restoreLabel}
      >
        恢复
      </button>
    </span>
  )
}

/**
 * 可编辑行的公共骨架：标签是钉死的锚（不参与过渡）；值槽内查看/编辑两层
 * 常挂叠放（grid 同格），切换只做 opacity 双向交叉——无空窗、无挂载跳变。
 * 隐藏层 inert + aria-hidden，焦点与读屏不误入；行高恒定 min-h-10。
 */
const ROW_BASE = 'group -mx-2 flex min-h-10 items-center gap-x-2.5 rounded-[8px] px-2 py-1.5 transition-colors'
const VALUE_BUTTON = 'flex cursor-pointer items-center gap-x-2.5 border-0 bg-transparent p-0 text-left'
const PENCIL = 'h-3 w-3 text-ink-3 transition-colors group-hover:text-accent'
const CELL = 'col-start-1 row-start-1 flex min-w-0 items-center gap-x-2.5 transition-opacity duration-200 motion-reduce:transition-none'
const CELL_HIDDEN = 'pointer-events-none opacity-0'

/** 达到额度后的行为：两选项平铺（分段控件）。 */
const TRIGGER_CHOICES: { value: AccountControlTrigger; label: string }[] = [
  { value: 'wait', label: '排队' },
  { value: 'block', label: '阻断' },
]

function triggerText(trigger: AccountControlTrigger | undefined): string {
  return trigger === 'block' ? '超了直接阻断' : '超了排队等待'
}

function ControlRuleField({
  ruleKey,
  preset,
  custom,
  onChange,
}: {
  ruleKey: RuleKey
  preset: AccountControlRule | null | undefined
  custom: AccountControlRuleOverride | null | undefined
  onChange: (next: AccountControlRuleOverride | null) => void
}) {
  const meta = RULES.find((item) => item.key === ruleKey)!
  const active = Boolean(custom)
  const effective = resolveAccountControlRule(preset, custom)
  /** min_interval_ms 是渠道硬保护，不允许显式不限制 */
  const allowUnlimited = ruleKey !== 'min_interval_ms'
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [trigger, setTrigger] = useState<AccountControlTrigger>('wait')
  const [unlimited, setUnlimited] = useState(false)

  const inputRef = useRef<HTMLInputElement>(null)
  const editCellRef = useRef<HTMLDivElement>(null)
  const viewButtonRef = useRef<HTMLButtonElement>(null)
  const wasEditing = useRef(false)
  const exitRefocus = useRef(false)

  // 焦点随层切换：进编辑聚焦输入框（不预选数字，光标落在末尾）；「不限」模式下输入框
  // 不渲染，焦点落在编辑层容器上（保住 Esc 与失焦提交）。键盘退出（Enter/Esc）焦点还给
  // 值按钮，失焦退出（点了别处）不抢焦点。
  useEffect(() => {
    if (editing) {
      if (unlimited) editCellRef.current?.focus()
      else inputRef.current?.focus()
    } else if (wasEditing.current && exitRefocus.current) {
      viewButtonRef.current?.focus()
    }
    wasEditing.current = editing
  }, [editing, unlimited])

  const min = ruleKey === 'min_interval_ms' ? 1 : 0
  const parsed = Number(draft)
  const draftValid = draft.trim() !== '' && Number.isInteger(parsed) && parsed >= min
  const draftError = editing && !unlimited && !draftValid ? `请输入不小于 ${min} 的整数` : null
  const zeroWarning = editing && !unlimited && draftValid && ruleKey !== 'min_interval_ms' && parsed === 0

  const begin = () => {
    setUnlimited(allowUnlimited && Boolean(custom?.unlimited))
    setDraft(String(effective?.limit ?? RULE_DRAFT_DEFAULT[ruleKey]))
    setTrigger(effective?.on_trigger ?? 'wait')
    setEditing(true)
  }
  /** 提交即落覆盖：用户语义里「改没改」由恢复表达，不做等值归净。 */
  const commit = (refocus = false) => {
    if (draftError) return
    exitRefocus.current = refocus
    onChange(unlimited && allowUnlimited ? { unlimited: true } : { limit: parsed, on_trigger: trigger })
    setEditing(false)
  }
  const cancel = (refocus = false) => {
    exitRefocus.current = refocus
    setEditing(false)
  }
  /** 焦点离开编辑层：合法则提交，非法则放弃（不写脏数据）。 */
  const blurCommit = (event: ReactFocusEvent<HTMLDivElement>) => {
    if (event.currentTarget.contains(event.relatedTarget)) return
    if (draftError) cancel()
    else commit()
  }
  const valueText = effective ? `${effective.limit} ${meta.unit}` : '不限制'
  const behaviorText = effective ? `· ${triggerText(effective.on_trigger)}` : null

  return (
    <div className="border-t border-line/70 first:border-t-0">
      <div className={`${ROW_BASE} ${editing ? '' : 'hover:bg-fill'}`}>
        <span className="w-24 flex-none text-[13px] text-ink-2">{meta.label}</span>
        <div className="grid min-w-0 flex-1">
          {/* 查看层 */}
          <div className={`${CELL} ${editing ? CELL_HIDDEN : 'opacity-100'}`} inert={editing} aria-hidden={editing}>
            <button ref={viewButtonRef} type="button" className={VALUE_BUTTON} onClick={begin} aria-label={`调整${meta.label}`}>
              <span className="num text-[14px] text-ink-1 transition-colors group-hover:text-accent">{valueText}</span>
              {behaviorText && <span className="text-[12px] text-ink-3">{behaviorText}</span>}
              <Pencil className={PENCIL} aria-hidden />
            </button>
            {active && (
              <ChangedMark
                defaultText={preset ? `${preset.limit} ${meta.unit}` : '不限制'}
                onRestore={() => onChange(null)}
                restoreLabel={`恢复${meta.label}默认值`}
              />
            )}
          </div>
          {/* 编辑层 */}
          <div
            ref={editCellRef}
            tabIndex={-1}
            className={`${CELL} outline-none ${editing ? 'opacity-100' : CELL_HIDDEN}`}
            inert={!editing}
            aria-hidden={!editing}
            onBlur={blurCommit}
            onKeyDown={(event) => {
              if (event.key === 'Escape') cancel(true)
            }}
          >
            {allowUnlimited && unlimited ? (
              <span className="text-[13px] text-ink-3">不限制{meta.label}</span>
            ) : (
              <>
                <StepperNumberInput
                  ref={inputRef}
                  size="sm"
                  ariaLabel={meta.label}
                  invalid={Boolean(draftError)}
                  value={draft}
                  onChange={setDraft}
                  step={RULE_STEP[ruleKey]}
                  min={min}
                  unit={meta.unit}
                  displayValue={draftValid ? parsed : undefined}
                  onEnter={() => commit(true)}
                />
                <span className="text-[13px] text-ink-3">· 超额后</span>
                <Segmented<AccountControlTrigger> size="sm" value={trigger} onChange={setTrigger} options={TRIGGER_CHOICES} />
              </>
            )}
            {/* 「不限」是极少切换的元决策：降为行尾小字切换，不占行首视觉首位。 */}
            {allowUnlimited && (
              <button
                type="button"
                className="ml-auto flex-none cursor-pointer border-0 bg-transparent p-0 text-[11px] text-ink-3 transition-colors hover:text-accent"
                onClick={() => setUnlimited((value) => !value)}
              >
                {unlimited ? '设置限额' : '改为不限'}
              </button>
            )}
          </div>
        </div>
      </div>
      {editing && (draftError || zeroWarning) && (
        <p className="mt-0.5 px-2 text-[12px] text-warn sm:ml-24 sm:px-0">
          {draftError ?? '0 次会阻断所有此类请求。'}
        </p>
      )}
    </div>
  )
}

function priorityError(priority: number | null | undefined): string | null {
  if (priority == null) return null
  return Number.isInteger(priority) ? null : '请输入整数'
}

function ControlPriorityField({
  preset,
  custom,
  onChange,
}: {
  preset: number
  custom: number | null | undefined
  onChange: (next: number | null) => void
}) {
  const active = custom != null
  const effective = custom ?? preset
  const tier = priorityTierOf(effective)
  const [editing, setEditing] = useState(false)
  const editCellRef = useRef<HTMLDivElement>(null)
  const viewButtonRef = useRef<HTMLButtonElement>(null)
  const wasEditing = useRef(false)
  const exitRefocus = useRef(false)

  useEffect(() => {
    if (editing) editCellRef.current?.focus()
    else if (wasEditing.current && exitRefocus.current) viewButtonRef.current?.focus()
    wasEditing.current = editing
  }, [editing])

  const choose = (key: PriorityTierKey) => {
    exitRefocus.current = true
    onChange(PRIORITY_TIERS.find((item) => item.key === key)!.value)
    setEditing(false)
  }
  const dismiss = (refocus = false) => {
    exitRefocus.current = refocus
    setEditing(false)
  }

  return (
    <div className={`${ROW_BASE} ${editing ? '' : 'hover:bg-fill'}`}>
      <span className="w-24 flex-none text-[13px] text-ink-2">排队优先</span>
      <div className="grid min-w-0 flex-1">
        {/* 查看层 */}
        <div className={`${CELL} ${editing ? CELL_HIDDEN : 'opacity-100'}`} inert={editing} aria-hidden={editing}>
          <button ref={viewButtonRef} type="button" className={VALUE_BUTTON} onClick={() => setEditing(true)} aria-label="调整排队优先">
            <span className="text-[14px] text-ink-1 transition-colors group-hover:text-accent">{tier.label}</span>
            <Pencil className={PENCIL} aria-hidden />
          </button>
          {active && (
            <ChangedMark
              defaultText={priorityTierOf(preset).label}
              onRestore={() => onChange(null)}
              restoreLabel="恢复排队优先默认值"
            />
          )}
        </div>
        {/* 编辑层 */}
        <div
          ref={editCellRef}
          tabIndex={-1}
          className={`${CELL} outline-none ${editing ? 'opacity-100' : CELL_HIDDEN}`}
          inert={!editing}
          aria-hidden={!editing}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) dismiss()
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') dismiss(true)
          }}
        >
          <Segmented<PriorityTierKey>
            size="sm"
            value={tier.key}
            onChange={choose}
            options={PRIORITY_TIERS.map((item) => ({ value: item.key, label: item.label }))}
          />
          <span className="text-[12px] text-ink-3">额度不足需要排队时，谁先走</span>
        </div>
      </div>
    </div>
  )
}

function ControlScopeEditor({
  label,
  preset,
  custom,
  onChange,
}: {
  label: string
  preset: AccountControlScope | null | undefined
  custom: AccountControlScopeOverride | null | undefined
  onChange: (next: AccountControlScopeOverride | null) => void
}) {
  const setRule = (key: RuleKey, rule: AccountControlRuleOverride | null) => {
    const next = { ...(custom ?? {}), [key]: rule }
    if (!rule) delete next[key]
    onChange(Object.values(next).some(Boolean) ? next : null)
  }
  return (
    <div className="mt-3">
      <h3 className="text-[14px] font-semibold text-ink-1">{label}</h3>
      <div className="mt-1">
        {RULES.map(({ key }) => (
          <ControlRuleField key={key} ruleKey={key} preset={preset?.[key]} custom={custom?.[key]} onChange={(rule) => setRule(key, rule)} />
        ))}
      </div>
    </div>
  )
}

function ControlOperationRow({
  monitorFrame,
  meta,
  preset,
  effective,
  custom,
  open,
  onToggle,
  onChange,
}: {
  monitorFrame: ControlFrame | undefined
  meta: AccountControlOperationMeta
  preset: AccountControlOperationPolicy
  effective: AccountControlOperationPolicy
  custom: AccountControlOperationOverride | undefined
  open: boolean
  onToggle: () => void
  onChange: (next: AccountControlOperationOverride | null) => void
}) {
  const customCount = countOperationOverrides(custom)
  const setScope = (key: ScopeKey, scope: AccountControlScopeOverride | null) => {
    const next = { ...(custom ?? {}), [key]: scope }
    if (!scope) delete next[key]
    onChange(normalizedOperationOverride(next))
  }
  const setPriority = (priority: number | null) => {
    const next: AccountControlOperationOverride = { ...(custom ?? {}) }
    if (priority == null) delete next.priority
    else next.priority = priority
    onChange(normalizedOperationOverride(next))
  }
  const zoneText = open ? 'text-ink-3' : 'text-ink-2'
  // 悬浮光晕：hover 判定挂在不动的外层（底部 2px 占位 + 负 margin 抵消，布局净零），
  // 位移作用在内层卡片——否则卡片上移逃出光标 → hover 失效回落 → 再 hover，边缘自激抖动。
  return (
    <div className="group/card -mb-0.5 pb-0.5">
    <div className="rounded-[12px] border border-line bg-surface transition-[transform,border-color] duration-150 group-hover/card:-translate-y-0.5 group-hover/card:border-border-strong motion-reduce:transition-none motion-reduce:group-hover/card:translate-y-0">
      <button type="button" className="w-full cursor-pointer border-0 bg-transparent px-4 py-3 text-left" onClick={onToggle} aria-expanded={open}>
        {/* 三区横排撑满卡宽，跨卡片列对齐成「隐形的表」；展开时摘要降级为 ink-3 实时图例，不收起 */}
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
          <span className="w-20 flex-none text-[15px] font-semibold text-ink-1">{meta.display_name}</span>
          <span className={`w-28 flex-none text-[13px] ${zoneText}`}>
            <span className="mr-2 text-ink-3">排队优先</span>{priorityTierOf(effective.priority).label}
          </span>
          <span className={`min-w-0 flex-1 basis-40 text-[13px] ${zoneText}`}>
            <span className="mr-2 text-ink-3">账户合计</span>{scopeSummary(effective.account)}
            <ControlScopeUsage frame={monitorFrame} operation={meta.key} />
          </span>
          <span className={`min-w-0 flex-1 basis-40 text-[13px] ${zoneText}`}>
            <span className="mr-2 text-ink-3">每个品种</span>{scopeSummary(effective.symbol)}
            {effective.symbol && <ControlSymbolUsage frame={monitorFrame} operation={meta.key} />}
          </span>
          <span className="flex flex-none items-baseline gap-2">
            {customCount > 0 && <span className="text-[12px] text-accent">已改 {customCount} 处</span>}
            <span className="text-[13px] text-ink-3" aria-hidden>{open ? '⌄' : '›'}</span>
          </span>
        </div>
      </button>
      <div inert={!open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="min-h-0 overflow-hidden">
          <div className="border-t border-line px-4 pb-3">
            <ControlPriorityField preset={preset.priority} custom={custom?.priority} onChange={setPriority} />
            <div className="grid grid-cols-1 gap-x-10 md:grid-cols-2">
              <ControlScopeEditor label="账户合计" preset={preset.account} custom={custom?.account} onChange={(scope) => setScope('account', scope)} />
              <div>
                <ControlScopeEditor label="每个品种" preset={preset.symbol} custom={custom?.symbol} onChange={(scope) => setScope('symbol', scope)} />
                <ControlSymbolUsage frame={monitorFrame} operation={meta.key} expanded />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    </div>
  )
}

export function AccountEditControlPage() {
  const { id } = useParams()
  const accountId = Number(id)
  const monitorFrame = useLiveControl((state) => state.frames[accountId])
  const toast = useToastStore((state) => state.toast)
  const accounts = useDomainStore((state) => state.accounts)
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const account = usePolling(useCallback((signal: AbortSignal) => getAccount(accountId, signal), [accountId]), {
    queryKey: `account:${accountId}`,
    intervalMs: 0,
  })
  const [model, setModel] = useState<AccountControlPolicyEditorModel | null>(null)
  const [presetKey, setPresetKey] = useState('')
  const [override, setOverride] = useState<AccountControlOverride>({ operations: {}, groups: {} })
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [otherOpen, setOtherOpen] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [previewNote, setPreviewNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [policyError, setPolicyError] = useState<Error | null>(null)
  const [previewError, setPreviewError] = useState<Error | null>(null)
  const [saveError, setSaveError] = useState<Error | null>(null)

  const loadPolicy = useCallback(() => {
    if (!account.data) return
    setPolicyError(null)
    getAccountControlPolicy(accountId)
      .then((value) => {
        setModel(value)
        setPresetKey(value.preset_key)
        setOverride(cloneOverride(value.override))
      })
      .catch((error) => setPolicyError(error instanceof Error ? error : new Error(String(error))))
  }, [account.data, accountId])

  useEffect(() => {
    if (!model) loadPolicy()
  }, [loadPolicy, model])

  /** 取消：放弃草稿（含预设切换预览与展开态），从服务端重新加载已保存的策略。 */
  const cancelEdit = () => {
    setOpenKey(null)
    setOtherOpen(false)
    setPreviewNote('')
    setPreviewError(null)
    setSaveError(null)
    loadPolicy()
  }

  const acc = account.data
  const cachedAccount = accounts?.find((item) => item.account_id === accountId) ?? null
  const title = (
    <div className="flex flex-wrap items-baseline gap-3">
      <AccountPageTitle
        accountId={accountId}
        page="执行流控"
        name={acc?.name ?? cachedAccount?.name}
        channel={acc?.trade_channel ?? cachedAccount?.trade_channel}
        market={acc?.market ?? cachedAccount?.market}
      />
    </div>
  )
  const normalized = normalizedAccountControlOverride(override)
  const effective = useMemo(() => model ? resolveAccountControlPolicy(model.preset_policy, override) : null, [model, override])
  const overrideCount = countAccountControlOverrides(normalized)
  const originalOverride = model?.override ?? null
  const dirtyPreset = Boolean(acc && presetKey !== acc.account_control_preset)
  const dirtyOverride = !sameAccountControlOverride(normalized, originalOverride)
  const dirty = dirtyPreset || dirtyOverride
  const errors = Object.values(override.operations ?? {}).flatMap((operation) => [
    priorityError(operation.priority),
    ...[operation.account, operation.symbol].flatMap((scope) =>
      RULES.map(({ key }) => ruleError(key, scope?.[key])).filter(Boolean),
    ),
  ]).filter(Boolean).concat(
    Object.values(override.groups ?? {}).flatMap((scope) =>
      RULES.map(({ key }) => ruleError(key, scope?.[key])).filter(Boolean),
    ),
  )

  if (account.error && !acc) return <EditError error={account.error} onRetry={account.refresh} />
  if (policyError && !model)
    return (
      <EditError
        error={policyError}
        onRetry={loadPolicy}
      />
    )
  if (!acc || !model || !effective)
    return (
      <section className="pb-24">
        {title}
        <EditLoading bare />
      </section>
    )

  const switchPreset = async (nextKey: string) => {
    if (nextKey === presetKey) return
    const previous = presetKey
    setPresetKey(nextKey)
    setPreviewing(true)
    setPreviewError(null)
    try {
      const preview = await getAccountControlPolicy(accountId, nextKey)
      setModel(preview)
      setPreviewNote(`已按「${preview.preset_display_name}」方案重新计算，你改过的 ${overrideCount} 处设置保持不变。`)
    } catch (error) {
      setPresetKey(previous)
      setPreviewError(error instanceof Error ? error : new Error(String(error)))
    } finally {
      setPreviewing(false)
    }
  }

  const setOperation = (key: string, next: AccountControlOperationOverride | null) => {
    setOverride((current) => {
      const operations = { ...current.operations }
      if (next) operations[key] = next
      else delete operations[key]
      return { ...current, operations }
    })
  }

  const operationRows = model.operations.filter((meta) => effective.operations[meta.key])
  const common = operationRows.filter((meta) => COMMON_KEYS.includes(meta.key) || Boolean(override.operations[meta.key]))
  const other = operationRows.filter((meta) => !common.includes(meta))
  const renderOperation = (meta: AccountControlOperationMeta) => (
    <ControlOperationRow
      key={meta.key}
      monitorFrame={monitorFrame}
      meta={meta}
      preset={model.preset_policy.operations[meta.key] ?? { priority: 100, account: {} }}
      effective={effective.operations[meta.key]}
      custom={override.operations[meta.key]}
      open={openKey === meta.key}
      onToggle={() => setOpenKey((value) => value === meta.key ? null : meta.key)}
      onChange={(next) => setOperation(meta.key, next)}
    />
  )

  const changes = dirty ? [
    ...(dirtyPreset ? [`方案改为「${model.preset_display_name}」`] : []),
    ...(dirtyOverride ? [`流控已改 ${overrideCount} 处`] : []),
  ] : []

  const save = async () => {
    if (errors.length) return toast(`流控设置有误：${errors[0]}`)
    setSaving(true)
    setSaveError(null)
    try {
      await updateAccount(accountId, {
        account_control_preset: presetKey,
        account_control_override: normalized,
      })
      toast('已保存，下次执行生效')
      void refreshAccounts()
      // 保存后留在本页：刷新 dirty 基线（acc 的 preset 与 model.override），
      // 否则底栏不会收敛（此前靠跳页卸载掩盖）。
      account.refresh()
      loadPolicy()
    } catch (error) {
      setSaveError(error instanceof Error ? error : new Error(String(error)))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section>
      {title}
      <EditSynopsis note="控制交易请求的频率与间隔；保存后从下一次执行开始生效。">
        {model.preset_display_name}{overrideCount > 0 ? ` · 已改 ${overrideCount} 处` : ''}
      </EditSynopsis>

      <Section label="预设方案">
        <div className="rounded-[12px] border border-line bg-surface px-4 py-3 md:col-span-2">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <div className="w-24 flex-none text-[13px] text-ink-2">当前方案</div>
            <Select<string>
              ariaLabel="流控预设方案"
              disabled={previewing}
              value={presetKey}
              onChange={(value) => void switchPreset(value)}
              className="w-full justify-between px-3 py-2 text-[15px] sm:w-64"
              options={model.compatible_presets.map((item) => ({ value: item.key, label: item.display_name, hint: item.description }))}
            />
            <span className="text-[13px] text-ink-3">{model.compatible_presets.find((item) => item.key === presetKey)?.description}</span>
          </div>
          <div className="mt-2.5 flex gap-3 text-[13px]"><span className="w-24 flex-none text-ink-2">统计时区</span><span className="text-ink-1">{model.timezone_display_name}</span></div>
          {previewNote && <p className="mt-2 text-[13px] text-ink-2">{previewNote}</p>}
          <ControlUsageNotice frame={monitorFrame} editing={dirty} />
        </div>
      </Section>
      <ErrorNotice title="预设方案预览失败" error={previewError} variant="mutation" />

      <Section label="常用操作"><div className="flex flex-col gap-2 md:col-span-2">{common.map(renderOperation)}</div></Section>

      {other.length > 0 && (
        <div className="mt-4">
          <button type="button" className="flex w-full cursor-pointer items-center gap-3 border-0 bg-transparent py-2 text-left" onClick={() => setOtherOpen((value) => !value)} aria-expanded={otherOpen}>
            <span className="text-[13px] font-semibold text-ink-2">其他操作</span>
            <span className="text-[12px] text-ink-3">{other.length} 项</span><span className="h-px flex-1 bg-line" /><span className="text-[13px] text-ink-3">{otherOpen ? '⌄' : '›'}</span>
          </button>
          <div inert={!otherOpen} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${otherOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><div className="flex flex-col gap-2 pt-1">{other.map(renderOperation)}</div></div></div>
        </div>
      )}

      {model.groups.length > 0 && (
        <Section label="共享限制">
          <div className="flex flex-col gap-2 md:col-span-2">
            {model.groups.map((group) => {
              const key = `group:${group.key}`
              const open = openKey === key
              const custom = override.groups[group.key]
              const preset = model.preset_policy.groups[group.key]
              const current = resolveAccountControlScope(preset, custom)
              return (
                <div key={group.key} className="group/card -mb-0.5 pb-0.5">
                {/* 悬浮光晕：hover 判定在不动的外层，位移在内层卡片（防边缘自激抖动，同操作卡） */}
                <div className="rounded-[12px] border border-line bg-surface transition-[transform,border-color] duration-150 group-hover/card:-translate-y-0.5 group-hover/card:border-border-strong motion-reduce:transition-none motion-reduce:group-hover/card:translate-y-0">
                  <button type="button" className="w-full cursor-pointer border-0 bg-transparent px-4 py-3 text-left" onClick={() => setOpenKey(open ? null : key)} aria-expanded={open}>
                    {/* 与操作卡同家族：标题 | 摘要 | 已改 ⌄ 单行横排；描述挪进展开区 */}
                    <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
                      <span className="flex-none text-[15px] font-semibold text-ink-1">{group.display_name}</span>
                      <span className={`min-w-0 flex-1 basis-40 text-[13px] ${open ? 'text-ink-3' : 'text-ink-2'}`}>{scopeSummary(current)}<ControlScopeUsage frame={monitorFrame} operation={group.key} scope="group" /></span>
                      <span className="flex flex-none items-baseline gap-2">
                        {custom && <span className="text-[12px] text-accent">已改</span>}
                        <span className="text-[13px] text-ink-3" aria-hidden>{open ? '⌄' : '›'}</span>
                      </span>
                    </div>
                  </button>
                  <div inert={!open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><div className="border-t border-line px-4 pb-3"><p className="pt-3 text-[13px] text-ink-3">{group.description}</p><ControlScopeEditor label="共同限制" preset={preset} custom={custom} onChange={(next) => setOverride((currentOverride) => { const groups = { ...currentOverride.groups }; if (next) groups[group.key] = next; else delete groups[group.key]; return { ...currentOverride, groups } })} /></div></div></div>
                </div>
                </div>
              )
            })}
          </div>
        </Section>
      )}

      <EditSaveBar changes={changes} blocked={Boolean(errors.length || previewing)} onCancel={cancelEdit} onSave={() => void save()} saving={saving} error={saveError} />
    </section>
  )
}
