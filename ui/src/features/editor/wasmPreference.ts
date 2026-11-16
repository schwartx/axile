import { useSyncExternalStore } from 'react'

const KEY = 'axile.editor.local-assistance'
const listeners = new Set<() => void>()
let enabled = false
try { enabled = localStorage.getItem(KEY) === 'true' } catch { /* Storage may be unavailable. */ }
export function setEditorWasmEnabled(value: boolean) {
  if (enabled === value) return
  enabled = value
  try { localStorage.setItem(KEY, String(value)) } catch { /* Keep this tab usable. */ }
  listeners.forEach(listener => listener())
}
function subscribe(listener: () => void) {
  listeners.add(listener)
  const changed = (event: StorageEvent) => {
    if (event.key === KEY || event.key === null) { enabled = event.newValue === 'true'; listener() }
  }
  window.addEventListener('storage', changed)
  return () => { listeners.delete(listener); window.removeEventListener('storage', changed) }
}
export function useEditorWasmEnabled() { return useSyncExternalStore(subscribe, () => enabled, () => false) }
