import { Tooltip } from '@/components/ui/Tooltip'
import type { ControlFrame, ControlRule } from '@/stores/liveControl'
import { useControlQuotaWarnings } from '@/features/account/useControlQuotaWarnings'
import { quotaStageName } from '@/features/account/controlWarnings'

function usageText(rules: ControlRule[], live: boolean): string {
  const parts: string[] = []
  for (const [key, label] of [['per_minute', '分钟'], ['per_day', '今日']] as const) {
    const rule = rules.find((item) => item.rule === key)
    if (!rule || (key === 'per_minute' && rule.limit == null)) continue
    if (rule.value == null) { parts.push(`${label}用量未知`); continue }
    parts.push(`${label}已用 ${rule.value}${rule.limit == null ? '' : `/${rule.limit}`}`)
    if (key === 'per_day' && rule.remaining != null) parts.push(`剩余 ${rule.remaining}`)
  }
  const interval = rules.find((item) => item.rule === 'min_interval_ms')
  if (live && interval && interval.estimated_wait_ms > 0) parts.push(`预计等待 ${interval.estimated_wait_ms} 毫秒`)
  return parts.join(' · ')
}

/** 只展示对应范围的用量，不把合约或外层／原生操作相加。 */
export function ControlScopeUsage({ frame, operation, scope = 'account', symbol }: {
  frame: ControlFrame | undefined
  operation: string
  scope?: ControlRule['scope']
  symbol?: string
}) {
  const warnings = useControlQuotaWarnings(frame)
  const warning = scope === 'account' ? warnings.find(item => item.operation === operation) : undefined
  const rules = frame?.rules.filter((rule) => rule.operation === operation && rule.scope === scope && (symbol == null || rule.symbol === symbol)) ?? []
  const waiting = frame?.waiting.filter((request) => {
    if (scope !== 'group' && request.operation !== operation) return false
    if (symbol != null && request.symbol !== symbol) return false
    return request.hits.some((hit) => scope === 'group' ? hit.scope_key === operation : hit.scope_type === scope)
      || (scope === 'account' && request.kind === 'queue')
  }).length ?? 0
  return <span className="mt-1 block text-[12px] font-normal leading-5 text-ink-3">
    {rules.length ? usageText(rules, frame?.status === 'running') : '用量未知'}
    {waiting > 0 && <span className="ml-2 text-warn">{waiting} 个请求等待</span>}
    {warning && <span className="ml-2 font-medium text-warn">{quotaStageName(warning.stage)}{warning.stale ? ' · 数据待更新' : ''}</span>}
  </span>
}

export function ControlSymbolUsage({ frame, operation, expanded = false }: {
  frame: ControlFrame | undefined
  operation: string
  expanded?: boolean
}) {
  const symbols = [...new Set(frame?.rules.filter((rule) => rule.operation === operation && rule.scope === 'symbol' && rule.value != null).map((rule) => rule.symbol).filter((symbol): symbol is string => symbol != null) ?? [])]
  if (!expanded) return <span className="mt-1 block text-[12px] font-normal leading-5 text-ink-3">{symbols.length ? `${symbols.length} 个品种有记录 · 展开查看用量` : frame ? '暂无品种用量' : '用量未知'}</span>
  if (!symbols.length) return null
  return <div className="mt-3 space-y-2 border-t border-line pt-3">
    {symbols.map((symbol) => <div key={symbol} className="text-[13px] text-ink-2">
      {symbol}<ControlScopeUsage frame={frame} operation={operation} scope="symbol" symbol={symbol} />
    </div>)}
  </div>
}

export function ControlUsageNotice({ frame, editing = false, inline = false }: { frame: ControlFrame | undefined; editing?: boolean; inline?: boolean }) {
  const notes = [frame?.status === 'running' ? '当前实例用量' : '今日累计 · 暂无运行实例']
  if (!frame) notes.splice(0, 1, '用量未知')
  else if (frame.status === 'unknown') notes.splice(0, 1, '运行状态未知 · 保留最后已知用量')
  else if (frame.status === 'incomplete') notes.splice(0, 1, '最后已知用量')
  if (frame?.pending_policy) notes.push('已保存配置下次执行生效')
  if (editing) notes.push('配置含未保存编辑值，用量仍按已生效规则展示')
  if (frame?.stale) notes.push('数据陈旧，保留最后已知值')
  if (frame?.incomplete) notes.push('统计可能不完整')
  if (frame?.sampled_at_ms) notes.push(`${new Date(frame.sampled_at_ms).toLocaleTimeString()} 更新`)
  return <Tooltip content="统计本地放行并预占额度的尝试，下游拒绝仍计数。运行中按实例实际规则展示；无运行实例时按已保存规则展示历史累计。自然日与分钟窗口沿用账户时区。">
    <p tabIndex={0} className={`${inline ? '' : 'mt-2'} text-[12px] leading-5 ${frame?.stale || frame?.incomplete || frame?.pending_policy ? 'text-warn' : 'text-ink-3'}`}>{notes.join(' · ')}</p>
  </Tooltip>
}
