import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

const source = readFileSync(new URL('../../../tests/value-slider.fixture.ts', import.meta.url), 'utf8')
function sliderCases() {
  const script = `const ui=${JSON.stringify(new URL('../../', import.meta.url).pathname)};\n${source}`
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || result.stdout || String(result.error))
  return JSON.parse(result.stdout.trim())
}

test('拖动预览后提交最终指针位置，释放捕获及失焦不重复提交', () => {
  const result = sliderCases()
  expect(result.drag).toEqual({ beforeRelease: [], changes: [15, 70, 85], commits: [85] })
  expect(result.canceled).toEqual({ changes: [30, 55], commits: [55] })
  expect(result.lost).toEqual([60])
})

test('重复键盘操作读取最新草稿，keyup或失焦仅提交一次', () => {
  const result = sliderCases()
  expect(result.keyboard).toEqual({ beforeRelease: [], changes: [11, 12], commits: [12] })
  expect(result.blurred).toEqual([100])
})

test('格式化共享可访问读数和标刻，既有数值输入保持单位与即时更新', () => {
  const result = sliderCases()
  expect(result.formatted).toEqual({ aria: 'time:10', marks: 'time:0 time:50 time:100' })
  expect(result.original).toEqual({ aria: '10ms', changes: [100] })
})
