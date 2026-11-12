import { expect, test } from 'bun:test'
import { isBusyStatus, isExecutingStatus, phaseLabel } from './execProgress'

test('queued 不误标成下单', () => {
  expect(phaseLabel('queued')).toBe('等待开跑')
  expect(phaseLabel('executing')).toBe('下单')
})

test('queued 是忙但不是正在下单', () => {
  expect(isBusyStatus('queued')).toBe(true)
  expect(isExecutingStatus('queued')).toBe(false)
  expect(isExecutingStatus('running')).toBe(true)
})


test('未知阶段不冒充触发，失败事件不推进到对账', async () => {
  const { phaseIndex, laterPhase, executionEventPhase } = await import('./execProgress')
  expect(phaseIndex(null)).toBe(-1)
  expect(phaseLabel('new-phase')).toBe('进度待同步')
  expect(laterPhase('executing', 'planning')).toBe('executing')
  expect(executionEventPhase([{ event_type: 'target_computed' }, { event_type: 'execution_failed' }])).toBe('planning')
  expect(executionEventPhase([{ event_type: 'target_computed' }, { event_type: 'execution_completed' }], true)).toBe('planning')
})
