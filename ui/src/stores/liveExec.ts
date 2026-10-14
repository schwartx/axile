import { create } from 'zustand'
import type { AccountDashboardItem } from '@/types/api'

/**
 * 实时执行态 store —— 「哪个账户此刻在跑、跑到哪个阶段」的单一读取源。
 *
 * 第一性原理：运行态的唯一真源是后端并发锁（registry）。前端不再靠本地 runner
 * 猜测，而是把两路服务端信号汇进这里，让所有视图从一处读：
 *   - **SSE**（热增量，秒级）：`applySnapshot`（连上/重连时的权威全量）+ `applyEvent`（逐帧）。
 *   - **仪表盘轮询**（冷兜底，5s）：`reconcile`，在 SSE 断线时仍能播种/清理。
 *
 * 对账避免抖动：轮询是 5s 陈旧快照，只用来「补 SSE 缺的」和「清理已陈旧的」，不会
 * 覆盖刚由 SSE 点亮、尚新鲜的条目（见 `reconcile` 的 STALE 守卫）。
 */

/** 单账户在途执行条目。 */
export interface RunEntry {
  executionId: string
  kind: string | null
  /** 阶段键（见 execProgress.PHASES）；queued 时为 `queued`。 */
  phase: string
  /** queued | running | terminating | done | failed | terminated。 */
  status: string
  pendingExecutionId: string | null
  pendingKind: string | null
  /** 本地最近更新时刻（ms），供对账 STALE 守卫。 */
  updatedAt: number
}

/** SSE 帧（snapshot 列表项 / event 单帧同构）。 */
export interface RunFrame {
  account_id: number
  execution_id: string
  kind: string | null
  phase: string
  status: string
  pending_execution_id?: string | null
  pending_kind?: string | null
}

/** 终态标记：命中即视为「已结束」，从运行集移除。 */
const TERMINAL = new Set(['done', 'failed', 'terminated'])

/** 轮询对账时，超过该时长（ms）未收到 SSE 更新的条目才允许被「未在跑」的轮询清除。 */
const STALE_MS = 6500

const ACTIVE_STATUS_ORDER: Record<string, number> = {
  queued: 0,
  running: 1,
  terminating: 2,
}

interface LiveExecState {
  /** 执行域已取得当前连接的快照；流控域故障不影响该标记。 */
  connected: boolean
  error: string | null
  beginReconnect: () => void
  disconnect: (error?: string | null) => void
  /** account_id → 在途执行；不在跑的账户不出现在表中。 */
  running: Map<number, RunEntry>
  /** SSE 连上/重连的权威全量快照，直接替换运行集。 */
  applySnapshot: (frames: RunFrame[]) => void
  /** SSE 单帧增量：终态仅摘同一 execution_id，否则 upsert。 */
  applyEvent: (frame: RunFrame) => void
  /** 仪表盘轮询兜底对账（SSE 断线时仍正确）。 */
  reconcile: (accounts: AccountDashboardItem[]) => void
}

function pendingOf(
  incoming: { pending_execution_id?: string | null; pending_kind?: string | null },
  prev?: RunEntry,
): { id: string | null; kind: string | null } {
  if ('pending_execution_id' in incoming) {
    return { id: incoming.pending_execution_id ?? null, kind: incoming.pending_kind ?? null }
  }
  return { id: prev?.pendingExecutionId ?? null, kind: prev?.pendingKind ?? null }
}

function toEntry(frame: RunFrame, updatedAt: number, prev?: RunEntry): RunEntry {
  const pending = pendingOf(frame, prev)
  return {
    executionId: frame.execution_id,
    kind: frame.kind ?? (prev?.executionId === frame.execution_id ? prev.kind : null),
    phase: frame.phase,
    status: frame.status,
    pendingExecutionId: pending.id,
    pendingKind: pending.kind,
    updatedAt,
  }
}

export const useLiveExecStore = create<LiveExecState>((set) => ({
  connected: false,
  error: null,
  beginReconnect: () => set({ connected: false, error: null }),
  disconnect: (error = null) => set({ connected: false, error }),
  running: new Map(),

  applySnapshot: (frames) =>
    set(() => {
      const now = Date.now()
      const next = new Map<number, RunEntry>()
      for (const f of frames) {
        if (!TERMINAL.has(f.status)) next.set(f.account_id, toEntry(f, now))
      }
      return { running: next, connected: true, error: null }
    }),

  applyEvent: (frame) =>
    set((s) => {
      const next = new Map(s.running)
      const prev = next.get(frame.account_id)
      if (TERMINAL.has(frame.status)) {
        // 终态只摘同一张票。A 的晚到 done 不能把已经点亮的 B 整户抹掉。
        if (prev == null || prev.executionId === frame.execution_id) {
          next.delete(frame.account_id)
        }
      } else {
        next.set(frame.account_id, toEntry(frame, Date.now(), prev))
      }
      return { running: next, connected: true, error: null }
    }),

  reconcile: (accounts) =>
    set((s) => {
      const now = Date.now()
      const next = new Map(s.running)
      for (const a of accounts) {
        const existing = next.get(a.account_id)
        if (a.running_execution_id) {
          const status = a.running_status ?? 'running'
          if (!existing || existing.executionId !== a.running_execution_id) {
            next.set(a.account_id, {
              executionId: a.running_execution_id,
              kind: a.running_kind ?? null,
              phase: a.running_phase ?? (status === 'queued' ? 'queued' : 'triggered'),
              status,
              pendingExecutionId: a.pending_execution_id ?? null,
              pendingKind: a.pending_kind ?? null,
              updatedAt: now,
            })
          } else {
            const advancesStatus = (ACTIVE_STATUS_ORDER[status] ?? -1) > (ACTIVE_STATUS_ORDER[existing.status] ?? -1)
            next.set(a.account_id, {
              ...existing,
              kind: existing.kind ?? a.running_kind ?? null,
              phase: advancesStatus ? (a.running_phase ?? existing.phase) : existing.phase,
              status: advancesStatus ? status : existing.status,
              pendingExecutionId: pendingOf(a, existing).id,
              pendingKind: pendingOf(a, existing).kind,
            })
          }
        } else if (existing && now - existing.updatedAt > STALE_MS) {
          // 轮询说没在跑，且该条目已陈旧（SSE 多半断了）→ 清除。新鲜条目不动，避免
          // 覆盖刚由 SSE 点亮、而轮询快照还没赶上的执行。
          next.delete(a.account_id)
        }
      }
      return { running: next }
    }),
}))

/** 便捷选择器：读某账户的在途执行条目（无则 null）。 */
export function useRunning(accountId: number): RunEntry | null {
  return useLiveExecStore((s) => s.running.get(accountId) ?? null)
}

/** 在途执行从有到无：此时执行记录与资产快照已落库，观测面应立刻重读。 */
export function executionJustSettled(previous: RunEntry | null, current: RunEntry | null): boolean {
  return previous != null && current == null
}
