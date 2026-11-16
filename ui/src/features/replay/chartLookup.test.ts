import { expect, it } from 'bun:test'
import { replaySeekScheduler, replayTimeUpperBound } from '@/features/replay/chartLookup'

it('历史盘口二分保留同微秒最后观察，不把空段或未来当作已知', () => {
  const rows = [{ time_us: 10, seq: 1 }, { time_us: 10, seq: 2 }, { time_us: 20, seq: 3 }]
  for (const time of [0, 10, 11, 19, 20, 99]) expect(replayTimeUpperBound(rows, time) - 1).toBe(rows.findLastIndex(row => row.time_us <= time))
  expect(replayTimeUpperBound([], 10)).toBe(0)
})

it('历史二分查找只读取对数数量的时间，不反向扫描完整历史', () => {
  let reads = 0
  const rows = Array.from({ length: 65536 }, (_, time_us) => ({ get time_us() { reads++; return time_us } }))
  expect(replayTimeUpperBound(rows, 0)).toBe(1)
  expect(reads).toBeLessThanOrEqual(17)
})

it('拖动每帧 latest-wins，结束立即提交一次，取消不泄漏旧游标', () => {
  let id = 0
  const frames = new Map<number, FrameRequestCallback>(), seeks: number[] = []
  const scheduler = replaySeekScheduler(time => seeks.push(time), callback => { frames.set(++id, callback); return id }, frame => { frames.delete(frame) })
  scheduler.schedule(10); scheduler.schedule(20); scheduler.schedule(30)
  expect(frames.size).toBe(1)
  expect(seeks).toEqual([])
  const callback = frames.get(id)!
  frames.delete(id); callback(0)
  expect(seeks).toEqual([30])
  scheduler.schedule(40); scheduler.schedule(50); scheduler.flush(); scheduler.flush()
  expect(seeks).toEqual([30, 50])
  expect(frames.size).toBe(0)
  scheduler.schedule(60); scheduler.cancel(); scheduler.flush()
  expect(frames.size).toBe(0)
  expect(seeks).toEqual([30, 50])
  scheduler.schedule(70); scheduler.flush()
  expect(seeks).toEqual([30, 50, 70])
})
