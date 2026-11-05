import { expect, test } from 'bun:test'
import { replayRecordingPatch } from '@/features/replay/accountSetting'

test('旧账户默认关闭，不在保存其他设置时顺带写入开关', () => {
  expect(replayRecordingPatch(false)).toEqual({})
})

test('开启只提交录制开关，不改变其他执行设置', () => {
  expect(replayRecordingPatch(true)).toEqual({ replay_recording_enabled: true })
})

test('显式关闭已有录制设置保留 false', () => {
  expect(replayRecordingPatch(false, true)).toEqual({ replay_recording_enabled: false })
})

test('未改变已保存的值不产生 PATCH 字段', () => {
  expect(replayRecordingPatch(true, true)).toEqual({})
  expect(replayRecordingPatch(false, false)).toEqual({})
})
