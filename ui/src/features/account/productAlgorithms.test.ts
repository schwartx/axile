import { expect, test } from 'bun:test'
import { productAlgorithmDraft, productAlgorithmError, productAlgorithmValue } from './productAlgorithms'
import { algorithmScheduleSummary } from './algorithmSchedule'

test('品种默认和跨日时段独立复制，提交剔除草稿键，清空删除全部品种', () => {
  const source = { 'SHFE.ag': { algorithm: { method: 'TWAP', params: { slices: 3 } }, algorithm_schedule: [
    { start: '21:00', end: '02:30', algorithm: { method: 'TWAP', params: { slices: 8 } } },
  ] } }
  const draft = productAlgorithmDraft(source)
  draft['SHFE.ag'].algorithm.params.slices = 4
  expect(source['SHFE.ag'].algorithm.params.slices).toBe(3)
  expect(productAlgorithmValue(draft)['SHFE.ag']).toEqual({ ...source['SHFE.ag'], algorithm: { method: 'TWAP', params: { slices: 4 } } })
  expect(productAlgorithmValue(draft)['SHFE.ag'].algorithm_schedule[0]).not.toHaveProperty('key')
  expect(productAlgorithmDraft(source, draft)['SHFE.ag'].schedule[0].key).toBe(draft['SHFE.ag'].schedule[0].key)
  expect(productAlgorithmValue({})).toEqual({})
  expect(algorithmScheduleSummary(source['SHFE.ag'].algorithm, 1, 2)).toContain('2 个品种')
})

test('独立检查所有品种时段，允许不同品种的相同时段', () => {
  const algorithm = { method: 'TWAP', params: { slices: 3 } }
  const period = { start: '21:00', end: '02:30', algorithm }
  const draft = productAlgorithmDraft({ ag: { algorithm, algorithm_schedule: [period] }, rb: { algorithm, algorithm_schedule: [period] } })
  expect(productAlgorithmError(draft)).toBeNull()
  draft.rb.schedule.push({ ...draft.rb.schedule[0], key: 'overlap' })
  expect(productAlgorithmError(draft)).toContain('rb：')
  expect(productAlgorithmError(draft)).toContain('重叠')
})
