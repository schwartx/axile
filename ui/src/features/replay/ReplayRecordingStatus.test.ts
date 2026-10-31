import { expect, it } from 'bun:test'
import { spawnSync } from 'node:child_process'
import type { ReplayStatus } from '@/lib/api/replay'

function renderStatus(data: ReplayStatus | null) {
  const ui = new URL('../../', import.meta.url).pathname
  const source = `import { mock } from 'bun:test';
    import { createElement } from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    mock.module(${JSON.stringify(ui + 'lib/hooks/usePolling.ts')}, () => ({ usePolling: () => ({ data: ${JSON.stringify(data)}, loading: false, error: null, refresh() {} }) }));
    mock.module(${JSON.stringify(ui + 'components/ui/ErrorNotice.tsx')}, () => ({ ErrorNotice: () => null }));
    const { ReplayRecordingStatus } = await import(${JSON.stringify(ui + 'features/replay/ReplayRecordingStatus.tsx')});
    console.log(renderToStaticMarkup(createElement(ReplayRecordingStatus, { accountId: 7, savedEnabled: false })));`
  const result = spawnSync(process.execPath, ['-e', source], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || String(result.error))
  return result.stdout
}

it('控制确认未知时只报状态异常和待生效，不声称录制已停止', () => {
  const html = renderStatus({ desired: false, state: 'error', config_version: 2, effective_version: null,
    error: '录制控制状态待确认；交易连接未改变' })
  expect(html).toContain('录制状态异常')
  expect(html).toContain('设置尚待生效')
  expect(html).toContain('录制控制状态待确认；交易连接未改变')
  expect(html).not.toContain('记录已中断')
  expect(html).not.toContain('已关闭')
})

it('没有运行态仍为未知，明确关闭且版本一致才显示已关闭', () => {
  expect(renderStatus(null)).toContain('实际状态未知')
  const closed = renderStatus({ desired: false, state: 'disabled', config_version: 2, effective_version: 2 })
  expect(closed).toContain('已关闭')
  expect(closed).not.toContain('设置尚待生效')
})

it('关闭控制尚待生效时使用中性文案，不反向宣称正在启用', () => {
  const html = renderStatus({ desired: false, state: 'starting', config_version: 2, effective_version: null })
  expect(html).toContain('录制设置待生效')
  expect(html).toContain('设置尚待生效')
  expect(html).not.toContain('正在启用录制')
  expect(html).not.toContain('已关闭')
})

it('等待旧录制释放时显示待生效，不能显示正在记录或永久故障', () => {
  const html = renderStatus({ desired: true, state: 'starting', config_version: 3, effective_version: null,
    reason: '等待上一段录制结束', recording_id: 'pending-recording' })
  expect(html).toContain('录制设置待生效')
  expect(html).toContain('设置尚待生效')
  expect(html).toContain('等待上一段录制结束')
  expect(html).not.toContain('正在记录')
  expect(html).not.toContain('录制状态异常')
  const ready = renderStatus({ desired: true, state: 'recording', config_version: 3, effective_version: 3,
    recording_id: 'pending-recording' })
  expect(ready).toContain('正在记录')
  expect(ready).not.toContain('设置尚待生效')
  expect(ready).not.toContain('等待上一段录制结束')
})

it('旧实例仍在录制而新实例等待时，聚合录制状态仍保留待生效标记', () => {
  const html = renderStatus({ desired: true, state: 'recording', config_version: 1, effective_version: null })
  expect(html).toContain('正在记录')
  expect(html).toContain('设置尚待生效')
  expect(html).not.toContain('已关闭')
})
