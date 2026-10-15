/**
 * 账户编辑壳：总览与子页共用的排版原子（小节 / 行 / 底栏）。
 *
 * 动效：页内仍偏静（α）；进/出子系统做「整卡 → 工作台顶栏」共享元素 FLIP
 * （同构壳几何连续，不共享文案内容 morph）。底栏仅 opacity。
 */

import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Link } from '@/components/ui/nav'
import { OverflowText } from '@/components/ui/OverflowText'
import { Skeleton } from '@/components/ui/Skeleton'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { MOTION_LAYOUT } from '@/lib/viewTransition'

/** 与入口卡 / 工作台顶栏同款的壳（圆角·边·底·内边距）。 */
const SHELL =
  'rounded-[12px] border border-line bg-surface px-4 py-3.5'

export const TEXT =
  'w-full rounded-[9px] border border-ink-3/25 bg-surface px-3 py-2 text-[15px] outline-none focus:border-accent'
export const NUM =
  'w-24 rounded-[9px] border border-ink-3/25 bg-surface px-3 py-1.5 num text-right text-[15px] outline-none focus:border-accent'
export const AREA =
  'w-full rounded-[9px] border border-ink-3/25 bg-surface px-3 py-2 font-mono text-[14px] leading-6 outline-none focus:border-accent'

/** 加载骨架。 */
export function EditLoading({
  /** 为真时不渲染 section/标题骨架（外层已挂真实标题做 FLIP）。 */
  bare = false,
}: {
  bare?: boolean
}) {
  const body = (
    <>
      {!bare && <Skeleton className="mt-3 h-6 w-44" />}
      <Skeleton className="mt-6 h-4 w-16" />
      <div className="mt-2 flex flex-col gap-3">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-9 w-full" />
        ))}
      </div>
    </>
  )
  if (bare) return body
  return (
    <section>
      {body}
    </section>
  )
}

