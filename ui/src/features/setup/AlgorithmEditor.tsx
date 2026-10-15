import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useEffect, useLayoutEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { ChoiceGroup } from '@/components/ui/ChoiceGroup'
import { ParameterNumberInput } from '@/components/ui/ParameterNumberInput'
import { Select } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Toggle } from '@/features/account/editUi'
import { useChannelDescriptor } from '@/stores/channels'
import {
  algorithmSchemaDefaults, algorithmSchemaFields, algorithmFieldOptions, algorithmNumericDisplay,
  validateAlgorithmSchemaParams, type AlgorithmSchemaField,
} from '@/features/setup/algorithmSchema'
import { algoLabel, validateAlgorithmParams, type AlgorithmRef } from '@/features/setup/algorithms'
import { algorithmCatalog, availableAlgorithms } from '@/features/setup/algorithmCatalog'
import type { AlgorithmInfo, AlgorithmSlot, TradeChannel } from '@/types/api'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

/** 换 method 时参数区交叉淡半程（总约 140+140ms）。 */
const PARAMS_FADE_MS = 140

/**
 * 换算法时：选择器行外的参数区做短交叉淡 + 高度过渡。
 * 同 method 下改 params 硬更新，不触发淡入（避免拖数字也闪）。
 */
function MethodParamsStage({ method, children }: { method: string; children: ReactNode }) {
  const methodRef = useRef(method)
  const [body, setBody] = useState(children)
  const [opaque, setOpaque] = useState(true)
  const [height, setHeight] = useState<number | undefined>(undefined)
  const innerRef = useRef<HTMLDivElement>(null)
  const heightTimerRef = useRef<number | undefined>(undefined)
  const boot = useRef(true)

  // 同 method：同步最新 children（改参数）。
  useEffect(() => {
    if (method !== methodRef.current) return
    setBody(children)
  }, [children, method])

  // 换 method：先淡出 → 换内容 → 淡入。
  useEffect(() => {
    if (method === methodRef.current) return

    const reduced =
      typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced || boot.current) {
      methodRef.current = method
      setBody(children)
      setOpaque(true)
      setHeight(undefined)
      boot.current = false
      return
    }

    setHeight(innerRef.current?.offsetHeight)
    setOpaque(false)
    const t = window.setTimeout(() => {
      methodRef.current = method
      setBody(children)
      setOpaque(true)
    }, PARAMS_FADE_MS)
    return () => window.clearTimeout(t)
  }, [method, children])

  useEffect(() => {
    boot.current = false
    return () => {
      if (heightTimerRef.current !== undefined) window.clearTimeout(heightTimerRef.current)
    }
  }, [])

  // 只在 method 替换期间量一次新内容高度；结束后回到 auto，让内部条件展开独立走布局流。
  useLayoutEffect(() => {
    const el = innerRef.current
    if (!el || height === undefined) return
    const frame = window.requestAnimationFrame(() => {
      setHeight(el.offsetHeight)
      if (heightTimerRef.current !== undefined) window.clearTimeout(heightTimerRef.current)
      heightTimerRef.current = window.setTimeout(() => setHeight(undefined), 150)
    })
    return () => window.cancelAnimationFrame(frame)
    // body 是 method 替换的唯一触发源；height 后续变化不应重新采样自身动画。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [body])

  return (
    <div
      className={`overflow-hidden transition-[height,opacity] duration-150 ease-[cubic-bezier(0.4,0,0.2,1)] motion-reduce:transition-none`}
      style={{
        height: height != null ? height : undefined,
        opacity: opaque ? 1 : 0,
      }}
    >
      <div ref={innerRef}>{body}</div>
    </div>
  )
}

/** 数字框：藏原生 spinner + 产品 focus，避免系统亮蓝/箭头掉队。 */
const FIELD =
  'w-28 rounded-[9px] border border-ink-3/30 bg-surface px-3 py-1.5 text-right text-[15px] num outline-none transition-[border-color] duration-200 ease-[cubic-bezier(0.4,0,0.2,1)] focus:border-accent motion-reduce:transition-none [-moz-appearance:textfield] [&::-webkit-inner-spin-button]:m-0 [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:m-0 [&::-webkit-outer-spin-button]:appearance-none'
const VALUE_FIELD =
  'w-full max-w-72 rounded-[9px] border border-ink-3/30 bg-surface px-3 py-1.5 text-[15px] outline-none transition-[border-color] duration-200 ease-[cubic-bezier(0.4,0,0.2,1)] focus:border-accent motion-reduce:transition-none'
const ROW = 'flex flex-col gap-2 border-t border-line py-3 sm:flex-row sm:items-center sm:justify-between'
const HINT = 'mt-1 block text-xs leading-relaxed text-ink-3'

/** 数字输入行：读写 params 里的某个数值键，超范围时给出琥珀色提示。 */
function NumRow({
  label,
  hint,
  value,
  onChange,
  min,
  max,
  step,
  suffix,
  exclusiveMin,
}: {
  label: string
  hint?: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  suffix?: string
  exclusiveMin?: boolean
}) {
  const bad = (min !== undefined && (exclusiveMin ? value <= min : value < min)) ||
    (max !== undefined && value > max) || !Number.isFinite(value)
  return (
    <div className={ROW}>
      <span className="text-[15px]">
        {label} {hint && <span className={HINT}>{hint}</span>}
      </span>
      <span className="flex items-center gap-2">
        <input
          className={FIELD}
          aria-label={label}
          type="number"
          value={Number.isFinite(value) ? value : ''}
          min={min}
          max={max}
          step={step ?? 1}
          onChange={(e) => onChange(e.target.value === '' ? Number.NaN : Number(e.target.value))}
        />
        {suffix && <span className="w-6 text-[14px] text-ink-3">{suffix}</span>}
        {bad && <span className="text-[13px] text-warn">超范围</span>}
      </span>
    </div>
  )
}

/** 通用编辑器：自定义算法无专属 UI，直接编辑裸 JSON 参数。 */
function GenericEditor({
  params,
  onChange,
  onValidationError,
}: {
  params: Record<string, unknown>
  onChange: (p: Record<string, unknown>) => void
  onValidationError?: (error: string | null) => void
}) {
  const [text, setText] = useState(() => JSON.stringify(params, null, 2))
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { setText(JSON.stringify(params, null, 2)); setErr(null) }, [params])
  useEffect(() => { onValidationError?.(err) }, [err, onValidationError])
  return (
    <div className="max-w-[620px]">
      <textarea
        className="w-full rounded-[11px] border border-ink-3/30 bg-surface p-3 font-mono text-[14px] leading-relaxed"
        aria-label="算法参数 JSON"
        rows={8}
        spellCheck={false}
        value={text}
        placeholder='{\n  "key": "value"\n}'
        onChange={(e) => {
          const t = e.target.value
          setText(t)
          const trimmed = t.trim()
          if (!trimmed) {
            setErr(null)
            onChange({})
            return
          }
          try {
            const parsed = JSON.parse(trimmed) as unknown
            if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
              setErr('params 必须是 JSON 对象')
              return
            }
            setErr(null)
            onChange(parsed as Record<string, unknown>)
          } catch (e2) {
            setErr(e2 instanceof Error ? e2.message : '无效 JSON')
          }
        }}
      />
      {err && <div className="mt-1 text-[13px] text-warn">JSON 有误：{err}</div>}
    </div>
  )
}

