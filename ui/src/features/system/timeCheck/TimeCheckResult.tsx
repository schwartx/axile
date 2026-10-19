import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import type { TimeCheckSnapshot } from '@/lib/api/timeCheck'
import { MOTION_LAYOUT } from '@/lib/viewTransition'
import { offsetText } from './display'

const triggers: Record<string, string> = { first: '首次检查', manual: '手动检查', automatic: '自动检查' }
const date = (value?: string | null) => value
  ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
  : '—'

/** 左栏展示最近证据；当前配置草稿不会改变历史检查结论。 */
export function TimeCheckResult({ snapshot }: { snapshot: TimeCheckSnapshot | null }) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const detailsId = useId()
  const result = snapshot?.state.result
  const running = !!snapshot?.state.running
  const alerts = Object.values(snapshot?.state.alerts ?? {})
  const ntp = result?.ntp
  const system = result?.system
  const offset = ntp?.status === 'ok' ? ntp.offset_ms : undefined
  const exceeded = offset != null && result != null && Math.abs(offset) > result.settings.threshold_ms
  const synchronized = system?.synchronized == null ? '同步状态未知' : system.synchronized ? '已同步' : '未同步'
  const enabled = system?.enabled == null ? '无法确认' : system.enabled ? '已开启' : '未开启'
  const headline = offset != null ? `本机${offset >= 0 ? '快' : '慢'}` : running ? '正在检查…' : result ? '参考时间不可用' : '尚未检查'

  return <section aria-label="检查结果" aria-live="polite" className="min-w-0 space-y-6">
    <div className="space-y-4">
      <div className="space-y-2">
        <p className="text-[14px] text-ink-2">{headline}</p>
        {offset != null && <p className={`num flex flex-wrap items-baseline gap-x-2 ${exceeded ? 'text-warn' : 'text-ink-1'}`}>
          <span className="text-[40px] font-medium leading-tight tracking-tight">{Math.abs(offset).toFixed(2)}</span>
          <span className="text-[15px] text-ink-3">ms</span>
        </p>}
        {offset != null && <p className={`text-[13px] ${exceeded ? 'text-warn' : 'text-ink-2'}`}>
          {exceeded ? '偏差超过阈值' : '偏差在阈值内'} · 允许 {result?.settings.threshold_ms} ms
        </p>}
        {running && result && <p className="text-xs text-ink-3">正在重新检查，以上为上次结果</p>}
      </div>
    </div>

    <div className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-4 border-t border-line pt-4">
      <p className="text-[13px] text-ink-3">系统自动校时</p>
      <p className={`text-right text-[14px] ${system && system.status !== 'ok' ? 'text-warn' : 'text-ink-1'}`}>
        {system ? `${enabled} · ${synchronized}` : '尚未检查'}
      </p>
    </div>

    {alerts.length > 0 && <ul className="space-y-3 border-l-2 border-warn/50 pl-3">
      {alerts.map((alert, index) => <li key={index} className="space-y-1 text-[13px]">
        <p className="break-words text-warn">{alert.message}</p>
        <p className="text-[11px] text-ink-3">{date(alert.checked_at)}</p>
      </li>)}
    </ul>}

    <dl className="space-y-3 text-[13px]">
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-4">
        <dt className="text-ink-3">参考时间源</dt>
        <dd className="break-all text-right text-ink-2">{result ? `${result.host}${result.port === 123 ? '' : `:${result.port}`}` : '—'}</dd>
      </div>
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-4">
        <dt className="text-ink-3">最近检查</dt>
        <dd className="min-w-0 space-y-1 text-right text-ink-2">
          <p>{result ? date(result.finished_at) : '—'}</p>
          {result && <p className="text-xs text-ink-3">{triggers[result.trigger]}</p>}
        </dd>
      </div>
    </dl>

    {result && <div>
      <button type="button" aria-expanded={detailsOpen} aria-controls={detailsId}
        onClick={() => setDetailsOpen(value => !value)}
        className="inline-flex cursor-pointer items-center gap-1.5 rounded text-[13px] text-ink-3 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent">
        检测详情<ChevronDown size={13} aria-hidden className={`transition-transform ${MOTION_LAYOUT} ${detailsOpen ? 'rotate-180' : ''}`} />
      </button>
      <div id={detailsId} inert={!detailsOpen} aria-hidden={!detailsOpen}
        className={`grid transition-[grid-template-rows] ${MOTION_LAYOUT} ${detailsOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="min-h-0 overflow-hidden"><div className="space-y-3 pt-3 text-xs text-ink-3">
          {ntp?.delay_ms != null && <p>往返延迟 {ntp.delay_ms.toFixed(2)} ms · {ntp.samples} 个有效样本</p>}
          {ntp?.error && <p className="break-words">{ntp.error}</p>}
          {!!ntp?.sample_errors?.length && <p className="break-words">采样说明：{ntp.sample_errors.join('；')}</p>}
          {system?.error && <p className="break-words">{system.error}</p>}
          {system?.evidence && <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all leading-relaxed">{system.evidence}</pre>}
          {ntp?.status !== 'ok' && snapshot?.state.last_success && <p>
            历史成功结果：{offsetText(snapshot.state.last_success.offset_ms)} · {snapshot.state.last_success.host} · {date(snapshot.state.last_success.checked_at)}
          </p>}
        </div></div>
      </div>
    </div>}
    {result && snapshot && result.settings.version !== snapshot.settings.version && <p className="text-xs leading-relaxed text-ink-3">
      配置已更新，等待按新配置检查。当前结果保留检查时的来源与阈值。
    </p>}
  </section>
}
