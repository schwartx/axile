import { ChevronLeft, ChevronRight, Eye, Folder, FolderOpen, HardDrive, LoaderCircle, X } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react'

import { apiSend } from '@/lib/api/client'
import type { AlgorithmInfo } from '@/types/api'
import { directoryBreadcrumbs } from '@/components/ui/directoryPath'
import { OverflowText } from '@/components/ui/OverflowText'
import { useDirectoryNavigation } from '@/components/ui/useDirectoryNavigation'
import type { DirectoryListing } from '@/types/api'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

export function DirectoryPicker({
  open,
  initialPath,
  algorithms = false,
  onClose,
  onSelect,
}: {
  open: boolean
  initialPath?: string
  algorithms?: boolean
  onClose: () => void
  onSelect: (path: string, listing: DirectoryListing) => void
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const locationRef = useRef<HTMLInputElement>(null)
  const triggerRef = useRef<HTMLElement | null>(null)
  const [loadedFiles, setLoadedFiles] = useState<Record<string, AlgorithmInfo[]>>({})
  const [fileErrors, setFileErrors] = useState<Record<string, string>>({})
  const [loadingFiles, setLoadingFiles] = useState<string[]>([])
  const loadFile = async (directory: string, file: string) => {
    const key = `${directory}/${file}`
    setLoadingFiles(values => [...values, key])
    setFileErrors(values => ({ ...values, [key]: '' }))
    try {
      const result = await apiSend<AlgorithmInfo[]>('POST', '/algorithms/load', { directory, file })
      setLoadedFiles(values => ({ ...values, [key]: result }))
    } catch (error) {
      setFileErrors(values => ({ ...values, [key]: error instanceof Error ? error.message : String(error) }))
    } finally {
      setLoadingFiles(values => values.filter(value => value !== key))
    }
  }
  const [showHidden, setShowHidden] = useState(false)
  const { listing, location, setLocation, locationRevision, focusedPath, setFocusedPath,
    loading, pending, error, load, reset, cancel, requestedPath, directoriesRef, previewRef } = useDirectoryNavigation(algorithms)

  useReadRecovery(async () => { await load(requestedPath.current, true) }, open)

  useEffect(() => {
    if (!open) return
    triggerRef.current = document.activeElement as HTMLElement | null
    reset()
    const initial = initialPath?.trim()
    let active = true
    void (async () => {
      if (!initial) {
        await load()
        return
      }
      if (await load(initial) !== false) return
      if (active) await load()
    })()
    requestAnimationFrame(() => locationRef.current?.focus())
    return () => {
      active = false
      cancel()
      triggerRef.current?.focus({ preventScroll: true })
    }
  }, [open, initialPath, load, reset, cancel])

  useEffect(() => {
    if (!open) return
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.altKey && event.key === 'ArrowUp' && listing?.parent) {
        event.preventDefault()
        void load(listing.parent)
        return
      }
      if (event.key !== 'Tab') return
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled):not([tabindex="-1"]), input:not(:disabled)')
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
  }, [open, onClose, listing?.parent, load])

  if (!open) return null

  const submitLocation = (event: FormEvent) => {
    event.preventDefault()
    if (location.trim()) void load(location.trim())
    else void load()
  }

  const selectCurrent = () => {
    if (!canSelect || !listing?.path) return
    onSelect(listing.path, listing)
    onClose()
  }

  const canSelect = Boolean(listing?.path) && !loading && !error && (!algorithms || (
    listing?.discovery?.can_use === true && location === listing.path
  ))

  const currentActive = !loading || requestedPath.current === listing?.path
  const entries = listing?.entries.filter(entry => !algorithms || showHidden || !entry.name.startsWith('.')) ?? []
  const hiddenCount = listing?.entries.filter(entry => entry.name.startsWith('.')).length ?? 0
  const currentName = listing?.path ? directoryBreadcrumbs(listing.path).at(-1)?.label ?? listing.path : '本机磁盘'
  const rowStyle = 'flex h-9 w-full min-w-0 cursor-pointer items-center gap-2 rounded-[7px] border-l-2 px-2 text-left transition-colors duration-150 motion-reduce:transition-none focus-visible:outline-2 focus-visible:outline-accent'
  const moveDirectoryFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[data-directory-entry]'))
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const key = event.key
    if (index < 0 || !['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(key) || event.altKey) return
    event.preventDefault()
    const next = key === 'Home' ? 0 : key === 'End' ? buttons.length - 1 : Math.max(0, Math.min(buttons.length - 1, index + (key === 'ArrowDown' ? 1 : -1)))
    buttons[next]?.focus()
  }

  return (
    <>
      <div className="fixed inset-0 z-[45] bg-scrim" onClick={onClose} />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="directory-picker-title"
        className={`fixed inset-0 z-[46] flex min-h-0 flex-col bg-surface sm:inset-auto sm:left-1/2 sm:top-1/2 ${algorithms ? 'sm:h-[min(560px,86vh)] sm:w-[min(920px,94vw)]' : 'sm:h-[min(680px,86vh)] sm:w-[min(720px,92vw)]'} sm:-translate-x-1/2 sm:-translate-y-1/2 sm:overflow-hidden sm:rounded-[8px] sm:shadow-[0_24px_60px_rgba(0,0,0,0.24)]`}
      >
        <div className="flex h-14 flex-none items-center border-b border-line px-4 sm:px-5">
          <div id="directory-picker-title" className="text-[17px] font-[640]">{algorithms ? '选择算法目录' : '选择目录'}</div>
          <span className="flex-1" />
          <button type="button" title="关闭" aria-label="关闭目录选择器" className="grid h-9 w-9 cursor-pointer place-items-center text-ink-2 hover:text-ink-1" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <form className="flex flex-none gap-2 border-b border-line px-4 py-3 sm:px-5" onSubmit={submitLocation}>
          <button type="button" title="上一级" aria-label="上一级" disabled={!listing?.parent} className="grid h-10 w-10 flex-none cursor-pointer place-items-center rounded-[8px] border border-line text-ink-2 disabled:cursor-default disabled:opacity-35" onClick={() => listing?.parent && void load(listing.parent)}>
            <ChevronLeft size={17} />
          </button>
          <input ref={locationRef} aria-label="目录路径" value={location} onChange={(event) => { locationRevision.current++; setLocation(event.target.value) }} placeholder="输入绝对路径" spellCheck={false} className="h-10 min-w-0 flex-1 rounded-[8px] border border-line bg-surface px-3 font-mono text-[14px] outline-none focus:border-accent" />
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

        {algorithms ? (
          <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1.8fr)_minmax(0,1fr)] overflow-hidden">
            <section className="flex min-h-0 min-w-0 flex-col border-r border-line" aria-label="目录导航">
              <div className="flex h-10 flex-none items-center justify-between gap-2 border-b border-line px-3 text-[12px] text-ink-3">
                <span>目录</span>
                <button type="button" aria-label="显示隐藏目录" aria-pressed={showHidden} title="显示隐藏目录" onClick={() => { setShowHidden(value => !value); setFocusedPath(listing?.path ?? null) }} className={`flex h-7 cursor-pointer items-center gap-1 rounded px-1.5 transition-colors duration-150 motion-reduce:transition-none hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent ${showHidden ? 'text-accent' : 'text-ink-3'}`}>
                  <Eye size={14} aria-hidden /><span className="hidden sm:inline">隐藏项{hiddenCount > 0 ? ` · ${hiddenCount}` : ''}</span>
                </button>
              </div>
              <div ref={directoriesRef} className="directory-browser-list min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable] overscroll-contain p-2" onKeyDown={moveDirectoryFocus} aria-busy={loading}>
                {listing?.path && <button type="button" data-directory-entry data-current-directory data-directory-path={listing.path} aria-current={currentActive ? 'location' : undefined} tabIndex={focusedPath === listing.path ? 0 : -1} onFocus={() => setFocusedPath(listing.path)} onClick={() => void load(listing.path!)} className={`${rowStyle} ${currentActive ? 'border-accent bg-accent-soft text-ink-1' : 'border-transparent text-ink-3'}`}>
                  <FolderOpen size={16} className="flex-none text-accent" aria-hidden />
                  <OverflowText className="min-w-0 flex-1 text-[13px] font-medium" text={currentName} />
                  {currentActive && <span className="hidden flex-none text-[11px] text-ink-3 sm:inline">当前</span>}
                </button>}
                {entries.map(entry => {
                  const pending = loading && requestedPath.current === entry.path
                  return <button key={entry.path} type="button" data-directory-entry data-directory-path={entry.path} tabIndex={focusedPath === entry.path ? 0 : -1} onFocus={() => setFocusedPath(entry.path)} aria-current={pending ? 'location' : undefined} className={`${rowStyle} mt-0.5 ${pending ? 'border-accent bg-accent-soft text-ink-1' : 'border-transparent text-ink-2 hover:bg-fill hover:text-ink-1'}`} onClick={() => void load(entry.path)}>
                    <Folder size={16} className="flex-none" aria-hidden />
                    <OverflowText className="min-w-0 flex-1 text-[13px]" text={entry.name} />
                    <ChevronRight size={13} className="flex-none text-ink-3" aria-hidden />
                  </button>
                })}
                {!loading && entries.length === 0 && <p className="px-2 py-4 text-[12px] leading-5 text-ink-3">{hiddenCount && !showHidden ? '没有可见子目录' : '没有子目录'}</p>}
              </div>
            </section>
            <section className="directory-browser-preview flex min-h-0 min-w-0 flex-col" aria-label="当前目录的算法" aria-busy={loading}>
              <div className="flex h-10 flex-none items-center gap-2 border-b border-line px-3 text-[12px] text-ink-2 sm:px-4" role="status" aria-live="polite">
                <span className="grid w-3.5 flex-none place-items-center" aria-hidden>{pending && <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" />}</span>
                <span>{pending ? '正在检查目录…' : error ? '目录读取失败' : `当前目录的算法 · ${listing?.discovery?.algorithms.length ?? 0}`}</span>
              </div>
              <div ref={previewRef} className="min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable] overscroll-contain px-3 py-3 sm:px-4">
                <ErrorNotice intent="read" title="目录读取失败" error={error} hasData={listing != null} embedded onRetry={async () => { await load(requestedPath.current) }} />
                <div aria-hidden={!!error}>
                  {!error && listing?.discovery && <>
                    {listing.discovery.algorithms.length === 0 ? <div className="py-6 text-[13px] leading-6 text-ink-3">
                      <FolderOpen size={24} className="mb-3 text-ink-3" aria-hidden />
                      <p className="text-ink-2">当前目录没有发现算法</p>
                      <p className="mt-1">从左侧进入脚本所在的文件夹。</p>
                    </div> : <ul className="divide-y divide-line">
                      {listing.discovery.algorithms.map(algorithm => <li key={`${algorithm.file}:${algorithm.name}:${algorithm.function}`} className="py-3 first:pt-0">
                        <p className="break-all text-[14px] font-medium text-ink-1">{algorithm.name || algorithm.function}</p>
                        <OverflowText className="mt-1 block min-w-0 font-mono text-[12px] text-ink-3" text={algorithm.file} />
                        {!algorithm.name && !loadedFiles[`${listing.path}/${algorithm.file}`] && <p className="mt-1 text-[12px] text-ink-3">名称待加载</p>}
                        <button type="button" disabled={loadingFiles.includes(`${listing.path}/${algorithm.file}`) || !!loadedFiles[`${listing.path}/${algorithm.file}`]} className="mt-2 cursor-pointer text-[12px] text-accent disabled:opacity-50" onClick={() => void loadFile(listing.path!, algorithm.file)}>{loadingFiles.includes(`${listing.path}/${algorithm.file}`) ? '正在加载…' : loadedFiles[`${listing.path}/${algorithm.file}`] ? '已加载' : '加载文件'}</button>
                        {loadedFiles[`${listing.path}/${algorithm.file}`]?.map(meta => <p key={meta.name} className="mt-2 text-[13px] text-ink-1">{meta.label || meta.name} · {meta.name}</p>)}
                        {fileErrors[`${listing.path}/${algorithm.file}`] && <p role="alert" className="mt-2 text-[12px] text-warn">{fileErrors[`${listing.path}/${algorithm.file}`]}</p>}
                      </li>)}
                    </ul>}
                    {listing.discovery.issues.map((issue, index) => <p key={index} className="mt-3 break-words text-[12px] leading-5 text-warn">{issue.file}：{issue.message}</p>)}
                  </>}
                </div>
              </div>
            </section>
          </div>
        ) : (
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

        )}

        <div className="flex flex-none items-center gap-3 border-t border-line px-4 py-3 sm:px-5">
          {algorithms ? <span className="min-w-0 flex-1 text-[12px] text-ink-3">仅添加当前目录</span> : <OverflowText className="min-w-0 flex-1 text-[13px] text-ink-3" text={listing?.path ?? '请选择文件夹'} />}
          <button type="button" className="h-10 cursor-pointer rounded-[8px] border border-line px-4 text-[14px] text-ink-2" onClick={onClose}>取消</button>
          <button type="button" disabled={!canSelect} className="h-10 cursor-pointer rounded-[8px] border border-ink-1 bg-ink-1 px-4 text-[14px] font-[550] text-surface disabled:cursor-default disabled:opacity-40" onClick={selectCurrent}>{algorithms ? '添加此目录' : '使用此目录'}</button>
        </div>
      </div>
    </>
  )
}
