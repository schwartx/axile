import { expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

it('价格轴平移、缩放和滚轮期间锁宽，结束后适配并收回多余宽度', () => {
  const source = readFileSync(new URL('../../../tests/replay-axis-width.fixture.ts', import.meta.url), 'utf8')
  const script = `const ui=${JSON.stringify(new URL('../../', import.meta.url).pathname)};\n${source}`
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || result.stdout)
  expect(result.stdout.trim()).toBe('ok')
})
