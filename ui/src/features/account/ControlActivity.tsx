import { useId, useState } from 'react'
import type { ControlFrame } from '@/stores/liveControl'

const OUTCOME: Record<string, string> = {
  submitted: '已提交，最终结果未知', accepted: '已提交，最终结果未知', pending: '结果未知',
  downstream_error: '下游异常', broker_rejected: '柜台拒绝', broker_sync_rejected: '柜台同步拒绝',
  broker_acknowledged: '柜台已确认', policy_blocked: '规则阻断', fetched: '查询已返回',
}
const priority = (value: number) => ({ 0: '最优先', 10: '高', 20: '中', 100: '普通' }[value] ?? `优先级 ${value}`)
function hitMessage(hit: unknown): string {
  if (hit == null || typeof hit !== 'object' || !('message' in hit)) return ''
  return typeof hit.message === 'string' ? hit.message : ''
}
function recordReason(metadata: Record<string, unknown>): string {
  const direct = hitMessage(metadata.account_control_hit)
  const waits = metadata.account_control_wait_hits
  const waitMessages = Array.isArray(waits) ? waits.map(hitMessage) : []
  const error = typeof metadata.error_msg === 'string' ? metadata.error_msg
    : typeof metadata.error_id === 'number' ? `柜台错误码 ${metadata.error_id}` : ''
  return [...new Set([direct, ...waitMessages, error].filter(Boolean))].join('；')
}

/** 等待区默认折叠，标题显示总数；未知与空队列分别表达。 */
export function ControlWaitingSection({ frame, operationNames }: { frame: ControlFrame | undefined; operationNames: Record<string, string> }) {
  const unknown = !frame || frame.status === 'unknown' || frame.status === 'incomplete'
  const [open, setOpen] = useState(false)
  const id = useId()
  return <div className="mt-4">
    <button type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open} aria-controls={id} className="flex w-full cursor-pointer items-center gap-3 border-0 bg-transparent py-2 text-left">
      <span className={`text-[13px] font-semibold ${frame?.waiting_total ? 'text-warn' : 'text-ink-2'}`}>等待请求({unknown ? '未知' : frame?.waiting_total})</span>
      <span className="h-px flex-1 bg-line" /><span className="text-[13px] text-ink-3" aria-hidden>{open ? '⌄' : '›'}</span>
    </button>
    <div inert={!open} aria-hidden={!open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
        <div id={id} className="mt-1 rounded-[12px] border border-line bg-surface px-4 py-3">
          <ControlWaitingList frame={frame} operationNames={operationNames} />
        </div>
      </div>
    </div>
  </div>
}

/** 页面和弹窗共享等待内容，展示方式由各自的容器决定。 */
export function ControlWaitingList({ frame, operationNames }: { frame: ControlFrame | undefined; operationNames: Record<string, string> }) {
  const unknown = !frame || frame.status === 'unknown' || frame.status === 'incomplete'
  return <div role="region" aria-label="等待请求">
    {frame?.stale && <p className="mb-2 text-[12px] text-warn">数据陈旧，以下为最后已知等待状态。</p>}
    {frame?.waiting.map((request) => <div key={request.id} className="border-t border-line py-2 first:border-t-0 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[13px]">
        <span className="font-semibold text-ink-1">{operationNames[request.operation] ?? '其他操作'}</span>
        <span className="text-ink-2">{request.symbol ?? '账户'}</span>
        <span className="text-warn">{request.kind === 'quota' ? '额度等待' : '排队等待'}</span>
        <span className="num text-ink-3">已等待 {(request.waited_ms / 1000).toFixed(1)} 秒</span>
        <span className="text-ink-3">{priority(request.priority)}</span>
        {request.estimated_wait_ms != null && <span className="num text-ink-3">预计再等 {(request.estimated_wait_ms / 1000).toFixed(1)} 秒</span>}
      </div>
      {request.hits.length > 0 && <p className="mt-1 break-words text-[12px] leading-5 text-ink-3">{request.hits.map(hitMessage).filter(Boolean).join('；')}</p>}
    </div>)}
    {!frame?.waiting.length && <p className="text-[13px] text-ink-3">{unknown ? '等待状态未知' : frame.stale ? '最后一次快照中暂无等待请求' : '暂无等待请求'}</p>}
    {!!frame && frame.waiting_total > frame.waiting.length && <p className="mt-2 text-[12px] text-ink-3">共 {frame.waiting_total} 个等待请求，显示前 {frame.waiting.length} 项</p>}
    {!!frame?.waiting.length && <p className="mt-2 text-[12px] text-ink-3">预计时间仅供参考，新请求和配置变化可能改变放行时间。</p>}
  </div>
}

/** 最近记录沿用页面折叠区样式，避免执行期间的列表挤占配置空间。 */
export function ControlRecentRecords({ frame, operationNames }: { frame: ControlFrame | undefined; operationNames: Record<string, string> }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return <div className="mt-4">
    <button type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open} aria-controls={id} className="flex w-full cursor-pointer items-center gap-3 border-0 bg-transparent py-2 text-left">
      <span className="text-[13px] font-semibold text-ink-2">最近记录</span>
      <span className="text-[12px] text-ink-3">{frame && frame.status !== 'unknown' ? `${frame.records.length} 条` : '未知'}</span>
      <span className="h-px flex-1 bg-line" /><span className="text-[13px] text-ink-3" aria-hidden>{open ? '⌄' : '›'}</span>
    </button>
    <div inert={!open} aria-hidden={!open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
        <div id={id} role="region" aria-label="最近记录" className="mt-1 rounded-[12px] border border-line bg-surface px-4 py-3">
          {frame?.stale && <p className="mb-2 text-[12px] text-warn">数据陈旧，保留最后已知记录。</p>}
          {frame?.incomplete && <p className="mb-2 text-[12px] text-warn">统计可能不完整，记录不保证覆盖异常退出前的全部请求。</p>}
          {frame?.records.map((record) => <div key={record.event_uid} className="border-t border-line py-2 first:border-t-0 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[13px]">
              <span className="num text-[12px] text-ink-3">{new Date(record.occurred_at_ms).toLocaleString()}</span>
              <span className="font-semibold text-ink-1">{operationNames[record.operation] ?? '其他操作'}</span>
              <span className="text-ink-2">{record.symbol ?? '账户'}</span>
              <span className={record.decision === 'blocked' ? 'text-warn' : 'text-ink-3'}>{record.decision === 'blocked' ? '本地阻断' : '已放行'}</span>
              <span className={['downstream_error', 'broker_rejected', 'broker_sync_rejected'].includes(record.outcome) ? 'text-warn' : 'text-ink-3'}>{OUTCOME[record.outcome] ?? '结果未知'}</span>
            </div>
            {recordReason(record.metadata) && <p className="mt-1 break-words text-[12px] leading-5 text-ink-3">{recordReason(record.metadata)}</p>}
          </div>)}
          {!frame?.records.length && <p className="text-[13px] text-ink-3">{!frame || frame.status === 'unknown' ? '记录未知' : '暂无记录'}</p>}
          <p className="mt-2 text-[12px] text-ink-3">最近最多 100 条；本地放行仍计入额度，后续拒绝或异常不回退计数。</p>
        </div>
      </div>
    </div>
  </div>
}
