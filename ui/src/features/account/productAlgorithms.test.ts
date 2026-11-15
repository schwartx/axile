import { expect, test } from 'bun:test'
import { algorithmScopeLabel, productAlgorithmDraft, productAlgorithmError, productAlgorithmValue } from './productAlgorithms'

test('品种草稿不修改已保存参数，排序不影响脏检查，证券代码保留交易所', () => {
  const saved = { 'SHFE.ag': { algorithm: { method: 'TWAP', params: { total_duration: 30 } } } }
  const draft = productAlgorithmDraft(saved)
  draft['SHFE.ag'].algorithm.params.total_duration = 60
  expect(saved['SHFE.ag'].algorithm.params.total_duration).toBe(30)
  expect(Object.keys(productAlgorithmValue({ z: saved['SHFE.ag'], a: saved['SHFE.ag'] }))).toEqual(['a', 'z'])
  expect(algorithmScopeLabel('ctp', 'SHFE.ag')).toBe('ag')
  expect(algorithmScopeLabel('tq', 'SHFE.ag')).toBe('ag')
  expect(algorithmScopeLabel('gm', '600000.SH')).toBe('600000.SH')
  expect(productAlgorithmDraft()).toEqual({})
  expect(productAlgorithmError({})).toBeNull()
})
