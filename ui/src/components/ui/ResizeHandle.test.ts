import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

const source = readFileSync(new URL('../../../tests/resize-handle.fixture.ts', import.meta.url), 'utf8')
function cases() {
  const script = `const ui=${JSON.stringify(new URL('../../', import.meta.url).pathname)};\n${source}`
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || result.stdout || String(result.error))
  return JSON.parse(result.stdout.trim())
}

test('拖动只提交最终坐标一次，取消或丢失捕获会结束拖动且不误提交', () => {
  const result = cases()
  expect(result.drag).toEqual({ changes: [40, 60], commits: [75], resizing: [true, false] })
  expect(result.cancel).toEqual({ changes: [40], commits: [], resizing: [true, false] })
  expect(result.lost).toEqual({ commits: [], resizing: [true, false] })
  expect(result.secondary).toEqual([])
})

test('支持相反占比方向、宽度坐标、键盘边界与重置，禁用后不能操作', () => {
  const result = cases()
  expect(result.keys).toEqual([55, 45, 20, 80])
  expect(result.resets).toBe(1)
  expect(result.width).toEqual([30, 55])
  expect(result.disabled).toEqual({ changes: [], commits: [], resets: 0, tabIndex: -1 })
})


test('回放保留明细与图表的可用高度，窗口变小后夹紧，恢复窗口不丢失选择', () => {
  const { stage } = cases()
  expect(stage.initialHeight).toBe(128)
  expect(stage.limitedHeight).toBe(655)
  expect(stage.savedHeight).toBe('655')
  expect(stage.smallHeight).toBeCloseTo(200 / 3)
  expect(stage.restoredHeight).toBe(655)
  expect(stage.resetHeight).toBe(128)
})
