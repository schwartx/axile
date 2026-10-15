import { ChevronLeft } from 'lucide-react'
import type { To } from 'react-router'
import { Link } from '@/components/ui/nav'
import { Tooltip } from '@/components/ui/Tooltip'

export interface BackLinkProps {
  to: To
  label: string
}

/** 标题行内的返回入口：显式指定上级页面，直达子页时同样可用。 */
export function BackLink({ to, label }: BackLinkProps) {
  return (
    <Tooltip content={label}>
      <Link
        to={to}
        aria-label={label}
        className="inline-flex size-11 shrink-0 items-center justify-center rounded-chip text-ink-2 transition-colors duration-150 hover:bg-bg-subtle hover:text-ink-1 active:bg-fill focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent motion-reduce:transition-none md:size-9 [@media(pointer:coarse)]:size-11"
      >
        <ChevronLeft size={18} strokeWidth={2} aria-hidden="true" />
      </Link>
    </Tooltip>
  )
}
