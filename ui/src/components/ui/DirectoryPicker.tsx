import { ChevronLeft, ChevronRight, Folder, HardDrive, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'

import { directoryBreadcrumbs } from '@/components/ui/directoryPath'
import { OverflowText } from '@/components/ui/OverflowText'
import { getDirectories } from '@/lib/api/system'
import type { DirectoryListing } from '@/types/api'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

export function DirectoryPicker({
  open,
  initialPath,
  onClose,
  onSelect,
}: {
  open: boolean
  initialPath?: string
  onClose: () => void
  onSelect: (path: string) => void
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const locationRef = useRef<HTMLInputElement>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const triggerRef = useRef<HTMLElement | null>(null)
  const [listing, setListing] = useState<DirectoryListing | null>(null)
  const [location, setLocation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const requestedPath = useRef<string | undefined>(undefined)

  const load = useCallback(async (path?: string, preserveLocation = false): Promise<boolean> => {
    requestedPath.current = path
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true)
    setError(null)
    try {
      const next = await getDirectories(path, controller.signal)
      if (controller.signal.aborted) return false
      setListing(next)
      if (!preserveLocation) setLocation(next.path ?? '')
    } catch (caught) {
      if (controller.signal.aborted) return false
      setError(caught instanceof Error ? caught : new Error(String(caught)))
      return false
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
    return true
  }, [])
  useReadRecovery(async () => { await load(requestedPath.current, true) }, open)

  useEffect(() => {
    if (!open) return
    triggerRef.current = document.activeElement as HTMLElement | null
    setListing(null)
    setError(null)
    const initial = initialPath?.trim()
    let active = true
    void (async () => {
      if (!initial) {
        await load()
        return
      }
      if (await load(initial)) return
      if (active) await load()
    })()
    requestAnimationFrame(() => locationRef.current?.focus())
    return () => {
      active = false
      controllerRef.current?.abort()
      triggerRef.current?.focus({ preventScroll: true })
    }
  }, [open, initialPath, load])

  useEffect(() => {
    if (!open) return
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)')
      if (!focusable?.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', keydown)
    return () => window.removeEventListener('keydown', keydown)
  }, [open, onClose])

  if (!open) return null

  const submitLocation = (event: FormEvent) => {
    event.preventDefault()
    if (location.trim()) void load(location.trim())
    else void load()
  }

  const selectCurrent = () => {
    if (!listing?.path) return
    onSelect(listing.path)
    onClose()
  }

  return (
    <>
      <div className="fixed inset-0 z-[45] bg-scrim" onClick={onClose} />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="directory-picker-title"
        className="fixed inset-0 z-[46] flex min-h-0 flex-col bg-surface sm:inset-auto sm:left-1/2 sm:top-1/2 sm:h-[min(680px,86vh)] sm:w-[min(720px,92vw)] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:overflow-hidden sm:rounded-[8px] sm:shadow-[0_24px_60px_rgba(0,0,0,0.24)]"
      >
        <div className="flex h-14 flex-none items-center border-b border-line px-4 sm:px-5">
          <div id="directory-picker-title" className="text-[17px] font-[640]">选择目录</div>
          <span className="flex-1" />
          <button type="button" title="关闭" aria-label="关闭目录选择器" className="grid h-9 w-9 cursor-pointer place-items-center text-ink-2 hover:text-ink-1" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <form className="flex flex-none gap-2 border-b border-line px-4 py-3 sm:px-5" onSubmit={submitLocation}>
          <button type="button" title="上一级" aria-label="上一级" disabled={!listing?.parent} className="grid h-10 w-10 flex-none cursor-pointer place-items-center rounded-[8px] border border-line text-ink-2 disabled:cursor-default disabled:opacity-35" onClick={() => listing?.parent && void load(listing.parent)}>
            <ChevronLeft size={17} />
          </button>
          <input ref={locationRef} value={location} onChange={(event) => setLocation(event.target.value)} placeholder="输入绝对路径" spellCheck={false} className="h-10 min-w-0 flex-1 rounded-[8px] border border-line bg-surface px-3 font-mono text-[14px] outline-none focus:border-accent" />
          <button type="submit" className="h-10 flex-none cursor-pointer rounded-[8px] border border-line px-4 text-[14px] text-ink-2">前往</button>
        </form>

        <div className="flex min-h-10 flex-none items-center gap-1 overflow-x-auto border-b border-line px-4 py-2 sm:px-5" aria-label="当前目录">
          {listing?.path ? directoryBreadcrumbs(listing.path).map((crumb, index, crumbs) => (
            <div key={crumb.path} className="flex flex-none items-center">
              {index > 0 && <ChevronRight size={13} className="mx-0.5 text-ink-3" />}
              <button type="button" className={`cursor-pointer rounded px-1.5 py-1 text-[13px] ${index === crumbs.length - 1 ? 'text-ink-1' : 'text-ink-2 hover:bg-fill'}`} onClick={() => void load(crumb.path)}>
                {crumb.label}
              </button>
            </div>
          )) : <span className="text-[13px] text-ink-3">本机磁盘</span>}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2 sm:px-3" aria-busy={loading}>
          <ErrorNotice intent="read" title="目录读取失败" error={error} hasData={listing != null} embedded onRetry={async () => { await load(requestedPath.current) }} />
          {!error && loading && !listing && <div className="px-3 py-4 text-[14px] text-ink-3">正在读取目录…</div>}
          {listing?.entries.length === 0 && <div className="px-3 py-4 text-[14px] text-ink-3">此目录没有子文件夹</div>}
          {listing?.entries.map((entry) => (
            <button key={entry.path} type="button" className="flex h-11 w-full cursor-pointer items-center gap-3 rounded-[8px] px-3 text-left hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent" onClick={() => void load(entry.path)}>
              {listing.path === null ? <HardDrive size={17} className="text-ink-2" /> : <Folder size={17} className="text-ink-2" />}
              <OverflowText className="min-w-0 flex-1 text-[15px]" text={entry.name} />
              <ChevronRight size={15} className="text-ink-3" />
            </button>
          ))}
        </div>

        <div className="flex flex-none items-center gap-3 border-t border-line px-4 py-3 sm:px-5">
          <OverflowText className="min-w-0 flex-1 text-[13px] text-ink-3" text={listing?.path ?? '请选择文件夹'} />
          <button type="button" className="h-10 cursor-pointer rounded-[8px] border border-line px-4 text-[14px] text-ink-2" onClick={onClose}>取消</button>
          <button type="button" disabled={!listing?.path} className="h-10 cursor-pointer rounded-[8px] border border-ink-1 bg-ink-1 px-4 text-[14px] font-[550] text-surface disabled:cursor-default disabled:opacity-40" onClick={selectCurrent}>使用此目录</button>
        </div>
      </div>
    </>
  )
}
