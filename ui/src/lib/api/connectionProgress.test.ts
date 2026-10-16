import { expect, test } from 'bun:test'
import { connectionCompletion, type ConnectionStep } from './connectionProgress'

const steps = (statuses: ConnectionStep['status'][]): ConnectionStep[] => statuses.map((status, i) => ({ id: `step${i}`, label: `${i}`, status }))

test('进度按完成步骤计算，最终确认前不能到 100%', () => {
  const partial = connectionCompletion({ status: 'running', steps: steps(['passed', 'running', 'pending']) })
  expect(partial.completed).toBe(1)
  expect(partial.total).toBe(3)
  expect(partial.percent).toBeCloseTo(100 / 3)
  expect(connectionCompletion({ status: 'running', steps: steps(['passed', 'passed']) }).percent).toBe(50)
  expect(connectionCompletion({ status: 'succeeded', steps: steps(['passed', 'passed']) }).percent).toBe(100)
})

test('失败和取消的未执行项不计入已完成步骤', () => {
  expect(connectionCompletion({ status: 'failed', steps: steps(['passed', 'failed', 'skipped', 'skipped']) }).percent).toBe(25)
  expect(connectionCompletion({ status: 'cancelled', steps: steps(['passed', 'skipped', 'skipped', 'skipped']) }).percent).toBe(25)
  expect(connectionCompletion({ status: 'queued', steps: [] }).percent).toBe(0)
})
