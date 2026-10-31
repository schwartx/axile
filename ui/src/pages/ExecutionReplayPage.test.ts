import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

const source = readFileSync(new URL('../../tests/replay-page.fixture.ts', import.meta.url), 'utf8')
function pageCase(scenario: string) {
  const script = `const ui=${JSON.stringify(new URL('../', import.meta.url).pathname)}; const scenario=${JSON.stringify(scenario)};\n${source}`
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || result.stdout || String(result.error))
  return JSON.parse(result.stdout.trim())
}

test('跨窗口加载期间隐藏，加载完成与返回页面都保持暂停', () => {
  const result = pageCase('hidden')
  expect(result.hiddenPlaying).toBe(false)
  expect(result.playing).toBe(false)
  expect(result.cursor).toContain('08:00:20.000000')
})

test('隐藏后在新窗完成前返回，也不恢复先前的自动续播', () => {
  const result = pageCase('hidden-return-before-load')
  expect(result.hiddenPlaying).toBe(false)
  expect(result.playing).toBe(false)
  expect(result.cursor).toContain('08:00:20.000000')
})

test('没有主动暂停时，下一窗口读取完成后继续播放', () => {
  const result = pageCase('continue')
  expect(result.playing).toBe(true)
  expect(result.cursor).toContain('08:00:20.000000')
})

test('下一窗读取中显式定位回缓存首窗，仍应用新定位并丢弃迟到响应', () => {
  const result = pageCase('cached-seek')
  expect(result.initial).toContain('08:00:10.000000')
  expect(result.cursor).toContain('08:00:00.000000')
  expect(result.afterLateResponse).toBe(result.cursor)
  expect(result.afterNextSeek).toContain('08:00:05.000000')
})

test.each(['drag', 'duration'])('跨窗读取中主动%s，响应到达后仍保持暂停', action => {
  const result = pageCase(`pause-${action}`)
  expect(result.pausedBeforeLoad).toBe(true)
  expect(result.playingAfterLoad).toBe(false)
  if (action === 'drag') expect(result.seekLabel).toContain('准备定位 1970-01-01 08:00:25.000000')
})

test('首窗读取失败后仍能定位后窗，播放及步进不执行失效状态', () => {
  const result = pageCase('missing')
  expect(result.failed).toEqual({ seek: true, playDisabled: true, nextDisabled: false })
  expect(result.recovered).toBe(true)
  expect(result.orders).toBe(true)
})

test('读取失败时在原窗口内改定位也重新请求，而非只改失效游标', () => {
  const result = pageCase('missing-seek')
  expect(result.recovered).toBe(true)
  expect(result.orders).toBe(true)
  expect(result.cursor).toContain('08:00:00.500000')
})

test('实际订单列表及详情区分归属，冲突保留执行身份且不误计本次', () => {
  const result = pageCase('ownership')
  expect(result.O1.row).toContain('其他执行')
  expect(result.O1.detail).toContain('其他执行 · 执行 E1')
  expect(result.O2.row).toContain('本次执行')
  expect(result.O2.detail).toContain('本次执行 · 执行 E2')
  expect(result.M.row).toContain('人工')
  expect(result.M.detail).toContain('归属： 人工')
  expect(result.U.row).toContain('归属未知')
  expect(result.C.row).toContain('归属未知')
  expect(result.C.detail).toContain('归属未知 · 执行 E2')
})