/** 加载失败。 */
export function EditError({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  return (
    <section>
      <ErrorNotice intent="read" title="账户加载失败" error={error} onRetry={onRetry} />
    </section>
  )
}

/**
 * 小节：eyebrow 标题 + 发丝线，正文两列网格。
 */
export function Section({ label, action, children }: { label: string; action?: ReactNode; children: ReactNode }) {
  return (
    <div className="mt-7">
      <div className="mb-2 flex items-center gap-3">
        <span className="text-[12px] font-semibold tracking-wide text-ink-3">{label}</span>
        <span className="h-px min-w-0 flex-1 bg-line" />
        {action}
      </div>
      <div className="grid grid-cols-1 gap-x-8 gap-y-3 md:grid-cols-2">{children}</div>
    </div>
  )
}

/**
 * 编辑页当前配置摘要：承接草稿的人话表达，不重复页面身份，也不使用卡片壳抢层级。
 */
export function EditSynopsis({
  children,
  note,
}: {
  children: ReactNode
  note?: string
}) {
  return (
    <div className="mt-4">
      <div className="text-[12px] font-semibold tracking-wide text-ink-3">当前配置</div>
      <div className="mt-1 text-[15px] font-medium leading-6 text-ink-1">{children}</div>
      {note && <p className="mt-1 text-[14px] leading-5 text-ink-2">{note}</p>}
    </div>
  )
}

/** 设置行：左标签 / 右控件。 */
export function Row({
  label,
  hint,
  top,
  span,
  children,
}: {
  label: string
  hint?: string
  top?: boolean
  span?: boolean
  children: ReactNode
}) {
  return (
    <div className={`flex gap-4 ${span ? 'md:col-span-2' : ''} ${top ? 'items-start' : 'items-center'}`}>
      <div className={`w-24 flex-none text-[14px] text-ink-2 ${top ? 'pt-2' : ''}`}>
        {label}
        {hint && <span className="block text-[12px] text-ink-3">{hint}</span>}
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

/** 紧凑数字格。 */
export function NumCell({
  label,
  hint,
  value,
  onChange,
  bad,
}: {
  label: string
  hint?: string
  value: string
  onChange: (v: string) => void
  /** 越界时为真：标题旁标「超范围」（琥珀），并由上层禁用保存。 */
  bad?: boolean
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[14px] text-ink-2">
        {label}
        {hint && <span className="ml-1.5 text-[12px] text-ink-3">{hint}</span>}
        {bad && <span className="ml-1.5 text-[12px] text-warn">超范围</span>}
      </span>
      <input className={NUM} value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

/** 开关（中性轨；开=accent，成败不走红绿）。 */
export function Toggle({
  on,
  onClick,
  ariaLabel,
}: {
  on: boolean
  onClick: () => void
  ariaLabel?: string
}) {
  return (
    <button
      type="button"
      className={`relative h-[26px] w-[44px] flex-none cursor-pointer rounded-full border-0 transition ${
        on ? 'bg-accent' : 'bg-border-strong'
      }`}
      onClick={onClick}
      aria-pressed={on}
      aria-label={ariaLabel}
    >
      <span
        className={`absolute top-[3px] h-[20px] w-[20px] rounded-full bg-surface shadow transition-all ${
          on ? 'left-[21px]' : 'left-[3px]'
        }`}
      />
    </button>
  )
}

/**
 * 入口 / 工作台同构三列：左标签 · 中摘要 · 右操作位。
 * FLIP 时排版族一致，避免「列表行 → 大标题」假连续。
 */
function ShellColumns({
  label,
  hint,
  summary,
  trailing,
}: {
  label: string
  hint?: string
  summary: string
  trailing: ReactNode
}) {
  return (
    <div className="flex items-center gap-4">
      <div className="w-24 flex-none text-[14px] text-ink-2">
        {label}
        {hint && <span className="block text-[12px] text-ink-3">{hint}</span>}
      </div>
      <OverflowText className="min-w-0 flex-1 text-[15px] font-medium text-ink-1" text={summary} />
      <div className="flex-none">{trailing}</div>
    </div>
  )
}

/**
 * 总览入口卡：整块挂 ``shellVtName``，与 :func:`EditWorktopBar` 做同构壳 FLIP。
 */
export function EntryRow({
  label,
  hint,
  summary,
  to,
  shellVtName,
}: {
  label: string
  hint?: string
  summary: string
  to: string
  /** 与子页工作台顶栏配对的 view-transition-name。 */
  shellVtName: string
}) {
  const shellVtStyle: CSSProperties = { viewTransitionName: shellVtName }
  return (
    <Link
      to={to}
      style={shellVtStyle}
      className={`${SHELL} transition-[border-color] duration-100 ease-[cubic-bezier(0.4,0,0.2,1)] hover:border-ink-3/40 motion-reduce:transition-none md:col-span-2`}
    >
      <ShellColumns
        label={label}
        hint={hint}
        summary={summary}
        trailing={<span className="text-[13.5px] font-semibold text-accent">调整 ›</span>}
      />
    </Link>
  )
}

/**
 * 子页工作台顶栏：与入口卡同壳、同三列，挂同一 ``shellVtName``。
 * 右槽放 chip（或静默占位），不用大标题；说明文案在壳外。
 */
export function EditWorktopBar({
  label,
  hint,
  summary,
  trailing,
  shellVtName,
  lead,
}: {
  /** 与入口左列一致，如「执行算法」「定时」。 */
  label: string
  hint?: string
  /** 与入口中列一致的人话摘要（可随草稿更新）。 */
  summary: string
  /** 右列：渠道 chip 等；勿放「调整」。 */
  trailing?: ReactNode
  shellVtName: string
  lead?: string
}) {
  const shellVtStyle: CSSProperties = { viewTransitionName: shellVtName }
  return (
    <div className="mt-3">
      <div style={shellVtStyle} className={SHELL}>
        <ShellColumns
          label={label}
          hint={hint}
          summary={summary}
          trailing={
            trailing ?? (
              // 与入口「调整 ›」近似占位宽，减轻 FLIP 时右缘伸缩
              <span className="invisible text-[13.5px] font-semibold" aria-hidden>
                调整 ›
              </span>
            )
          }
        />
      </div>
      {lead && <p className="mt-2 text-[14px] text-ink-2">{lead}</p>}
    </div>
  )
}

/**
 * 子页底栏：常挂载；有改动时仅 opacity 到位（不 slide，方向 α）。
 *
 * 取消与保存都不离开本页：取消就地重置草稿，保存成功后 dirty 收敛、
 * 底栏自行淡出（与 SettingsSaveBar 语义一致）。
 */
export function EditSaveBar({
  changes,
  blocked,
  onCancel,
  onSave,
  saving,
  error = null,
}: {
  changes: string[]
  blocked?: boolean
  onCancel: () => void
  onSave: () => void
  saving?: boolean
  error?: unknown | null
}) {
  const open = changes.length > 0
  const barRef = useRef<HTMLDivElement>(null)
  const [barHeight, setBarHeight] = useState(0)

  useLayoutEffect(() => {
    const bar = barRef.current
    if (!bar) return
    const updateHeight = () => setBarHeight(bar.offsetHeight)
    updateHeight()
    const observer = new ResizeObserver(updateHeight)
    observer.observe(bar)
    return () => observer.disconnect()
  }, [])

  return (
    <>
      <div aria-hidden style={{ height: open ? barHeight : 0 }} />
      <div
        ref={barRef}
        className={`fixed right-0 bottom-0 left-[218px] z-30 border-t border-line bg-surface/85 backdrop-blur-md transition-opacity ${MOTION_LAYOUT} ${
          open ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
        aria-hidden={!open}
      >
        <div className="mx-auto w-full max-w-[2176px] px-10">
          <div className="flex items-center gap-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-[12px] font-semibold tracking-wide text-ink-3">
                待保存 · {changes.length} 项{blocked && <span className="text-warn"> · 有错误</span>}
              </div>
              <OverflowText className="num text-[14px] text-ink-1" text={changes.join('  ·  ')} />
            </div>
            <button
              type="button"
              className="flex-none cursor-pointer text-[14px] text-ink-2 hover:text-ink-1"
              onClick={onCancel}
              tabIndex={open ? 0 : -1}
            >
              取消
            </button>
            <button
              type="button"
              className="flex-none cursor-pointer rounded-[9px] border-0 bg-ink-1 px-5 py-2 text-[14.5px] font-semibold text-surface disabled:opacity-45"
              onClick={onSave}
              disabled={blocked || saving || !open}
              tabIndex={open ? 0 : -1}
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
          <ErrorNotice intent="mutation" title="保存失败" error={error} variant="mutation" onRetry={onSave} />
        </div>
      </div>
    </>
  )
}
