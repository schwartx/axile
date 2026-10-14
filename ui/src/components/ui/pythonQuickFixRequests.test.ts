import { expect, test } from 'bun:test'
import type { LSPClient } from '@codemirror/lsp-client'
import { ApiError } from '@/lib/api/client'
import { errorInfo } from '@/lib/errorInfo'
import { readPythonCodeActions, readPythonQuickFixOptions } from './pythonQuickFixRequests'

const range = { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } }
const input = { uri: 'file:///synthetic.py', code: 'x = 1', kind: 'portfolio' as const, range }

function clientWith(request: (method: string) => Promise<unknown>): Pick<LSPClient, 'request'> {
  return { request: request as LSPClient['request'] }
}

test('a failed code action request preserves its full error and can later recover to a genuine empty result', async () => {
  const raw = 'synthetic '.repeat(200) + '\nTraceback\npassword=synthetic\n尾部根因'
  const error = new Error(raw)
  let failed = true
  const client = clientWith(async () => { if (failed) throw error; return [] })
  const first = await readPythonCodeActions(client, { textDocument: { uri: input.uri }, range, context: { diagnostics: [] } })
  expect(first.error).toBe(error)
  expect(errorInfo(first.error).text).toContain(raw)
  failed = false
  const second = await readPythonCodeActions(client, { textDocument: { uri: input.uri }, range, context: { diagnostics: [] } })
  expect(second.error).toBeNull()
  expect(second.actions).toEqual([])
})

test('valid contract fixes survive an LSP repair failure', async () => {
  const error = new Error('code action failure\n尾部根因')
  const client = clientWith(async method => { if (method === 'textDocument/codeAction') throw error; return { items: [] } })
  const contract = { diagnostics: [], fixes: [{ title: '保留的契约修复', edits: [{ range, newText: 'fixed' }] }] }
  const result = await readPythonQuickFixOptions(client, input, async () => contract)
  expect(result.contract?.fixes).toEqual(contract.fixes)
  expect(result.error).toBe(error)
})

test('JSON-RPC object rejections retain their actual message, code and nested diagnostic data', async () => {
  const message = '原始错误\n' + 'synthetic '.repeat(200) + '\n尾部根因'
  const client = clientWith(async () => { throw { code: -32603, message, data: { traceback: 'Traceback\n完整原文', token: 'synthetic' } } })
  const result = await readPythonCodeActions(client, { textDocument: { uri: input.uri }, range, context: { diagnostics: [] } })
  expect(result.error?.message).toContain(message)
  const text = errorInfo(result.error).text
  expect(text).toContain('-32603')
  expect(text).toContain('完整原文')
  expect(text).toContain('synthetic')
  expect(text).not.toContain('[object Object]')
})

test('malformed repair responses remain a repair failure and preserve the original response', async () => {
  const client = clientWith(async () => ({ error: 'malformed response\n完整尾部' }))
  const result = await readPythonCodeActions(client, { textDocument: { uri: input.uri }, range, context: { diagnostics: [] } })
  expect(result.error?.message).toContain('快速修复响应格式无效')
  expect(result.error?.message).toContain('完整尾部')
  expect(result.actions).toEqual([])
})

test('multiple quick fix read failures retain every original and HTTP metadata', async () => {
  const contractError = new ApiError(500, 'contract failure\n尾部契约', { code: 'CONTRACT', requestId: 'req-contract' })
  const client = clientWith(async method => { throw new Error(`${method} failed\n完整 LSP 原文`) })
  const result = await readPythonQuickFixOptions(client, input, async () => { throw contractError })
  const text = errorInfo(result.error).text
  expect(text).toContain('尾部契约')
  expect(text).toContain('HTTP：500')
  expect(text).toContain('req-contract')
  expect(text).toContain('textDocument/diagnostic failed')
  expect(text).toContain('textDocument/codeAction failed')
})
