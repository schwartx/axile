/** 可读性门禁：主视图源码不得从调试载荷挖错误，也不得消费技术原文字段。 */
import { expect, test } from 'bun:test'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

const SRC_ROOT = join(import.meta.dir, '..', '..')
/** 审计/证据视图是技术原文的合法居所，默认折叠展示。 */
const ALLOWED = new Set(['history/ExecutionEvidence.tsx', 'history/executionEvidenceModel.ts', 'features/history/executionErrorAdapter.ts', 'features/history/ExecutionErrorEvidence.tsx', 'features/account/connectionTechnicalDetails.ts', 'components/ui/ErrorDetailsContent.tsx', 'lib/errorInfo.ts'])

function collect(dir: string, base = ''): string[] {
  const files: string[] = []
  for (const name of readdirSync(dir)) {
    const rel = base ? `${base}/${name}` : name
    const full = join(dir, name)
    if (statSync(full).isDirectory()) files.push(...collect(full, rel))
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\./.test(name)) files.push(rel)
  }
  return files
}

test('主视图不得从 details.debug 读取错误文本', () => {
  const offenders: string[] = []
  for (const rel of collect(SRC_ROOT)) {
    if (ALLOWED.has(rel)) continue
    const source = readFileSync(join(SRC_ROOT, rel), 'utf8')
    if (/debug\?*\.\s*error|debug\)\?\.error|\bdebug:\s*\{[^}]*error/.test(source)) offenders.push(rel)
  }
  expect(offenders).toEqual([])
})

test('主视图不得消费 technical_detail（仅审计视图可读）', () => {
  const offenders: string[] = []
  for (const rel of collect(SRC_ROOT)) {
    if (ALLOWED.has(rel)) continue
    const source = readFileSync(join(SRC_ROOT, rel), 'utf8')
    if (source.includes('technical_detail')) offenders.push(rel)
  }
  expect(offenders).toEqual([])
})
