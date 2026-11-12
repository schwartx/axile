/**
 * 执行阶段进度模型 —— 与后端 `axile.server.execution.live.PHASE_ORDER` 一一对应。
 *
 * 五步确定态阶段条：触发 → 冻结输入 → 算目标 → 下单 → 对账。后端事件流按此推进
 * （单调不回退），前端据 phase 键渲染阶段条与文案。
 */

/** 阶段键 → 中文短标签，顺序即进度顺序。 */
export const PHASES = [
  { key: 'triggered', label: '触发' },
  { key: 'snapshot', label: '冻结输入' },
  { key: 'planning', label: '算目标' },
  { key: 'executing', label: '下单' },
  // 后端 key 沿用 `settling`，但这步实质是「对账」（核对实际仓位 vs 目标、产出
  // reconciliation），永续调仓无 T+n 交收/CCP 清算，故标签用机构口径的「对账」而非「结算」。
  { key: 'settling', label: '对账' },
] as const

export type PhaseKey = (typeof PHASES)[number]['key']

/** 阶段键 → 索引；未知阶段保持未知。 */
export function phaseIndex(phase: string | null | undefined): number {
  const i = PHASES.findIndex((p) => p.key === phase)
  return i
}

/** 阶段键 → 进行时文案（用于「正在执行 · X」）。queued 不是五步之一。 */
export function phaseLabel(phase: string | null | undefined): string {
  if (phase === 'queued') return '等待开跑'
  return PHASES[phaseIndex(phase)]?.label ?? '进度待同步'
}

/** 从已保存的生命周期与订单事件恢复最后确认阶段，不从结果行猜进度。 */
export function executionEventPhase(events: { event_type: string }[]): string {
  const phases: Record<string, string> = {
    execution_started: 'triggered', input_snapshotted: 'snapshot', target_computed: 'planning',
    order_submitted: 'executing', order_acknowledged: 'executing', order_terminal: 'executing',
    execution_completed: 'settling',
  }
  return events.reduce((phase, event) => laterPhase(phase, phases[event.event_type]), '')
}

/** 同一执行只采用更靠后的已知阶段。 */
export function laterPhase(current: string | null | undefined, incoming: string | null | undefined): string {
  return phaseIndex(incoming) > phaseIndex(current) ? incoming! : current ?? ''
}

/** 单笔任务终态优先于可能迟到的实时运行帧。 */
export function isActiveExecution(status: string | null | undefined): boolean {
  return status === 'QUEUED' || status === 'RUNNING' || status === 'TERMINATING'
}

/** 是否处于真正下单（阶段条、禁用清仓/刷新）。 */
export function isExecutingStatus(status: string | null | undefined): boolean {
  return status === 'running' || status === 'terminating'
}

/** 是否有未完成票（主槽切终止、显示忙态）。 */
export function isBusyStatus(status: string | null | undefined): boolean {
  return status === 'queued' || isExecutingStatus(status)
}

/** 执行种类 → 动词（区分「执行 / 清仓」的进行态标题）。 */
export function runVerb(kind: string | null | undefined): string {
  return kind === 'clear_positions' || kind === 'clear' ? '清仓' : '执行'
}
