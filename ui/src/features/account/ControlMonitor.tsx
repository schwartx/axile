import { useEffect, useRef, useState } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { Link } from '@/components/ui/nav'
import { Tooltip } from '@/components/ui/Tooltip'
import { useLiveControl, type ControlFrame, type ControlRule } from '@/stores/liveControl'
import { DEFAULT_CONTROL_METRICS, readControlPreferences, saveControlPreferences } from '@/features/account/controlPreferences'

const OP: Record<string, string> = { place_order: '报单', cancel_order: '撤单', cancel_order_ctp: 'CTP 原生撤单', query_order: '查单', option_exercise: '期权行权', option_abandon: '期权放弃', option_self_close: '期权自对冲', cancel_option_exercise: '撤销期权行权', cancel_option_abandon: '撤销期权放弃', cancel_option_self_close: '撤销期权自对冲' }
const operation = (key: string) => OP[key] ?? key
function controlValue(rule: ControlRule | undefined): string {
  if (!rule || rule.value == null) return '未知'
  if (rule.limit === 0) return `${rule.value} / 0（禁用）`
  return rule.limit == null ? `${rule.value}（无限制）` : `${rule.value} / ${rule.limit}`
}
function statusText(frame: ControlFrame | undefined) {
  if (!frame) return '数据未知'
  const parts = []
  if (frame.stale) parts.push('数据陈旧，保留最后已知值')
  if (frame.incomplete) parts.push('统计可能不完整：存在未完成会话或落库失败')
  if (frame.status === 'idle') parts.push('无运行实例；展示已确认历史累计')
  if (frame.status === 'disabled') parts.push('流控未启用')
  if (frame.status === 'unknown') parts.push('运行实例数据未知；保留已知值')
  if (frame.waiting_total) parts.push(`${frame.waiting_total} 个请求等待`)
  if (frame.records.some((r) => r.decision === 'blocked')) parts.push('最近有请求被阻断')
  return parts.join(' · ') || '实时数据'
}
export function ControlRules({ frame }: { frame: ControlFrame | undefined }) {
  return <div className="space-y-2 text-[13px]">
    <p className={frame?.stale || frame?.incomplete || frame?.waiting_total ? 'text-warn' : 'text-ink-3'}>{statusText(frame)}</p>
    <p className="text-ink-3">更新：{frame?.sampled_at_ms ? new Date(frame.sampled_at_ms).toLocaleTimeString() : '未知'} · {frame?.timezone ?? '时区未知'} · 自然日／固定分钟窗口</p>
    {frame?.pending_policy && <p className="text-warn">已保存配置与当前实例不同；下次执行生效，以下仍为当前实例规则。</p>}
    <div className="overflow-x-auto"><table className="w-full text-left text-[13px]">
      <thead className="text-ink-3"><tr><th className="py-2">规则</th><th>用量／上限</th><th>剩余／等待</th></tr></thead>
      <tbody>{frame?.rules.map((rule) => <tr key={rule.id} className="border-t border-line">
        <td className="py-2 pr-3">{rule.scope === 'group' ? '共享组' : rule.scope === 'symbol' ? rule.symbol : '账户'} · {operation(rule.operation)} · {rule.rule === 'per_day' ? '每日' : rule.rule === 'per_minute' ? '每分钟' : '最小间隔'}</td>
        <td className="pr-3">{rule.rule === 'min_interval_ms' ? `${rule.limit} ms` : controlValue(rule)}</td>
        <td>{rule.rule === 'min_interval_ms' ? `预计 ${rule.estimated_wait_ms} ms` : rule.value == null ? '未知' : rule.remaining == null ? '无限制' : rule.remaining}{rule.on_trigger && ` · ${rule.on_trigger === 'wait' ? '超额等待' : '超额阻断'}`}</td>
      </tr>)}</tbody>
    </table></div>
    <p className="text-ink-3">统计本地放行并预占额度的尝试，下游拒绝或失败仍计数；不是成功报撤单或成交。普通撤单不与 CTP 原生撤单相加，期权操作单列。</p>
    <p className="text-ink-3">等待时间为估计，新请求可能改变顺序；间隔按本地放行时间计算。</p>
  </div>
}
function ControlDialog({ open, frame, accountId, selected, onSelect, onClose }: { open: boolean; frame: ControlFrame | undefined; accountId: number; selected: [string, string]; onSelect: (keys: [string, string]) => void; onClose: () => void }) {
  const options = frame?.metrics ?? []
  return <CenteredModal open={open} title="账户流控" onClose={onClose} widthClassName="w-[768px]">
      <ControlRules frame={frame} />
      <h3 className="mt-4 font-semibold">等待请求（{frame?.waiting_total ?? '未知'}）</h3>
      {frame?.waiting.map((q) => <p key={q.id} className="mt-2 text-[13px]">{operation(q.operation)} · {q.symbol ?? '账户'} · 优先级 {q.priority} · 已等待 {(q.waited_ms / 1000).toFixed(1)} 秒 · {q.kind === 'quota' ? '额度等待' : '排队等待'}{q.estimated_wait_ms != null && ` · 预计 ${(q.estimated_wait_ms / 1000).toFixed(1)} 秒`} {q.hits.map((hit) => String(hit.message ?? '')).join('、')}</p>)}
      {frame?.waiting_total === 0 && <p className="mt-2 text-[13px] text-ink-3">暂无等待</p>}
      {!!frame && frame.waiting_total > frame.waiting.length && <p className="text-ink-3">仅展示前 {frame.waiting.length} 项</p>}
      <h3 className="mt-4 font-semibold">最近记录（最多 100 条）</h3>
      {frame?.records.map((r) => <p key={r.event_uid} className="mt-2 text-[13px]">{new Date(r.occurred_at_ms).toLocaleTimeString()} · {operation(r.operation)} · {r.symbol ?? '账户'} · {r.decision === 'blocked' ? '本地阻断' : '已放行'} · {({ submitted: '已提交，最终结果未知', accepted: '已提交，最终结果未知', pending: '结果未知', downstream_error: '下游异常', broker_rejected: '柜台拒绝', broker_sync_rejected: '柜台同步拒绝', broker_acknowledged: '柜台已确认', policy_blocked: '规则阻断', fetched: '查询已返回' } as Record<string, string>)[r.outcome] ?? '结果未知'} {String((r.metadata.account_control_hit as Record<string, unknown> | undefined)?.message ?? '')}</p>)}
      {frame?.records.length === 0 && <p className="mt-2 text-[13px] text-ink-3">暂无记录</p>}
      <h3 className="mt-4 font-semibold">账户卡关注指标</h3>
      <div className="my-2 flex flex-wrap gap-2">{selected.map((value, index) => <select key={index} aria-label={`关注指标 ${index + 1}`} value={value} onChange={(event) => { const next: [string, string] = [...selected]; next[index] = event.target.value; onSelect(next) }} className="max-w-full rounded border border-line bg-surface px-2 py-1">
        {options.length ? options.map((option) => <option key={option.id} value={option.id} disabled={selected[1 - index] === option.id}>{option.label?.startsWith('今日') ? option.label : `今日${operation(option.operation)}`}</option>) : <option value={value}>{DEFAULT_CONTROL_METRICS[index] === value ? index ? '今日撤单' : '今日报单' : value}</option>}
      </select>)}</div>
      <p className="mb-4 text-[13px] text-ink-3">恰好两项；仅保存在此浏览器此账户，不修改交易限额。</p>
      <Link to={`/accounts/${accountId}/edit/control`} onClick={onClose} className="text-accent">打开当前账户流控设置 ›</Link>
  </CenteredModal>
}
export function AccountControlSummary({ accountId }: { accountId: number }) {
  const frame = useLiveControl((s) => s.frames[accountId])
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<[string, string]>(() => readControlPreferences(accountId))
  const close = useRef(() => setOpen(false)).current
  useEffect(() => {
    if (!frame || !frame.metrics.length || selected.every((id, index) => id === DEFAULT_CONTROL_METRICS[index])) return
    if (selected.some((id) => !frame.metrics.some((m) => m.id === id))) {
      const next: [string, string] = [...DEFAULT_CONTROL_METRICS]
      setSelected(next); saveControlPreferences(accountId, next)
    }
  }, [frame, selected, accountId])
  return <>
    <Tooltip content="今日本地放行并预占额度的尝试数；下游失败仍计数。点击查看流控详情。">
    <button
      type="button"
      onClick={(event) => { event.stopPropagation(); setOpen(true) }}
      className="group flex min-w-0 max-w-full flex-none cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 text-left text-[14px]"
    >
      <span className="flex-none text-ink-3">流控</span>
      <span data-config-value className="num min-w-0 font-medium text-ink-1 group-hover:underline">
        {selected.map((id, index) => {
          const metric = frame?.metrics.find((m) => m.id === id)
          const label = metric ? operation(metric.operation) : index ? '撤单' : '报单'
          return `${label} ${controlValue(metric).replaceAll(' / ', '/')}`
        }).join(' · ')}
      </span>
      {frame?.waiting_total ? <span className="flex-none text-warn">等待</span>
        : frame?.incomplete ? <span className="flex-none text-warn">不完整</span>
        : frame?.stale ? <span className="flex-none text-warn">陈旧</span>
        : frame?.records.some((r) => r.decision === 'blocked') ? <span className="flex-none text-warn">部分请求阻断</span> : null}
    </button>
    </Tooltip>
    <ControlDialog open={open} frame={frame} accountId={accountId} selected={selected} onSelect={(next) => { setSelected(next); saveControlPreferences(accountId, next) }} onClose={close} />
  </>
}
