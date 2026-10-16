import type { AccountActivity } from '@/lib/api/accounts'
import type { PortfolioAccountRecord } from '@/types/api'
import type { TimeWindow } from '@/features/account/executionJournal'
import { executionRecordError } from '@/features/account/executionRecordError'
import { executionOutcome } from '@/features/account/executionOutcome'
import { shanghaiTime } from './costs'

export function performanceEvents(bindings: PortfolioAccountRecord[], activity: AccountActivity[], window: TimeWindow, names: Map<number, string>) {
  return [
    ...bindings.map(b => ({ time: b.created_at, tag: '换绑', text: b.portfolio_id == null ? '解绑组合' : names.get(b.portfolio_id) ?? `组合 #${b.portfolio_id}`, executionId: null as string | null })),
    ...activity.flatMap(a => {
      if (a.kind === 'schedule_skip') return [{ time: a.occurred_at, tag: '跳过', text: a.reason_code === 'BUSY' ? '执行中' : '休市', executionId: null as string | null }]
      if (a.record.is_success === 1) return []
      const view = executionOutcome(a.record)
      const tag = view.sessionScope === 'all' ? '跳过' : view.state === 'TERMINATED' ? '终止' : view.state === 'PARTIAL' ? '未完成' : view.state === 'BLOCKED' ? '受阻' : '失败'
      return [{ time: a.occurred_at, tag, text: view.sessionScope ? view.title : executionRecordError(a.record) || '执行未完成', executionId: a.record.execution_id }]
    }),
  ].filter(e => shanghaiTime(e.time) >= window.start && shanghaiTime(e.time) < window.end).sort((a, b) => shanghaiTime(b.time) - shanghaiTime(a.time))
}
