import { describe, expect, it, spyOn } from 'bun:test'
import { readReplayWindow, replayWindowQuery, validateReplayManifest, validateReplayPage } from '@/lib/api/replay'
import { replaySymbols, replayWindowRequest } from '@/features/replay/presentation'
import type { ReplayRecording, ReplayWindow } from '@/features/replay/types'

const request = { recording_id: 'rec-A', start_us: 100, end_us: 200, symbols: ['合约 A'] }
const page = () => ({ recording_id: 'rec-A', window_id: 'w', state_before_start: {}, messages: [{ schema_version: 1, recording_id: 'rec-A', capture_seq: 3, time_us: 100, topic: '/trade', data: {} }], next_cursor: null, complete: true, coverage: [{ start_us: 100, end_us: 200 }], gaps: [], context: { schema_version: 1 } })

describe('回放只读窗口边界', () => {
  it('请求绑定录制段，保留完整整数微秒和不透明游标', () => {
    const query = new URLSearchParams(replayWindowQuery(request, 'a&b'))
    expect(query.get('recording_id')).toBe('rec-A')
    expect(query.get('symbols')).toBe('合约 A')
    expect(query.get('cursor')).toBe('a&b')
    expect(() => replayWindowQuery({ ...request, start_us: 1.5 })).toThrow()
  })
  it('UTC重叠不能让其他录制段响应进入当前窗口', () => {
    expect(() => validateReplayPage({ ...page(), recording_id: 'rec-B' }, request)).toThrow('窗口已变化')
    expect(() => validateReplayPage(page(), request, 'old-window')).toThrow('窗口已变化')
  })
  it('筛选后的序号允许跳号，但不允许重复或使用不安全纳秒', () => {
    const row = page()
    row.messages.push({ ...row.messages[0], capture_seq: 7, time_us: 100 })
    expect(validateReplayPage(row, request).messages.length).toBe(2)
    row.messages[1].capture_seq = 3
    expect(() => validateReplayPage(row, request)).toThrow('顺序无效')
    row.messages[1].capture_seq = 7
    row.messages[1].time_us = Number.MAX_SAFE_INTEGER + 1
    expect(() => validateReplayPage(row, request)).toThrow()
  })
  it('范围是左闭右开，不把上界和未知版本当作可播放数据', () => {
    const row = page()
    row.messages[0].time_us = 200
    expect(() => validateReplayPage(row, request)).toThrow('超出')
    expect(() => validateReplayManifest({ account_id: 1, execution_id: 'e', recordings: [{ recording_id: 'r', coverage: [], context: { schema_version: 2 } }] })).toThrow('版本暂不支持')
  })
  it('品种目录兼容旧录制缺字段，并拒绝非字符串或无效名称', () => {
    const recording = { recording_id: 'r', coverage: [], context: { schema_version: 1 } }
    const manifest = { account_id: 1, execution_id: 'e', recordings: [recording] }
    expect(validateReplayManifest(manifest).recordings[0].symbols).toBeUndefined()
    const complete = validateReplayManifest({ ...manifest, recordings: [{ ...recording, symbols: ['A', 'X'], symbols_complete: true }] }).recordings[0]
    expect(complete.symbols).toEqual(['A', 'X'])
    expect(complete.symbols_complete).toBe(true)
    expect(validateReplayManifest({ ...manifest, recordings: [{ ...recording, symbols_complete: false }] }).recordings[0].symbols_complete).toBe(false)
    expect(() => validateReplayManifest({ ...manifest, recordings: [{ ...recording, symbols_complete: 'yes' }] })).toThrow('目录完整性')
    expect(() => validateReplayManifest({ ...manifest, recordings: [{ ...recording, symbols_complete: true }] })).toThrow('缺少名称列表')
    for (const symbols of ['A', [1], [''], ['A\nB']]) {
      expect(() => validateReplayManifest({ ...manifest, recordings: [{ ...recording, symbols }] })).toThrow('品种目录')
    }
  })
  it('只有完整目录才筛选品种，旧录制后窗仍能发现新出现的X', () => {
    const recording: ReplayRecording = { recording_id: 'rec-A', context: { schema_version: 1 }, symbols: ['A'], coverage: [] }
    const range = { start_us: 100, end_us: 200 }
    const future = { ...page(), messages: [{ ...page().messages[0], data: { symbol: 'X' } }] } as ReplayWindow
    for (const symbols_complete of [false, undefined]) {
      const selected = replayWindowRequest({ ...recording, symbols_complete }, range, 'A')
      expect(new URLSearchParams(replayWindowQuery(selected)).has('symbols')).toBe(false)
      expect(replaySymbols(validateReplayPage(future, selected), ['A'])).toEqual(['A', 'X'])
    }
    const complete = replayWindowRequest({ ...recording, symbols: ['A', 'X'], symbols_complete: true }, range, 'X')
    expect(new URLSearchParams(replayWindowQuery(complete)).get('symbols')).toBe('X')
  })
  it('固定窗口分页只恢复首页基线，后页继续同微秒序号', async () => {
    const urls: string[] = []
    const preconnect = globalThis.fetch.preconnect
    const fetchMock = spyOn(globalThis, 'fetch').mockImplementation(Object.assign(async (input: string | URL | Request) => {
      urls.push(String(input))
      const first = urls.length === 1
      const value = { ...page(), state_before_start: first ? { marker: 'initial' } : null,
        messages: [{ ...page().messages[0], capture_seq: first ? 3 : 7 }], next_cursor: first ? 'opaque&next' : null, complete: !first }
      return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }, { preconnect }))
    try {
      const value = await readReplayWindow('exec', request)
      expect(value.messages.map(event => event.capture_seq)).toEqual([3, 7])
      expect(value.state_before_start).toEqual({ marker: 'initial' })
      expect(new URL(urls[1], 'http://localhost').searchParams.get('cursor')).toBe('opaque&next')
      expect(urls.every(url => url.includes('recording_id=rec-A'))).toBe(true)
    } finally { fetchMock.mockRestore() }
  })
  it('未完成且游标循环时停止读取，避免无限拉取', async () => {
    let count = 0
    const preconnect = globalThis.fetch.preconnect
    const fetchMock = spyOn(globalThis, 'fetch').mockImplementation(Object.assign(async () => new Response(JSON.stringify({ ...page(), messages: [{ ...page().messages[0], capture_seq: ++count }], next_cursor: 'same', complete: false })), { preconnect }))
    try { await expect(readReplayWindow('exec', request)).rejects.toThrow('无法继续') }
    finally { fetchMock.mockRestore() }
    expect(count).toBe(2)
  })
})
