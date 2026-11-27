import type { ReactNode } from 'react'
import { ChevronDown, RefreshCw, Search } from 'lucide-react'

export const EXEC_COLS = 'grid grid-cols-2 gap-x-4 gap-y-2 xl:grid-cols-[155px_minmax(120px,1fr)_70px_95px_105px_115px_55px]'

export function JournalPageHeader({ title, actions }: { title: ReactNode; actions: ReactNode }) {
  return <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">{title}</div>
    <div className="ml-auto flex shrink-0 items-center gap-2">{actions}</div>
  </header>
}

export function JournalRefresh({ label, disabled, refreshing, onRefresh }: {
  label: string
  disabled: boolean
  refreshing: boolean
  onRefresh: () => void
}) {
  return <button type="button" aria-label={label} title={label} disabled={disabled} onClick={onRefresh}
    className="flex size-9 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-fill disabled:opacity-40">
    <RefreshCw size={16} aria-hidden className={refreshing ? 'animate-spin motion-reduce:animate-none' : ''} />
  </button>
}

export function JournalFilters({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center gap-3 border-b border-line pb-4">{children}</div>
}

export function JournalSymbolSearch({ value, onChange, maxLength }: {
  value: string
  onChange: (value: string) => void
  maxLength?: number
}) {
  return <label className="relative min-w-0 flex-1 basis-40 sm:max-w-64">
    <Search size={14} aria-hidden className="pointer-events-none absolute left-2.5 top-2.5 text-ink-3" />
    <input type="search" aria-label="搜索品种" placeholder="搜索品种" maxLength={maxLength}
      value={value} onChange={event => onChange(event.target.value)}
      className="w-full min-w-0 rounded-lg border border-line bg-surface py-1.5 pr-3 pl-8 text-[13px] text-ink-1 outline-none focus:border-accent" />
  </label>
}

export function JournalHeader({ columns = EXEC_COLS, children }: { columns?: string; children: ReactNode }) {
  return <div className={`${columns} hidden border-y border-line px-3 py-2 text-xs text-ink-3 xl:grid`}>{children}</div>
}

export function JournalField({ label, children }: { label: string; children: ReactNode }) {
  return <span className="block min-w-0 break-words xl:text-right"><span className="mr-2 text-xs text-ink-3 xl:hidden">{label}</span>{children}</span>
}

export function JournalRecordRow({ recordId, time, panelId, open, onToggle, label, children, details }: {
  recordId?: number
  time: ReactNode
  panelId: string
  open: boolean
  onToggle?: () => void
  label?: string
  children: ReactNode
  details?: ReactNode
}) {
  return <div className="border-b border-line" data-journal-record={recordId}>
    <button type="button" disabled={!onToggle} aria-expanded={onToggle ? open : undefined}
      aria-controls={onToggle ? panelId : undefined} aria-label={label} onClick={onToggle}
      className={`${EXEC_COLS} w-full items-baseline px-3 py-4 text-left text-[13px] hover:bg-bg-subtle disabled:cursor-default ${open ? 'bg-bg-subtle' : ''}`}>
      <span className="flex min-w-0 items-center gap-1 num text-xs text-ink-3">
        {onToggle && <ChevronDown size={14} aria-hidden className={`shrink-0 transition-transform motion-reduce:transition-none ${open ? 'rotate-180' : ''}`} />}
        {time}
      </span>
      {children}
    </button>
    {details && <div id={panelId} inert={!open}
      className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
        <div className="border-l-2 border-accent px-3 py-2">{details}</div>
      </div>
    </div>}
  </div>
}

export function JournalEvidenceHeader({ label, actions }: { label: ReactNode; actions?: ReactNode }) {
  return <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><span className="text-sm">{label}</span>{actions}</div>
}

export function JournalEvidenceTable({ label, className, children }: { label?: string; className?: string; children: ReactNode }) {
  return <table aria-label={label} className={className ?? 'w-full whitespace-nowrap text-left text-xs [&_td]:px-3 [&_td]:py-1.5 [&_th]:px-3 [&_th]:py-1.5'}>
    {children}
  </table>
}
