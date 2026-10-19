import type { ReactNode } from 'react'
import { Tooltip } from '@/components/ui/Tooltip'

/** 读取降级的安静提示，悬停及键盘聚焦均可查看说明。 */
export function ReadStatusDot({ label, children }: { label: string; children: ReactNode }) {
  return <Tooltip content={children}>
    <span tabIndex={0} role="img" aria-label={label} className="ml-2 inline-flex h-4 w-4 items-center justify-center rounded align-middle focus-visible:outline-2 focus-visible:outline-accent">
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-warn" />
    </span>
  </Tooltip>
}
