import { expect, test } from 'bun:test'
import { executionJustSettled, useLiveExecStore, type RunEntry } from './liveExec'

function run(over: Partial<RunEntry> = {}): RunEntry {
  return {
    executionId: 'e-1',
    kind: 'rebalance',
    phase: 'executing',
    status: 'running',
    pendingExecutionId: null,
    pendingKind: null,
    updatedAt: 1,
    ...over,
  }
}

test('executionJustSettled 只在从有到无时为真', () => {
  expect(executionJustSettled(run(), null)).toBe(true)
  expect(executionJustSettled(null, null)).toBe(false)
  expect(executionJustSettled(null, run())).toBe(false)
  expect(executionJustSettled(run(), run({ status: 'terminating' }))).toBe(false)
})

test('applySnapshot 保留 queued，丢弃终态', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().applySnapshot([
    { account_id: 1, execution_id: 'q1', kind: 'rebalance', phase: 'queued', status: 'queued' },
    { account_id: 2, execution_id: 'd1', kind: 'rebalance', phase: 'settling', status: 'done' },
  ])
  const running = useLiveExecStore.getState().running
  expect(running.get(1)?.status).toBe('queued')
  expect(running.get(1)?.executionId).toBe('q1')
  expect(running.has(2)).toBe(false)
})

test('reconcile 不把 queued 写成 running，并回填 pending', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().reconcile([
    {
      account_id: 8,
      name: 'a',
      remark: null,
      market: '期货',
      trade_channel: 'ctp',
      is_started: true,
      portfolio_id: null,
      is_scheduled: false,
      next_run_time: null,
      total_asset: 1,
      currency: 'CNY',
      holdings_count: 0,
      position_weights: [],
      last_is_success: null,
      last_exec_at: null,
      running_execution_id: 'e-8',
      running_kind: 'rebalance',
      running_phase: 'queued',
      running_status: 'queued',
      pending_execution_id: null,
      pending_kind: null,
    },
  ])
  const entry = useLiveExecStore.getState().running.get(8)
  expect(entry?.status).toBe('queued')
  expect(entry?.phase).toBe('queued')
})

test('reconcile 在 SSE 断线时把 queued 推进为 running', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-8',
    kind: 'rebalance',
    phase: 'queued',
    status: 'queued',
  })

  useLiveExecStore.getState().reconcile([
    {
      account_id: 8,
      name: 'a',
      remark: null,
      market: '期货',
      trade_channel: 'ctp',
      is_started: true,
      portfolio_id: null,
      is_scheduled: false,
      next_run_time: null,
      total_asset: 1,
      currency: 'CNY',
      holdings_count: 0,
      position_weights: [],
      last_is_success: null,
      last_exec_at: null,
      running_execution_id: 'e-8',
      running_kind: 'rebalance',
      running_phase: 'planning',
      running_status: 'running',
      pending_execution_id: null,
      pending_kind: null,
    },
  ])

  const entry = useLiveExecStore.getState().running.get(8)
  expect(entry?.status).toBe('running')
  expect(entry?.phase).toBe('planning')
})

test('reconcile 不用陈旧轮询把 running 降级为 queued', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-8',
    kind: 'rebalance',
    phase: 'executing',
    status: 'running',
  })

  useLiveExecStore.getState().reconcile([
    {
      account_id: 8,
      name: 'a',
      remark: null,
      market: '期货',
      trade_channel: 'ctp',
      is_started: true,
      portfolio_id: null,
      is_scheduled: false,
      next_run_time: null,
      total_asset: 1,
      currency: 'CNY',
      holdings_count: 0,
      position_weights: [],
      last_is_success: null,
      last_exec_at: null,
      running_execution_id: 'e-8',
      running_kind: 'rebalance',
      running_phase: 'queued',
      running_status: 'queued',
      pending_execution_id: null,
      pending_kind: null,
    },
  ])

  const entry = useLiveExecStore.getState().running.get(8)
  expect(entry?.status).toBe('running')
  expect(entry?.phase).toBe('executing')
})

test('SSE 显式 pending_execution_id null 会清掉 pending', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-8',
    kind: 'rebalance',
    phase: 'queued',
    status: 'queued',
    pending_execution_id: 'e-9',
    pending_kind: 'rebalance',
  })
  expect(useLiveExecStore.getState().running.get(8)?.pendingExecutionId).toBe('e-9')

  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-8',
    kind: 'rebalance',
    phase: 'executing',
    status: 'running',
    pending_execution_id: null,
    pending_kind: null,
  })
  const entry = useLiveExecStore.getState().running.get(8)
  expect(entry?.status).toBe('running')
  expect(entry?.pendingExecutionId).toBeNull()
})

test('SSE 上一张票的终态不会抹掉下一张票', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-next',
    kind: 'rebalance',
    phase: 'queued',
    status: 'queued',
  })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-prev',
    kind: 'rebalance',
    phase: 'settling',
    status: 'done',
  })
  const entry = useLiveExecStore.getState().running.get(8)
  expect(entry?.executionId).toBe('e-next')
  expect(entry?.status).toBe('queued')
})

test('SSE 同一张票的终态仍会摘掉账户条目', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-8',
    kind: 'rebalance',
    phase: 'executing',
    status: 'running',
  })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-8',
    kind: 'rebalance',
    phase: 'settling',
    status: 'done',
  })
  expect(useLiveExecStore.getState().running.has(8)).toBe(false)
})

test('reconcile 显式 pending_execution_id null 会清掉 pending', () => {
  useLiveExecStore.setState({ running: new Map() })
  useLiveExecStore.getState().applyEvent({
    account_id: 8,
    execution_id: 'e-8',
    kind: 'rebalance',
    phase: 'executing',
    status: 'running',
    pending_execution_id: 'e-9',
    pending_kind: 'rebalance',
  })
  useLiveExecStore.getState().reconcile([
    {
      account_id: 8,
      name: 'a',
      remark: null,
      market: '期货',
      trade_channel: 'ctp',
      is_started: true,
      portfolio_id: null,
      is_scheduled: false,
      next_run_time: null,
      total_asset: 1,
      currency: 'CNY',
      holdings_count: 0,
      position_weights: [],
      last_is_success: null,
      last_exec_at: null,
      running_execution_id: 'e-8',
      running_kind: 'rebalance',
      running_phase: 'executing',
      running_status: 'running',
      pending_execution_id: null,
      pending_kind: null,
    },
  ])
  expect(useLiveExecStore.getState().running.get(8)?.pendingExecutionId).toBeNull()
})


test('running 轮询推进阶段，迟到 SSE 和旧轮询均不能回退', () => {
  useLiveExecStore.setState({ running: new Map([[8, run({ executionId: 'e-8', phase: 'planning' })]]) })
  const account = { account_id: 8, running_execution_id: 'e-8', running_status: 'running', running_phase: 'executing' } as import('@/types/api').AccountDashboardItem
  useLiveExecStore.getState().reconcile([account])
  expect(useLiveExecStore.getState().running.get(8)?.phase).toBe('executing')
  useLiveExecStore.getState().reconcile([{ ...account, running_phase: 'planning' }])
  useLiveExecStore.getState().applyEvent({ account_id: 8, execution_id: 'e-8', phase: 'snapshot', status: 'running', kind: 'rebalance' })
  expect(useLiveExecStore.getState().running.get(8)?.phase).toBe('executing')
})