/** 将显示草稿与持久数值分离，保留输入中的小数点和空值。 */
function NumericSchemaField({ field, value, onChange, describedBy }: {
  field: AlgorithmSchemaField
  value: unknown
  onChange: (value: unknown) => void
  describedBy?: string
}) {
  const spec = field.schema
  const display = algorithmNumericDisplay(spec, value)
  const external = Number.isFinite(display.value) ? String(display.value) : ''
  const [draft, setDraft] = useState(external)
  const lastEmitted = useRef<unknown>(value)
  useEffect(() => {
    if (!Object.is(value, lastEmitted.current)) setDraft(external)
    lastEmitted.current = value
  }, [value, external])
  const parsed = draft.trim() === '' ? Number.NaN : Number(draft)
  const raw = parsed / display.scale
  const schema = { type: 'object', properties: { [field.name]: spec }, required: [field.name] }
  const error = validateAlgorithmSchemaParams({ [field.name]: raw }, schema)
  const mode = spec['x-control'] === 'presets' || spec['x-control'] === 'slider' || spec['x-control'] === 'numberflow' ? spec['x-control'] : 'stepper'
  // 排他边界仅阻止越界步进，不以显示步长缩窄手动输入的合法范围。
  const min = display.min
  const max = display.max
  return <ParameterNumberInput label={spec.title || field.name} value={draft} unit={display.suffix}
    step={display.step} min={min} max={max} exclusiveMin={spec.exclusiveMinimum !== undefined} exclusiveMax={spec.exclusiveMaximum !== undefined} decimal={spec.type === 'number'} mode={mode}
    presets={spec['x-presets']?.map((item) => item * display.scale)}
    sliderMin={spec['x-slider-min']} sliderMax={spec['x-slider-max']} describedBy={describedBy}
    error={error ?? undefined} onChange={(next) => {
      setDraft(next)
      const nextValue = next.trim() === '' ? Number.NaN : Number(next) / display.scale
      lastEmitted.current = nextValue
      onChange(nextValue)
    }} />
}

