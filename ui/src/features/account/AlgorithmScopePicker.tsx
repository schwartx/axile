import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { LoaderCircle, Plus, Trash2 } from 'lucide-react'
import { ConfirmModal, type ConfirmSpec } from '@/components/ui/ConfirmModal'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { ApiError } from '@/lib/api/client'
import { validateAlgorithmScope } from '@/lib/api/system'
import { shortErrorReason } from '@/lib/errorInfo'
import { algorithmScopeLabel } from '@/features/account/productAlgorithms'
import type { ChannelCapability } from '@/types/api'

/** 展示可配置的调仓范围，输入由渠道后端校验后才能加入草稿。 */
export function AlgorithmScopePicker({ channel, capability, products, value, onChange, onAdd, onRestoreDefault }: {
  channel: string; capability: ChannelCapability['algorithm_scope']; products: string[]; value: string
  onChange: (value: string) => void; onAdd: (value: string) => void
  onRestoreDefault: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [focused, setFocused] = useState(false)
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null)
  const [settling, setSettling] = useState<string | null>(null)
  const [settleWidth, setSettleWidth] = useState(260)
  const rowRef = useRef<HTMLDivElement>(null)
  const labelRef = useRef<HTMLSpanElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const addButtonRef = useRef<HTMLButtonElement>(null)
  const [code, setCode] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [validation, setValidation] = useState<{ input: string; code?: string; error?: string; retryable?: boolean } | null>(null)
  const input = code.trim()
  const formatError = input && capability && !new RegExp(`^(?:${capability.pattern})$`, 'i').test(input)
    ? `格式不正确，请按示例输入：${capability.example}` : null
  const current = validation?.input === input ? validation : null
  const label = (key: string) => algorithmScopeLabel(channel, key)
  const normalized = current?.code
  const duplicate = normalized && products.some(product => product.toLowerCase() === normalized.toLowerCase())
    ? `${label(normalized)} 已配置，请直接选择` : null
  const error = current?.error || duplicate
  const pending = Boolean(input && !error && !current?.code)

  useEffect(() => {
    if (!input || !capability) return
    const controller = new AbortController()
    setValidation(null)
    const timer = setTimeout(() => {
      if (formatError) { setValidation({ input, error: formatError }); return }
      void validateAlgorithmScope(channel, input, controller.signal).then(result => {
        if (!controller.signal.aborted) setValidation({ input, code: result.code })
      }).catch(reason => {
        if (!controller.signal.aborted) setValidation({
          input, error: shortErrorReason(reason), retryable: !(reason instanceof ApiError) || reason.status >= 500,
        })
      })
    }, 300)
    return () => { clearTimeout(timer); controller.abort() }
  }, [channel, input, capability, formatError, attempt])

  useEffect(() => {
    if (!adding || !inputRef.current) return
    // 等展开样式生效后再聚焦。
    const frame = requestAnimationFrame(() => {
      inputRef.current?.focus({ preventScroll: true })
      inputRef.current?.parentElement?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' })
    })
    return () => cancelAnimationFrame(frame)
  }, [adding])

  const focusProduct = () => rowRef.current?.querySelector<HTMLButtonElement>('[aria-label="调仓算法范围"] > button[aria-pressed="true"]')?.focus({ preventScroll: true })

  useEffect(() => {
    if (!settling) return
    focusProduct()
    const width = labelRef.current?.offsetWidth ?? 100
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => setSettleWidth(width))
    })
    // 过渡被浏览器或系统偏好中断时，也要恢复添加入口。
    const timer = setTimeout(() => setSettling(null), 400)
    return () => { cancelAnimationFrame(frame); clearTimeout(timer) }
  }, [settling])

  const resetInput = () => { setAdding(false); setFocused(false); setCode(''); setValidation(null) }
  const close = () => {
    resetInput()
    if (addButtonRef.current) requestAnimationFrame(() => addButtonRef.current?.focus({ preventScroll: true }))
  }
  const add = () => {
    if (!current?.code || error || pending || settling) return
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (!reduceMotion) { setSettleWidth(260); setSettling(current.code) }
    onAdd(current.code); resetInput()
    if (reduceMotion) requestAnimationFrame(focusProduct)
  }
  return <div ref={rowRef} className="relative flex h-12 min-w-0 items-center gap-2 py-2"
    style={{ '--algorithm-added-width': `${settleWidth}px` } as CSSProperties}
    onTransitionEnd={event => {
      if (settling && event.propertyName === 'width' && (event.target as HTMLElement).hasAttribute('aria-pressed')) setSettling(null)
    }}>
    <span ref={labelRef} aria-hidden className="pointer-events-none absolute invisible inline-block whitespace-nowrap px-2.5 text-xs font-semibold">{settling && label(settling)}</span>
    <div className="min-w-0 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      <Segmented ariaLabel="调仓算法范围" size="sm" value={value} onChange={onChange}
        className={`w-max shrink-0 [&>div]:ml-0 [&>div]:border-0 [&>div]:pl-0 ${settling ? '[&>button:last-of-type]:w-[var(--algorithm-added-width)] [&>button:last-of-type]:shrink-0 [&>button:last-of-type]:justify-start [&>button:last-of-type]:overflow-hidden [&>button:last-of-type]:whitespace-nowrap [&>button:last-of-type]:transition-[width] [&>button:last-of-type]:duration-200 [&>button:last-of-type]:ease-[cubic-bezier(.4,0,.2,1)] [&>button:last-of-type]:motion-reduce:transition-none [&>span]:transition-none' : ''}`}
        options={[{ value: '', label: '默认' }, ...products.map(product => ({ value: product, label: label(product) }))]}
        trailing={capability && <div
          className={`relative h-6 shrink-0 overflow-hidden duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${settling ? 'w-0 transition-none' : `transition-[width] ${adding ? 'w-[260px]' : 'w-[100px]'}`}`}
          onTransitionEnd={event => {
            if (adding && event.target === event.currentTarget && event.propertyName === 'width') {
              inputRef.current?.parentElement?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' })
            }
          }}>
          <button type="button" ref={addButtonRef} aria-expanded={adding} aria-controls="algorithm-product-form" aria-hidden={adding || !!settling} inert={adding || !!settling}
            onClick={() => setAdding(true)}
            className={`absolute left-0 top-0 inline-flex h-6 w-[100px] cursor-pointer items-center gap-1 whitespace-nowrap rounded-md px-2.5 text-xs text-ink-2 transition-colors duration-150 hover:bg-surface/70 hover:text-ink-1 active:bg-surface focus-visible:outline-2 focus-visible:outline-accent motion-reduce:transition-none ${adding || settling ? 'invisible' : 'visible'}`}>
            <Plus size={14} aria-hidden />添加品种
          </button>
          <div id="algorithm-product-form" role="group" aria-label="添加品种" aria-hidden={!adding} inert={!adding}
            className={`absolute inset-0 flex items-center gap-1 rounded-md bg-surface px-1 ${adding ? 'visible' : 'invisible'}`}>
            <Tooltip open={adding && (Boolean(error) || focused)} arrow interactive={Boolean(current?.retryable)} content={
              error ? <div role="alert" className="select-pop-in space-y-2">
                <p className="text-warn">{error}</p>
                {current?.retryable && <button type="button" onClick={() => setAttempt(previous => previous + 1)}
                  className="cursor-pointer rounded-chip px-2 py-1 text-xs text-accent focus-visible:outline-2 focus-visible:outline-accent">重试</button>}
              </div> : <p id="algorithm-code-help">{capability.description}</p>
            }>
              <input ref={inputRef} id="algorithm-product-code" aria-label="新增算法品种代码" aria-describedby="algorithm-code-status" aria-invalid={!!error} maxLength={80}
                placeholder={`例如 ${capability.example}`} value={code}
                onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
                onChange={event => { setCode(event.target.value); setValidation(null) }}
                onKeyDown={event => {
                  if (event.key === 'Enter') { event.preventDefault(); add() }
                  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() }
                }}
                className="h-6 min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-1.5 text-xs text-ink-1 caret-accent placeholder:text-ink-2 transition-colors duration-150 aria-invalid:border-warn focus-visible:outline-1 focus-visible:outline-accent motion-reduce:transition-none" />
            </Tooltip>
            <button type="button" aria-label="确认添加品种" aria-busy={pending} onClick={add} disabled={!input || pending || !!error || !current?.code}
              className="inline-flex h-6 w-10 shrink-0 cursor-pointer items-center justify-center rounded-md text-xs text-ink-1 transition-colors duration-150 hover:bg-fill active:bg-line focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:text-ink-3 motion-reduce:transition-none">
              {pending ? <LoaderCircle size={12} aria-hidden className="animate-spin motion-reduce:animate-none" /> : '添加'}
            </button>
            <button type="button" aria-label="取消添加品种" onClick={close}
              className="h-6 shrink-0 cursor-pointer rounded-md px-1.5 text-xs text-ink-2 transition-colors duration-150 hover:bg-fill hover:text-ink-1 active:bg-line focus-visible:outline-2 focus-visible:outline-accent motion-reduce:transition-none">取消</button>
            <span id="algorithm-code-status" aria-live="polite" className="sr-only">
              {pending ? '品种代码校验中' : !error && current?.code ? `代码格式通过：${label(current.code)}` : ''}
            </span>
          </div>
        </div>} />
    </div>
    {value && <button type="button" aria-label={`删除 ${label(value)} 的品种配置`} onClick={() => setConfirm({
      title: `删除 ${label(value)} 的品种配置？`,
      body: '删除后将沿用默认算法和时段，保存全部后生效。',
      okText: '删除配置', danger: true, onConfirm: onRestoreDefault,
    })}
      className="ml-auto inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-chip px-2 py-1.5 text-xs text-ink-2 transition-colors duration-150 hover:bg-warn-soft hover:text-warn active:bg-warn/20 active:text-warn focus-visible:outline-2 focus-visible:outline-warn motion-reduce:transition-none">
      <Trash2 size={14} aria-hidden />删除品种配置
    </button>}
    <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />
  </div>
}
