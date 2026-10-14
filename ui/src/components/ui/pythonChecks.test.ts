import { expect, test } from 'bun:test'
import { checkingSource, checkSource, checksAreEmpty, type PythonChecks } from './pythonChecks'
const empty = checkSource({ status: 'fulfilled', value: [] })
const snapshot: PythonChecks = { document: 'code', version: 1, generation: 1, lsp: empty, contract: empty, quickFixError: null }

test('only both completed sources can claim no static diagnostics', () => {
  expect(checksAreEmpty(snapshot)).toBe(true)
  expect(checksAreEmpty({ ...snapshot, lsp: checkingSource() })).toBe(false)
  const error = new Error('LSP 诊断失败\n尾部根因')
  const failed = checkSource({ status: 'rejected', reason: error })
  expect(failed.error).toBe(error)
  expect(checksAreEmpty({ ...snapshot, lsp: failed })).toBe(false)
  expect(checksAreEmpty(null)).toBe(false)
})
test('a successful source keeps diagnostics when another source fails', () => {
  const diagnostic = { message: '入口缺失', severity: 1, source: '入口契约', range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } } }
  const contract = checkSource({ status: 'fulfilled', value: [diagnostic] })
  expect(contract.diagnostics).toEqual([diagnostic])
  expect(checksAreEmpty({ ...snapshot, contract })).toBe(false)
})