export function SchemaFieldRow({ field, value, onChange }: {
  field: AlgorithmSchemaField
  value: unknown
  onChange: (value: unknown) => void
}) {
  const hintId = useId()
  const spec = field.schema
  const label = spec.title || field.name
  const hint = spec.description
  const heading = <span className="min-w-0 text-[15px]">{label}{hint && <span id={hintId} className={HINT}>{hint}</span>}</span>
  const options = algorithmFieldOptions(spec)
  if (options && (spec['x-control'] === 'choice' || spec['x-control'] === 'cards')) return (
    <div className={spec['x-control'] === 'cards' ? 'flex flex-col gap-2 border-t border-line py-3' : ROW}>
      {heading}
      <ChoiceGroup ariaLabel={label} describedBy={hint ? hintId : undefined} value={String(value ?? '')}
        variant={spec['x-control'] === 'cards' ? 'cards' : 'buttons'}
        options={options.map((option) => ({ ...option, description: spec['x-option-descriptions']?.[option.value] }))}
        onChange={(next) => onChange(spec.enum?.find((item) => String(item) === next) ?? next)} />
    </div>
  )
  if (options) return (
    <div className={ROW}>
      {heading}
      <Select className="min-w-[180px] shrink-0 justify-between px-3 py-1.5 text-[15px]" ariaLabel={label}
        value={String(value ?? '')} options={options}
        onChange={(next) => onChange(spec.enum?.find((item) => String(item) === next) ?? next)} />
    </div>
  )
  if (spec.type === 'boolean') return (
    <div className={ROW}>{heading}<Toggle on={value === true} ariaLabel={label} onClick={() => onChange(value !== true)} /></div>
  )
  if (spec.type === 'integer' || spec.type === 'number') {
    if (['stepper', 'numberflow', 'presets', 'slider'].includes(spec['x-control'] ?? '')) return (
      <div className={ROW}>{heading}<NumericSchemaField field={field} value={value} onChange={onChange} describedBy={hint ? hintId : undefined} /></div>
    )
    const display = algorithmNumericDisplay(spec, value)
    return <NumRow label={label} hint={hint} {...display} onChange={(next) => onChange(next / display.scale)} />
  }
  return (
    <div className={ROW}>{heading}<input className={VALUE_FIELD} aria-label={label}
      value={typeof value === 'string' ? value : ''} onChange={(event) => onChange(event.target.value)} /></div>
  )
}

/** 全部参数常显；名称、说明、选项文案与数值单位只读取服务端 schema。 */
export function SchemaEditor({ info, params, onChange, onValidationError }: {
  info: AlgorithmInfo | null
  params: Record<string, unknown>
  onChange: (params: Record<string, unknown>) => void
  onValidationError?: (error: string | null) => void
}) {
  const fields = info ? algorithmSchemaFields(info.params_schema) : null
  const effective = { ...algorithmSchemaDefaults(fields ?? []), ...info?.default_params, ...params }
  const error = info && fields ? validateAlgorithmParams(effective, info.name) ?? validateAlgorithmSchemaParams(effective, info.params_schema) : null
  useEffect(() => { if (info && fields) onValidationError?.(error) }, [info, fields, error, onValidationError])
  if (!info || !fields) return <GenericEditor params={params} onChange={onChange} onValidationError={onValidationError} />
  return (
    <div className="max-w-[720px]">
      {fields.map((field) => <SchemaFieldRow key={field.name} field={field} value={effective[field.name]}
        onChange={(value) => onChange({ ...params, [field.name]: value })} />)}
      {info.name === 'TWAP' && Number(effective.slices) > 0 && (
        <div className="text-xs text-ink-3">{Number(effective.total_duration) / Number(effective.slices)} 秒 / 片</div>
      )}
      {error && <div role="alert" className="py-2 text-[13px] text-warn">{error}</div>}
    </div>
  )
}

