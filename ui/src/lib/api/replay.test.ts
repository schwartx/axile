import { afterEach, expect, mock, test, spyOn } from 'bun:test'
import * as display from '@/features/replay/rawDisplay'
import { ApiError } from './client'
import { getReplayManifest, readReplayExecution, readReplayWindow, validateReplayManifest } from './replay'
import type { ReplayManifest, ReplayWindow } from '@/features/replay/types'

const manifest: ReplayManifest = { account_id: 7, execution_id: 'E', availability: 'available', recordings: [
  { recording_id: 'R', coverage: [{ start_us: 10, end_us: 20 }], symbols: ['S'], symbols_complete: true, context: { schema_version: 2 } },
] }
const window: ReplayWindow = { recording_id: 'R', window_id: 'c', state_before_start: { recording_id: 'R', time_us: 10, state_through_seq: 0 }, messages: [], coverage: [{ start_us: 10, end_us: 20 }], gaps: [], next_cursor: null, complete: true, context: { schema_version: 2 } }
afterEach(() => { mock.restore() })

test('manifest refresh and direct window reads use V2 without probing legacy HTTP endpoints', async () => {
  const fetch = spyOn(globalThis, 'fetch').mockRejectedValue(Error('unexpected legacy HTTP request'))
  const catalog = spyOn(display, 'rawDisplayManifest').mockResolvedValue(manifest)
  const read = spyOn(display, 'rawDisplayWindow').mockResolvedValue(window)
  const signal = new AbortController().signal
  expect(await getReplayManifest('E', signal)).toBe(manifest)
  expect(await getReplayManifest('E', signal)).toBe(manifest)
  const request = { recording_id: 'R', start_us: 10, end_us: 20, symbols: ['S'] }
  expect(await readReplayWindow('E', request, signal)).toBe(window)
  expect(catalog).toHaveBeenCalledTimes(2)
  expect(catalog).toHaveBeenLastCalledWith('E', signal)
  expect(read).toHaveBeenCalledWith('E', request, signal)
  expect(fetch).not.toHaveBeenCalled()
})

test('V2 errors propagate without falling back to a legacy endpoint', async () => {
  const fetch = spyOn(globalThis, 'fetch').mockRejectedValue(Error('unexpected legacy HTTP request'))
  const error = new ApiError(404, 'execution missing')
  spyOn(display, 'rawDisplayManifest').mockRejectedValue(error)
  await expect(getReplayManifest('E')).rejects.toBe(error)
  expect(fetch).not.toHaveBeenCalled()
})

test('execution segments preserve recording identity and filter symbols only for complete catalogs', async () => {
  const read = spyOn(display, 'rawDisplayWindow').mockImplementation(async (_, request) => ({ ...window, recording_id: request.recording_id, state_before_start: { recording_id: request.recording_id } }))
  const signal = new AbortController().signal
  const parts = [
    { recording: manifest.recordings[0], range: { start_us: 10, end_us: 20 } },
    { recording: { ...manifest.recordings[0], recording_id: 'R2', symbols_complete: false }, range: { start_us: 30, end_us: 40 } },
  ]
  const result = await readReplayExecution('E', parts, 'S', signal)
  expect(result.map(part => [part.range, part.window.recording_id, part.window.state_before_start?.recording_id])).toEqual([
    [parts[0].range, 'R', 'R'], [parts[1].range, 'R2', 'R2'],
  ])
  expect(read).toHaveBeenNthCalledWith(1, 'E', { recording_id: 'R', start_us: 10, end_us: 20, symbols: ['S'] }, signal)
  expect(read).toHaveBeenNthCalledWith(2, 'E', { recording_id: 'R2', start_us: 30, end_us: 40 }, signal)
})

test('invalid windows and canceled execution reads stop before decoding', async () => {
  const read = spyOn(display, 'rawDisplayWindow').mockResolvedValue(window)
  for (const request of [{ recording_id: '', start_us: 10, end_us: 20 }, { recording_id: 'R', start_us: 1.5, end_us: 20 }, { recording_id: 'R', start_us: 20, end_us: 20 }]) {
    await expect(readReplayWindow('E', request)).rejects.toThrow('有效的整数微秒范围')
  }
  const controller = new AbortController()
  controller.abort()
  await expect(readReplayExecution('E', [{ recording: manifest.recordings[0], range: { start_us: 10, end_us: 20 } }], 'S', controller.signal)).rejects.toThrow()
  expect(read).not.toHaveBeenCalled()
})

test('manifest validation preserves integer microseconds and rejects invalid identity, coverage and symbols', () => {
  expect(validateReplayManifest({ ...manifest, execution_start_us: 1793621032000269 }).execution_start_us).toBe(1793621032000269)
  for (const value of [
    { ...manifest, account_id: null }, { ...manifest, execution_start_us: 1.5 },
    { ...manifest, recordings: [{ ...manifest.recordings[0], coverage: [{ start_us: 20, end_us: 10 }] }] },
    { ...manifest, recordings: [{ ...manifest.recordings[0], symbols: ['\n'] }] },
    { ...manifest, recordings: [{ ...manifest.recordings[0], symbols: undefined }] },
    { ...manifest, recordings: [{ ...manifest.recordings[0], context: { schema_version: 3 } }] },
  ]) expect(() => validateReplayManifest(value)).toThrow()
})
