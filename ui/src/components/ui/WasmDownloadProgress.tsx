import type { WasmDownloadState } from '@/lib/wasmDownload'

// oxlint-disable-next-line react/only-export-components -- Shared size label for download prompts.
export const downloadSize = (bytes: number) => `${(bytes / 1_000_000).toFixed(2)} MB`
export function WasmDownloadProgress({ state, onCancel, onRetry }: {
  state: WasmDownloadState; onCancel: () => void; onRetry: () => void
}) {
  if (state.phase === 'downloading') return <div className="space-y-1 py-2 text-xs text-ink-2">
    <div className="flex flex-wrap items-center gap-3"><span role="status">正在下载组件 · {downloadSize(state.loaded)} / {downloadSize(state.total)} · {state.total ? Math.floor(state.loaded / state.total * 100) : 100}%</span><button type="button" className="text-accent focus-visible:outline-accent" onClick={onCancel}>取消下载</button></div>
    <progress aria-label="组件下载进度" className="h-1.5 w-full accent-accent" value={state.loaded} max={state.total || 1} />
  </div>
  if (state.phase === 'error') return <p role="alert" className="py-2 text-xs text-warn">{state.error}<button type="button" className="ml-3 text-accent focus-visible:outline-accent" onClick={onRetry}>重试下载</button></p>
  return null
}
