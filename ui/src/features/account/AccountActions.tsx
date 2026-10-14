import { useEffect, useRef, useState } from 'react'
import { Copy, Pencil, Trash2 } from 'lucide-react'
import { ConfirmModal, type ConfirmSpec } from '@/components/ui/ConfirmModal'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Tooltip } from '@/components/ui/Tooltip'

interface AccountActionsProps {
  name: string
  isStarted: boolean
  running: boolean
  hasPendingSupplement?: boolean
  /** 真正在下单：禁用清仓。queued 时仍可清仓。 */
  executing: boolean
  /** 终止请求在途：按钮切「终止中…」并禁用，防连点；由 useTerminateAction 驱动。 */
  terminating: boolean
  onExec: () => void
  onClear: () => void
  onTerminate: () => void
  toggling?: boolean
  onToggleStarted: () => void
  onEdit: () => void
  onCopy: () => void
  onDelete: () => void
}

const BTN = 'cursor-pointer whitespace-nowrap rounded-lg border px-3 py-1.5 text-[14px]'

/** 主操作说明气泡：hover 满 2s 才出，压制掠过噪音；不弹窗。 */
const ACTION_TIP_MS = 2000

/**
 * 账户操作。
 *
 * 主操作槽（立即执行↔终止）：同一按钮随 ``running`` 切壳/提示/动作，标签走日记式换字；
 *   两态均单击即行动（无弹窗），hover 2s 出说明气泡——终止那句「已成交不回滚」即点击前唯一提醒。
 * 启停：单击即行动（无弹窗），hover 2s 出说明；标签同走日记式换字。
 * 清仓：hover 2s 出说明气泡；点击走确认弹窗（不可逆）。执行中禁用，气泡不触发，改由原生 title 兜底。
 * 删除：菜单内确认弹窗（不可逆）。
 * 执行中时以「终止」替代「立即执行」并禁用清仓。
 */