export interface AlgorithmEditorProps {
  slot: AlgorithmSlot
  channel: TradeChannel
  value: AlgorithmRef | null
  onChange: (value: AlgorithmRef | null) => void
  allowClear?: boolean
  onValidationError?: (error: string | null) => void
  nameVtName?: string
}

export function AlgorithmEditor({ slot, channel, value, onChange, allowClear, onValidationError, nameVtName }: AlgorithmEditorProps) {
  useReadRecovery(() => algorithmCatalog.load())
  const catalog = useSyncExternalStore(algorithmCatalog.subscribe, algorithmCatalog.getSnapshot, algorithmCatalog.getSnapshot)
  useEffect(() => {
    const state = algorithmCatalog.getSnapshot()
    if (state.data === null && !state.error) void algorithmCatalog.load()
  }, [])
  useEffect(() => { if (value === null) onValidationError?.(null) }, [value, onValidationError])
  const descriptor = useChannelDescriptor(channel)
  const fallback = slot === 'trade' ? descriptor?.defaults.trade_algorithm : descriptor?.defaults.empty_positions_algorithm
  const candidates = availableAlgorithms(catalog.data ?? [], channel, slot)
  const info = catalog.data?.find((algo) => algo.name === value?.method) ?? null
  const currentAvailable = candidates.some((algo) => algo.name === value?.method)
  const options = candidates.map((algo) => ({ value: algo.name, label: algo.label ? `${algo.label} · ${algo.name}` : algo.name }))
  if (value && !currentAvailable) options.push({ value: value.method, label: `${value.method} · 当前配置，清单未提供` })
  const pick = (method: string) => {
    if (method === value?.method) return
    const next = candidates.find((algo) => algo.name === method)
    if (next) onChange({ method, params: { ...next.default_params } })
  }

  return (
    <div>
      <ErrorNotice intent="read" hasData={catalog.data != null} contextKey={`${channel}:${slot}`} title="算法清单加载失败" evidence={[{ label: '渠道', value: channel }, { label: '槽位', value: slot }]} error={catalog.error} onRetry={() => algorithmCatalog.load()} />
      {catalog.data && candidates.length === 0 && <p className="mb-3 text-sm text-ink-3">当前渠道无可选算法</p>}
      {value === null ? (
        <div className="text-sm text-ink-3">未设置 · 使用默认清仓逻辑
          <button type="button" disabled={!fallback} className="ml-3 text-accent disabled:opacity-50"
            onClick={() => { if (fallback) onChange(structuredClone(fallback)) }}>设置清仓算法</button>
        </div>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <Select ariaLabel={slot === 'trade' ? '主交易算法' : '清仓算法'}
              className="min-w-0 max-w-full justify-between px-3 py-1.5 text-[15px]" value={value.method} onChange={pick} options={options}
              selectedValue={<span className="block min-w-0 truncate">
                <span style={nameVtName ? { viewTransitionName: nameVtName } : undefined}>{algoLabel(value.method)}</span>
                <span className="text-ink-3"> · {value.method}</span>
              </span>} />
            {catalog.loading && <Skeleton className="h-4 w-8" />}
            {fallback && JSON.stringify(value) !== JSON.stringify(fallback) && <button type="button" className="text-sm text-ink-3 hover:text-ink-1"
              onClick={() => onChange(structuredClone(fallback))}>恢复默认</button>}
            {allowClear && <button type="button" className="text-sm text-ink-3 hover:text-warn" onClick={() => onChange(null)}>清除</button>}
          </div>
          <MethodParamsStage method={value.method}>
            <p className="mb-3 max-w-[720px] whitespace-pre-line text-sm leading-relaxed text-ink-2">{info?.description || '说明暂不可用'}</p>
            <SchemaEditor key={value.method} onValidationError={onValidationError} info={info} params={value.params} onChange={(params) => onChange({ method: value.method, params })} />
          </MethodParamsStage>
        </>
      )}
    </div>
  )
}
