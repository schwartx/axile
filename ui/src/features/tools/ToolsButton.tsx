import { Wrench, X } from 'lucide-react'
import { useId, useLayoutEffect, useRef, useState, type ComponentType } from 'react'
import { createPortal } from 'react-dom'
import { useMatch } from 'react-router'

import { Segmented } from '@/components/ui/Segmented'
import { useDomainStore } from '@/stores/domain'
import { useNavigationStore } from '@/stores/ui'

export type AccountToolProps = { accountId: number; open: boolean }
export type AccountTool = { value: string; label: string; panel: ComponentType<AccountToolProps> }

// 渠道可在同名目录的 tools.ts 导出自己的工具列表。
const channelTools = new Map(Object.entries(import.meta.glob<AccountTool[]>('../channels/*/tools.ts', {
  eager: true, import: 'tools',
})).map(([path, tools]) => [path.split('/')[2], tools]))

type Point = { x: number; y: number }
const EDGE = 8
const ARROWS: Record<string, Point> = {
  ArrowLeft: { x: -16, y: 0 },
  ArrowRight: { x: 16, y: 0 },
  ArrowUp: { x: 0, y: -16 },
  ArrowDown: { x: 0, y: 16 },
}

function clampToViewport(point: Point, panel: DOMRect): Point {
  return {
    x: Math.max(EDGE, Math.min(point.x, window.innerWidth - panel.width - EDGE)),
    y: Math.max(EDGE, Math.min(point.y, window.innerHeight - panel.height - EDGE)),
  }
}

export function ToolsButton() {
  const route = useMatch('/accounts/:id/*')
  const activeId = useNavigationStore(state => state.activeAccountId)
  const accountId = route?.params.id ? Number(route.params.id) : activeId
  const account = useDomainStore(state => state.accounts?.find(item => item.account_id === accountId && !item.archived_at))
  if (!account) return null
  const tools = channelTools.get(account.trade_channel)
  if (!tools?.length) return null
  return <ChannelToolbox key={account.trade_channel} accountId={account.account_id} tools={tools} />
}

function ChannelToolbox({ accountId, tools }: { accountId: number; tools: AccountTool[] }) {
  const id = useId()
  const tabPanelId = `${id}-tab-panel`
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const drag = useRef<Point | null>(null)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState<Point | null>(null)
  const [tab, setTab] = useState(tools[0].value)

  useLayoutEffect(() => {
    const panel = panelRef.current
    if (!open || !panel) return
    const focusTarget = panel.querySelector<HTMLElement>('[data-active-tool] [data-tool-autofocus]') ?? panel
    focusTarget.focus({ preventScroll: true })
    if (focusTarget instanceof HTMLInputElement) focusTarget.select()
    const fit = () => {
      const rect = panel.getBoundingClientRect()
      const point = clampToViewport(rect, rect)
      if (point.x !== rect.x || point.y !== rect.y) setPosition(point)
    }
    fit()
    const observer = new ResizeObserver(fit)
    observer.observe(panel)
    window.addEventListener('resize', fit)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', fit)
    }
  }, [open])

  function close() {
    drag.current = null
    setOpen(false)
    triggerRef.current?.focus({ preventScroll: true })
  }

  return <>
    <button ref={triggerRef} type="button" aria-label="工具箱" title="工具箱" aria-haspopup="dialog" aria-expanded={open}
      aria-controls={open ? id : undefined}
      className="grid h-8 w-8 cursor-pointer place-items-center rounded-chip text-ink-3 hover:bg-fill hover:text-ink-1 aria-expanded:bg-fill aria-expanded:text-ink-1"
      onClick={() => open ? close() : setOpen(true)}>
      <Wrench size={17} aria-hidden />
    </button>
    {createPortal(
      <div ref={panelRef} id={id} role="dialog" aria-modal={false} aria-label="工具箱" tabIndex={-1}
        hidden={!open}
        className={`fixed z-30 ${open ? 'flex' : 'hidden'} max-h-[calc(100dvh-1rem)] w-[21rem] max-w-[calc(100vw-1rem)] flex-col overflow-hidden rounded-xl border border-border-strong bg-surface text-ink-1 shadow-[0_6px_18px_rgba(0,0,0,0.28)]`}
        style={position ? { left: position.x, top: position.y } : { right: 20, top: (triggerRef.current?.getBoundingClientRect().bottom ?? 48) + 10 }}
        onKeyDown={event => {
          if (event.key !== 'Escape' || event.defaultPrevented) return
          event.stopPropagation()
          close()
        }}>
        <div className="flex shrink-0 items-center gap-2 border-b border-line px-2.5">
          <div role="group" aria-label="移动工具窗" title="拖动或用方向键移动工具窗" tabIndex={0}
            className="flex min-w-0 flex-1 cursor-grab touch-none select-none items-center gap-2 rounded-md py-1.5 active:cursor-grabbing"
            onPointerDown={event => {
              if (event.button !== 0 || !event.isPrimary || !panelRef.current) return
              if (event.target instanceof Element && event.target.closest('button')) return
              const rect = panelRef.current.getBoundingClientRect()
              drag.current = { x: event.clientX - rect.x, y: event.clientY - rect.y }
              event.currentTarget.setPointerCapture(event.pointerId)
            }}
            onPointerMove={event => {
              if (!drag.current || !event.currentTarget.hasPointerCapture(event.pointerId) || !panelRef.current) return
              setPosition(clampToViewport({ x: event.clientX - drag.current.x, y: event.clientY - drag.current.y }, panelRef.current.getBoundingClientRect()))
            }}
            onLostPointerCapture={() => { drag.current = null }}
            onKeyDown={event => {
              const delta = ARROWS[event.key]
              if (event.target !== event.currentTarget || !delta || !panelRef.current) return
              event.preventDefault()
              const rect = panelRef.current.getBoundingClientRect()
              setPosition(clampToViewport({ x: rect.x + delta.x, y: rect.y + delta.y }, rect))
            }}>
            <Wrench size={15} className="shrink-0 text-ink-3" aria-hidden />
            <Segmented size="sm" ariaLabel="工具" panelId={tabPanelId} options={tools} value={tab} onChange={setTab} />
          </div>
          <button type="button" aria-label="关闭工具箱" onClick={close}
            className="grid h-7 w-7 cursor-pointer place-items-center rounded-chip text-ink-3 hover:bg-fill hover:text-ink-1">
            <X size={16} aria-hidden />
          </button>
        </div>
        <div className="min-h-0 overflow-y-auto overscroll-contain p-3">
          <div id={tabPanelId} role="tabpanel" aria-labelledby={`${tabPanelId}-${tab}`}>
            {tools.map(({ value, panel: Tool }) => (
              <div key={value} hidden={value !== tab} data-active-tool={value === tab ? '' : undefined}>
                <Tool key={accountId} accountId={accountId} open={open && value === tab} />
              </div>
            ))}
          </div>
        </div>
      </div>, document.body,
    )}
  </>
}
