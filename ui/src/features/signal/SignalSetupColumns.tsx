import type { ReactNode } from 'react'

export function SignalSetupColumns({ children, sidebar, footer }: { children: ReactNode; sidebar: ReactNode; footer?: ReactNode }) {
  return <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(0,360px)] gap-8">
    <div className="flex min-h-0 min-w-0 flex-col">{children}</div>
    <div className="flex min-h-0 flex-col gap-3">
      <div className="min-h-0 flex-1 overflow-y-auto">{sidebar}</div>
      <div className="shrink-0">{footer}</div>
    </div>
  </div>
}
