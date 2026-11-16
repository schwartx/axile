import { afterAll, expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import init, { Workspace, PositionEncoding } from './wasm/ty_wasm'

await init({ module_or_path: readFileSync(new URL('./wasm/ty_wasm_bg.wasm', import.meta.url)) })
const workspace = new Workspace('/draft', PositionEncoding.Utf16, {})
const library = JSON.parse(new TextDecoder().decode(Bun.zstdDecompressSync(readFileSync(new URL('./wasm/library.json.bin', import.meta.url))))) as { files: Record<string, string> }
for (const [path, code] of Object.entries(library.files)) workspace.openFile(path, code).free()
workspace.updateOptions({ environment: { 'extra-paths': ['/lib'], 'python-version': '3.12' } })
afterAll(() => workspace.free())

test('the zstd-compressed WASM is lossless', () => {
  expect(Buffer.from(Bun.zstdDecompressSync(readFileSync(new URL('./wasm/ty_wasm_bg.wasm.bin', import.meta.url)))))
    .toEqual(readFileSync(new URL('./wasm/ty_wasm_bg.wasm', import.meta.url)))
})

function request(code: string, method: string, params = {}) {
  const file = workspace.openFile('/draft/portfolio.py', code)
  try { return JSON.parse(workspace.axileRequest(method, file, JSON.stringify(params))) }
  finally { workspace.closeFile(file) }
}

test('offline Context completion, diagnostics, source navigation and formatting use the packaged WASM', () => {
  const code = 'from axile.server.context import Context\ndef calculate_portfolio(context: Context):\n    context.get_'
  const completions = request(code, 'textDocument/completion', { position: { line: 2, character: 16 } })
  expect(completions.some((item: { label: string }) => item.label === 'get_price')).toBe(true)
  const report = request('wrong: int = "中文😀"\n', 'textDocument/diagnostic')
  expect(report.items.some((item: { message: string }) => item.message.includes('not assignable'))).toBe(true)
  expect(report.items[0].range.end.character).toBeGreaterThan(0)
  expect(request('value={"中文":1}\n', 'axile/format').code).toBe('value = {"中文": 1}\n')
  const definition = request('from axile.server.context import Context\n', 'textDocument/definition', { position: { line: 0, character: 35 } })
  expect(definition[0].uri).toBe('file:///lib/axile/server/context.py')
})

test('symbols, folds, semantic tokens and rename preserve the current editing features', () => {
  const code = 'class Strategy:\n    def calculate(self):\n        return 1\n\ndef other():\n    return 2\n'
  const symbols = request(code, 'textDocument/documentSymbol')
  expect(symbols[0].name).toBe('Strategy')
  expect(symbols[0].children[0].name).toBe('calculate')
  expect(request(code, 'textDocument/foldingRange').length).toBeGreaterThan(0)
  expect(request(code, 'textDocument/semanticTokens/full').data.length).toBeGreaterThan(0)
  const renamed = request('value = 1\nprint(value)\n', 'textDocument/rename', { position: { line: 0, character: 2 }, newName: 'price' })
  expect(renamed.changes['file:///draft/portfolio.py']).toHaveLength(2)
})

test('browser and server contract checks agree on the same frozen examples', () => {
  const fixtures = JSON.parse(readFileSync(new URL('../../../../tests/fixtures/editor_contract.json', import.meta.url), 'utf8'))
  for (const fixture of fixtures) {
    expect(request(fixture.code, 'axile/contract', { kind: fixture.kind })).toEqual(fixture.report)
  }
})
