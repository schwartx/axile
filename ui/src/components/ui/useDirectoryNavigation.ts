import { useCallback, useRef, useState } from 'react'
import { flushSync } from 'react-dom'

import { getDirectories } from '@/lib/api/system'
import { withViewTransition } from '@/lib/viewTransition'
import type { DirectoryListing } from '@/types/api'

interface Position { scroll: number; preview: number }

function visibleRow(container: HTMLDivElement | null, path: string | null) {
  if (!container || !path) return null
  const row = container.querySelector<HTMLButtonElement>(`[data-directory-path="${CSS.escape(path)}"]`)
  if (!row) return null
  const bounds = container.getBoundingClientRect()
  const rect = row.getBoundingClientRect()
  return rect.top >= bounds.top && rect.bottom <= bounds.bottom ? row : null
}

/** 只记录视口位置；目录内容始终重新读取。 */
export function useDirectoryNavigation(algorithms: boolean) {
  const directoriesRef = useRef<HTMLDivElement>(null)
  const previewRef = useRef<HTMLDivElement>(null)
  const listingRef = useRef<DirectoryListing | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const active = useRef<{ stop: () => void } | null>(null)
  const positions = useRef(new Map<string, Position>())
  const requestedPath = useRef<string | undefined>(undefined)
  const locationRevision = useRef(0)
  const [listing, setListing] = useState<DirectoryListing | null>(null)
  const [location, setLocation] = useState('')
  const [focusedPath, setFocusedPath] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const cancel = useCallback(() => {
    controllerRef.current?.abort()
    active.current?.stop()
    active.current = null
  }, [])

  const reset = useCallback(() => {
    cancel()
    positions.current.clear()
    listingRef.current = null
    setListing(null)
    setError(null)
    setLoading(false)
    setPending(false)
  }, [cancel])

  const commit = useCallback(async (next: DirectoryListing, revision: number, preserveLocation: boolean, controller: AbortController, restoreFocus: boolean) => {
    const current = listingRef.current
    const returning = !!current?.path && next.path === current.parent && next.entries.some(entry => entry.path === current.path)
    const entering = !!current?.path && next.parent === current.path && current.entries.some(entry => entry.path === next.path)
    const identity = entering ? next.path : returning ? current!.path : null
    const source = visibleRow(directoriesRef.current, identity)
    const destinationFocus = returning ? current!.path : next.path
    const position = !entering && next.path ? positions.current.get(next.path) : undefined
    let destination: HTMLButtonElement | null = null
    let cleaned = false
    const cleanup = () => {
      if (cleaned) return
      cleaned = true
      source?.style.removeProperty('view-transition-name')
      destination?.style.removeProperty('view-transition-name')
    }
    const update = () => {
      if (controller.signal.aborted) return
      source?.style.removeProperty('view-transition-name')
      listingRef.current = next
      setListing(next)
      setFocusedPath(destinationFocus)
      setLoading(false)
      setPending(false)
      if (!preserveLocation && revision === locationRevision.current) setLocation(next.path ?? '')
    }
    const afterUpdate = () => {
      if (controller.signal.aborted) return
      if (directoriesRef.current) directoriesRef.current.scrollTop = position?.scroll ?? 0
      if (previewRef.current) previewRef.current.scrollTop = position?.preview ?? 0
      destination = source ? visibleRow(directoriesRef.current, identity) : null
      if (destination) Object.assign(destination.style, { viewTransitionName: 'algorithm-directory-row' })
      if ((restoreFocus || (returning && document.activeElement?.tagName !== 'INPUT')) && destinationFocus) {
        const focusRow = directoriesRef.current?.querySelector<HTMLButtonElement>(`[data-directory-path="${CSS.escape(destinationFocus)}"]`)
          ?? directoriesRef.current?.querySelector<HTMLButtonElement>('[data-current-directory]')
        focusRow?.focus({ preventScroll: true })
      }
    }
    if (!algorithms || !current || current.path === next.path) {
      flushSync(update)
      afterUpdate()
      cleanup()
      return
    }
    if (source) Object.assign(source.style, { viewTransitionName: 'algorithm-directory-row' })
    const transition = withViewTransition(update, 'directory-browser', afterUpdate)
    if (!transition) { cleanup(); return }
    const running = { stop: () => { transition.skipTransition(); cleanup() } }
    active.current = running
    void transition.ready.catch(() => {})
    void transition.finished.catch(() => {}).finally(() => {
      cleanup()
      if (active.current === running) active.current = null
    })
    await transition.updateCallbackDone
    if (source && !destination) running.stop()
  }, [algorithms])

  const load = useCallback(async (path?: string, preserveLocation = false): Promise<boolean | undefined> => {
    cancel()
    const current = listingRef.current
    if (current?.path) positions.current.set(current.path, {
      scroll: directoriesRef.current?.scrollTop ?? 0,
      preview: previewRef.current?.scrollTop ?? 0,
    })
    const restoreFocus = directoriesRef.current?.contains(document.activeElement) ?? false
    const controller = new AbortController()
    controllerRef.current = controller
    requestedPath.current = path
    const revision = locationRevision.current
    setLoading(true)
    setPending(false)
    setError(null)
    const timer = window.setTimeout(() => { if (!controller.signal.aborted) setPending(true) }, 150)
    try {
      const next = await getDirectories(path, controller.signal, algorithms)
      if (controller.signal.aborted) return
      await commit(next, revision, preserveLocation, controller, restoreFocus)
      return controller.signal.aborted ? undefined : true
    } catch (caught) {
      if (controller.signal.aborted) return
      setError(caught instanceof Error ? caught : new Error(String(caught)))
      return false
    } finally {
      window.clearTimeout(timer)
      if (!controller.signal.aborted) { setLoading(false); setPending(false) }
    }
  }, [algorithms, cancel, commit])

  return { listing, location, setLocation, locationRevision, focusedPath, setFocusedPath,
    loading, pending, error, load, reset, cancel, requestedPath, directoriesRef, previewRef }
}