export function AccountActions(props: AccountActionsProps) {
  // menuOpen=意图（决定 pop-in / pop-out），menuMounted=是否在 DOM。
  // 收起需先播退场再卸载，故两态分离：合上时先 setMenuOpen(false) 触发 pop-out，
  // 播完 onAnimationEnd 再 setMenuMounted(false)。
  const [menuOpen, setMenuOpen] = useState(false)
  const [menuMounted, setMenuMounted] = useState(false)
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  const openMenu = () => {
    setMenuMounted(true)
    setMenuOpen(true)
  }

  // reduce-motion 下 select-pop-out 动画为 none，onAnimationEnd 不触发，故此处即时卸载兜底。
  const closeMenu = () => {
    setMenuOpen(false)
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) setMenuMounted(false)
  }

  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) closeMenu()
    }
    document.addEventListener('click', onDoc)
    return () => document.removeEventListener('click', onDoc)
  }, [menuOpen])

  const askClear = () =>
    setConfirm({
      title: '一键清仓',
      body: `把 ${props.name} 的所有持仓平掉、回到空仓。此操作不可撤销。`,
      okText: '确认清仓',
      danger: true,
      onConfirm: props.onClear,
    })

  const execTip = (
    <div className="flex flex-col gap-0.5">
      <span>现在按最新目标调一次仓。</span>
      <span className="text-ink-2">目标没变时多数空跑、几乎无成本。</span>
    </div>
  )

  const terminateTip = (
    <div className="flex flex-col gap-0.5">
      <span>停下正在进行的调仓。</span>
      <span className="text-ink-2">已成交的部分不会回滚。排队里的下一次也会取消。</span>
    </div>
  )

  const clearTip = (
    <div className="flex flex-col gap-0.5">
      <span>把所有持仓平掉、回到空仓。</span>
      <span className="text-ink-2">不可撤销；点击后仍会再确认一次。</span>
    </div>
  )

  const toggleTip = props.isStarted ? (
    <div className="flex flex-col gap-0.5">
      <span>暂停后不再按计划自动调仓。</span>
      <span className="text-ink-2">手动执行不受影响。</span>
    </div>
  ) : (
    <div className="flex flex-col gap-0.5">
      <span>恢复按计划自动调仓。</span>
      <span className="text-ink-2">仍可随时手动执行。</span>
    </div>
  )

  const menuItemBase =
    'flex w-full cursor-pointer items-center gap-2.5 rounded-lg border-0 bg-transparent px-3 py-2.5 text-left text-sm'
  // 编辑=可逆，中性；删除=不可逆，静止即红、悬停红底（与清仓/终止同属红档）。
  const menuItem = `${menuItemBase} text-ink-1 hover:bg-fill`
  const menuItemDanger = `${menuItemBase} text-bad hover:bg-bad/10`

  return (
    <div className="ml-auto flex flex-none items-center gap-2">
      {/* 主操作槽：同一按钮跨 running 存活，壳/提示/动作随态切，标签走日记式换字。
          两态若拆成两个分支会各自挂载，InkRewrite 当首帧不播——必须合一。 */}
      <Tooltip content={(props.running || props.hasPendingSupplement) ? terminateTip : execTip} delay={ACTION_TIP_MS} arrow>
        <button
          className={
            (props.running || props.hasPendingSupplement)
              ? `${BTN} border-bad/40 font-[550] text-bad hover:bg-bad/10 disabled:opacity-45 disabled:cursor-default disabled:hover:bg-transparent`
              : `${BTN} border-ink-1 bg-ink-1 font-[550] text-surface`
          }
          onClick={(props.running || props.hasPendingSupplement) ? props.onTerminate : props.onExec}
          // 终止在途禁用防连点；disabled 吞指针事件、气泡不触发，故补原生 title 兜「为何不可点」。
          disabled={props.terminating}
          title={props.terminating ? '终止请求已发出，正在停止…' : undefined}
          aria-label={props.terminating ? '终止中' : (props.running || props.hasPendingSupplement) ? '终止执行' : '立即执行'}
        >
          {/* 同槽日记式换字：立即执行↔终止↔终止中，纯 opacity crossfade，无 FLIP。 */}
          <InkRewrite
            text={props.terminating ? '■ 终止中…' : (props.running || props.hasPendingSupplement) ? '■ 终止执行' : '▶ 立即执行'}
            tone="label"
          />
        </button>
      </Tooltip>

      <Tooltip content={toggleTip} delay={ACTION_TIP_MS} arrow>
        <button
          className={`${BTN} border-line text-ink-2 hover:border-ink-3/40 hover:text-ink-1`}
          disabled={props.toggling}
          onClick={props.onToggleStarted}
          aria-label={props.isStarted ? '暂停自动执行' : '启动自动执行'}
        >
          {/* 同槽日记式换字：启动↔暂停，纯 opacity crossfade，无 FLIP。 */}
          <InkRewrite text={props.isStarted ? '⏸ 暂停' : '▶ 启动'} tone="label" />
        </button>
      </Tooltip>

      {/* 执行中禁用：disabled 会吞掉指针事件、气泡不触发，故保留原生 title 兜「为何不可点」。 */}
      <Tooltip content={clearTip} delay={ACTION_TIP_MS} arrow>
        <button
          className={`${BTN} border-bad/40 text-bad hover:bg-bad/10 disabled:opacity-40`}
          onClick={askClear}
          disabled={props.executing}
          title={props.executing ? '执行中，无法清仓' : undefined}
        >
          ⚠ 一键清仓
        </button>
      </Tooltip>

      {/* ⋯ 仅留罕见/不可逆的配置类动作 */}
      <div className="relative" ref={ref}>
        <button
          className="cursor-pointer rounded-md px-1.5 text-[23px] leading-none text-ink-3 hover:bg-fill hover:text-ink-1"
          onClick={(e) => {
            e.stopPropagation()
            if (menuOpen) closeMenu()
            else openMenu()
          }}
          aria-label="更多操作"
        >
          ⋯
        </button>
        {menuMounted && (
          <div
            className={`${menuOpen ? 'select-pop-in' : 'select-pop-out'} absolute right-0 z-[37] mt-1.5 min-w-[170px] rounded-xl border border-line bg-surface p-1.5 shadow-[0_12px_32px_rgba(0,0,0,0.16)]`}
            onAnimationEnd={() => {
              if (!menuOpen) setMenuMounted(false)
            }}
          >
            <button className={menuItem} onClick={() => { closeMenu(); props.onEdit() }}>
              <Pencil size={16} className="shrink-0" aria-hidden />
              <span>编辑账户</span>
            </button>
            <button className={menuItem} onClick={() => { closeMenu(); props.onCopy() }}>
              <Copy size={16} className="shrink-0" aria-hidden />
              <span>复制账户</span>
            </button>
            <div className="mx-1.5 my-1.5 h-px bg-line" />
            <button
              className={menuItemDanger}
              onClick={() => {
                closeMenu()
                setConfirm({
                  title: '删除账户',
                  body: `删除 ${props.name} 及其全部执行/绑定记录。此操作不可撤销。`,
                  okText: '删除',
                  danger: true,
                  onConfirm: props.onDelete,
                })
              }}
            >
              <Trash2 size={16} className="shrink-0" aria-hidden />
              <span>删除账户</span>
            </button>
          </div>
        )}
      </div>

      <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />
    </div>
  )
}
