import { Info } from 'lucide-react'
import { Tooltip } from '@/components/ui/Tooltip'

/** 账户执行超时的通用说明入口。 */
export function ExecutionTimeoutHelp() {
  return (
    <Tooltip
      arrow
      content={
        <div>
          <div className="font-medium text-ink-1">执行超时</div>
          <div className="mt-1 text-ink-2">
            单次执行的总上限，各时段共用。超时后中断，不再开新单；挂单留待下次执行前清理。
          </div>
          <div className="mt-1 text-ink-3">
            无法中断渠道内部卡死，与算法「最长等待」分属不同层级。
          </div>
        </div>
      }
    >
      <button
        type="button"
        aria-label="查看执行超时说明"
        className="inline-flex size-6 shrink-0 items-center justify-center rounded-chip text-ink-3 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent"
      >
        <Info size={14} aria-hidden />
      </button>
    </Tooltip>
  )
}
