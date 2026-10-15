/** 账户相关接口。 */
import { apiGet, apiSend } from '@/lib/api/client'
import type { CostSummary, CostTrade } from '@/features/history/costs'
import type {
  Account,
  AccountConnectionDetail,
  AccountRuntimeSync,
  AccountDashboard,
  AccountRebalancePlan,
  AccountNextRun,
  AccountControlPolicyEditorModel,
  AccountAssetSnapshot,
  AccountAssetSnapshotList,
  ExecuteRecordList,
  Message,
  PortfolioAccountList,
  TargetWeightSnapshot,
} from '@/types/api'

/** 只读最新连接观察，绝不触发 SDK。 */
export function getAccountConnection(id: number, signal?: AbortSignal): Promise<AccountConnectionDetail> {
  return apiGet(`/account/${id}/connection`, signal)
}

/** 明确操作账户连接；写请求不自动重试。 */
export function operateAccountConnection(id: number, action: 'reconnect' | 'disconnect'): Promise<AccountConnectionDetail> {
  return apiSend('POST', `/account/${id}/connection/${action}`)
}

/** 账户写入载荷；凭证只能从表单写入，绝不会存在于读取模型中。 */
export type AccountUpdatePayload = Partial<Account> & {
  account_config?: Record<string, unknown>
  feishu_key?: string | null
}

export interface AccountFeishuTestResult {
  ok: boolean
  message: string
  error_line?: number | null
  traceback?: string | null
}

/** 使用输入或已保存的 Webhook 推送样例执行结果通知卡片。 */
export function testAccountFeishu(
  id: number,
  feishuKey: string,
): Promise<AccountFeishuTestResult> {
  return apiSend<AccountFeishuTestResult>('POST', `/account/${id}/feishu/test`, {
    feishu_key: feishuKey,
  })
}

/** 用当前页面草稿运行一次样例执行通知函数。 */
export function testAccountNotificationFunction(id: number, code: string, feishuKey?: string | null, eventType: 'execution.finished' | 'supplement.cancelled' = 'execution.finished'): Promise<AccountFeishuTestResult> {
  return apiSend<AccountFeishuTestResult>('POST', `/account/${id}/notification/test`, {
    code,
    event_type: eventType,
    ...(feishuKey !== undefined ? { feishu_key: feishuKey } : {}),
  })
}

/** 读取与执行链路相同的默认飞书函数源码。 */
export async function getDefaultAccountNotification(id: number, signal?: AbortSignal): Promise<string> {
  const result = await apiGet<{ code: string }>(`/account/${id}/notification/default`, signal)
  return result.code
}

export interface SchedulePreview {
  timezone: 'Asia/Shanghai'
  evaluated_at: string
  calendar: {
    requirement: 'required' | 'not_required'
    availability: 'available' | 'unavailable' | 'not_required'
    unavailable_reason: 'uncovered' | 'read_failed' | null
    calendar_id: string | null
    label: string | null
    coverage_start: string | null
    coverage_end: string | null
  }
  next_cursor: string | null
  has_more: boolean
  items: Array<{
    scheduled_at: string
    base_scheduled_at?: string | null
    index?: number
    effective_count?: number
    is_last?: boolean
    calendar_day: string
    calendar_status: 'available_open' | 'available_closed' | 'unavailable' | 'not_required'
    action: 'execute' | 'skip'
    unavailable_reason: 'uncovered' | 'read_failed' | null
    calendar_id: string | null
    label: string | null
    reason_code: 'CALENDAR.CLOSED' | 'CALENDAR.NO_NIGHT_SESSION' | 'CALENDAR.SESSION_CLOSED' | 'BUSY' | null
  }>
}

export interface ScheduleSkipActivity {
  kind: 'schedule_skip'
  occurred_at: string
  id: number
  channel: string
  reason_code: 'CALENDAR.CLOSED' | 'CALENDAR.NO_NIGHT_SESSION' | 'CALENDAR.SESSION_CLOSED' | 'BUSY'
  calendar_day: string
  calendar_id: string
  calendar_label: string
}

export interface ActivityExecutionRecord {
  id: number | null
  execution_id: string | null
  created_at: string
  is_success: number
  status?: string | null
  task_status?: string | null
  error?: string | null
  outcome?: string | null
  outcome_reason?: string | null
  reason_code?: string | null
  execution_kind?: string | null
  symbol_results?: Record<string, { status?: string; error?: string; outcome?: string; outcome_reason?: string; reason_code?: string }>
  total_asset?: number | null
  summary: CostSummary
  duration_sec: number | null
  trade_count: number
}

export interface ExecutionActivity {
  kind: 'execution'
  occurred_at: string
  record: ActivityExecutionRecord
}

export interface ActivitySymbolRow {
  symbol: string
  summary: CostSummary
  last_time: number
  n_trades: number
  trades?: CostTrade[]
}

export interface ActivitySymbolList {
  data: ActivitySymbolRow[]
  count: number
}

export type AccountActivity = ExecutionActivity | ScheduleSkipActivity

export interface AccountActivityList {
  data: AccountActivity[]
  count: number
}

/** 仪表盘聚合：一次拿到所有账户的舰队卡数据。 */
export function getDashboard(signal?: AbortSignal): Promise<AccountDashboard> {
  return apiGet<AccountDashboard>('/account/dashboard', signal)
}

/** 当前资产与目标快照下、由服务端按渠道规则计算的可执行持仓对照。 */
export function getAccountRebalancePlan(id: number, signal?: AbortSignal): Promise<AccountRebalancePlan> {
  return apiGet<AccountRebalancePlan>(`/account/${id}/rebalance_plan`, signal)
}

/** 从交易渠道主动查询并保存最新账户资产。 */
export function refreshAccountAssets(id: number): Promise<AccountAssetSnapshot> {
  return apiSend<AccountAssetSnapshot>('POST', `/account/${id}/assets/refresh`)
}

/** 账户资产观测历史（最新在前）。 */
export function getAccountAssetSnapshots(
  id: number,
  params: { skip?: number; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AccountAssetSnapshotList> {
  const q = new URLSearchParams()
  if (params.skip != null) q.set('skip', String(params.skip))
  if (params.limit != null) q.set('limit', String(params.limit))
  const qs = q.toString()
  return apiGet<AccountAssetSnapshotList>(`/account/${id}/asset_snapshots${qs ? `?${qs}` : ''}`, signal)
}

/** 账户详情。 */
export function getAccount(id: number, signal?: AbortSignal): Promise<Account> {
  return apiGet<Account>(`/account/${id}`, signal)
}

/** 读取账户流控编辑模型；presetKey 用于无副作用预览另一预设方案。 */
export function getAccountControlPolicy(
  id: number,
  presetKey?: string,
  signal?: AbortSignal,
): Promise<AccountControlPolicyEditorModel> {
  const query = presetKey ? `?preset_key=${encodeURIComponent(presetKey)}` : ''
  return apiGet<AccountControlPolicyEditorModel>(`/account/${id}/control/policy${query}`, signal)
}

/** 账户下次调度执行时间。 */
export function getNextRun(id: number, signal?: AbortSignal): Promise<AccountNextRun> {
  return apiGet<AccountNextRun>(`/account/${id}/next_run_time`, signal)
}

/** 只读预览未来原始 Cron 触发点及其日历动作；after 严格续接其后的时间线。 */
export function previewSchedule(
  tradeChannel: string,
  cronExpr: string,
  params: { after?: string | null; limit?: number; scheduled_ats?: string[]; supplement?: { count: number; interval_minutes: number } | null } = {},
  signal?: AbortSignal,
): Promise<SchedulePreview> {
  return apiSend<SchedulePreview>('POST', '/account/schedule-preview', {
    trade_channel: tradeChannel,
    cron_expr: cronExpr,
    supplement: params.supplement ?? null,
    ...(params.scheduled_ats ? { scheduled_ats: params.scheduled_ats } : {}),
    after: params.after ?? null,
    limit: params.limit ?? 5,
  }, signal)
}

function queryString(params: Record<string, string | number | undefined | null>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value != null && value !== '') query.set(key, String(value))
  }
  const suffix = query.size ? `?${query}` : ''
  return suffix
}

/** 执行与休市跳过组成的账户活动流。 */
export function getAccountActivity(
  id: number,
  params: { skip?: number; limit?: number; since?: string; until?: string } = {},
  signal?: AbortSignal,
): Promise<AccountActivityList> {
  return apiGet<AccountActivityList>(`/account/${id}/activity${queryString(params)}`, signal)
}

/** 实时按品种汇总。 */
export function getActivitySymbols(
  id: number,
  params: { since: string; until: string; symbol?: string },
  signal?: AbortSignal,
): Promise<ActivitySymbolList> {
  return apiGet<ActivitySymbolList>(`/account/${id}/activity/symbols${queryString(params)}`, signal)
}

/** 只读账户当前组合下最近一次成功计算的执行器口径目标快照。 */
export function getAccountTargetSnapshot(id: number, signal?: AbortSignal): Promise<TargetWeightSnapshot> {
  return apiGet<TargetWeightSnapshot>(`/account/${id}/target_snapshot`, signal)
}

/** 使用真实账户上下文主动重新计算并保存目标快照。 */
export function refreshAccountTargetSnapshot(id: number): Promise<TargetWeightSnapshot> {
  return apiSend<TargetWeightSnapshot>('POST', `/account/${id}/target_snapshot/refresh`)
}

/**
 * 绩效页「全量」执行记录缓存（limit=500 全量口径，按 account 键）。
 *
 * 用于「小卡 → 回看页」的即时跳转：hover 预取写入此缓存，绩效页首帧直接
 * 复用，避免落地即骨架屏，也让金额共享元素 FLIP 有真实落点。缓存随下一次成功取数刷新。
 */
const execRecordsCache = new Map<number, ExecuteRecordList>()
const execRecordsInflight = new Map<number, Promise<ExecuteRecordList>>()

/** 读取已缓存的全量执行记录（无则 ``undefined``）。 */
export function getCachedExecuteRecords(id: number): ExecuteRecordList | undefined {
  return execRecordsCache.get(id)
}

/** 预取账户全量执行记录进缓存（hover 时调用；已缓存或在途则跳过，失败静默）。 */
export function prefetchExecuteRecords(id: number): void {
  if (execRecordsCache.has(id) || execRecordsInflight.has(id)) return
  const p = getExecuteRecords(id, { limit: 500 })
  execRecordsInflight.set(id, p)
  p.finally(() => execRecordsInflight.delete(id)).catch(() => {})
}

/** 账户执行记录（分页，默认最近 100 条）。全量口径（limit=500）结果入缓存。 */
export function getExecuteRecords(
  id: number,
  params: { skip?: number; limit?: number; execution_id?: string; record_id?: number } = {},
  signal?: AbortSignal,
): Promise<ExecuteRecordList> {
  const q = new URLSearchParams()
  if (params.skip != null) q.set('skip', String(params.skip))
  if (params.limit != null) q.set('limit', String(params.limit))
  if (params.execution_id != null) q.set('execution_id', params.execution_id)
  if (params.record_id != null) q.set('record_id', String(params.record_id))
  const qs = q.toString()
  const promise = apiGet<ExecuteRecordList>(`/account/execute_records/${id}${qs ? `?${qs}` : ''}`, signal)
  if (params.limit === 500 && params.skip == null && params.execution_id == null && params.record_id == null) {
    promise.then((r) => execRecordsCache.set(id, r)).catch(() => {})
  }
  return promise
}

/** 账户组合更换记录。 */
export function getPortfolioRecords(
  id: number,
  signal?: AbortSignal,
): Promise<PortfolioAccountList> {
  return apiGet<PortfolioAccountList>(`/account/portfolio_records/${id}`, signal)
}

/** 创建账户。 */
export function createAccount(body: Record<string, unknown>): Promise<Account> {
  return apiSend<Account>('POST', '/account/', body)
}

export function copyAccount(id: number, name: string): Promise<Account> {
  return apiSend<Account>('POST', `/account/${id}/copy`, { name })
}

/** 更新账户（局部）。 */
export function updateAccount(id: number, patch: AccountUpdatePayload): Promise<Account> {
  return apiSend<Account>('PATCH', `/account/${id}`, patch)
}

/** 删除账户。 */
export function deleteAccount(id: number): Promise<Message> {
  return apiSend<Message>('DELETE', `/account/${id}`)
}

/** 尚无同步记录时，后端以 202 + detail 返回，不能当作已同步。 */
export function parseAccountRuntimeSync(value: unknown): AccountRuntimeSync | null {
  if (!value || typeof value !== 'object') throw new Error('运行态同步响应无效')
  const record = value as Record<string, unknown>
  if (typeof record.detail === 'string' && record.status === undefined) return null
  if (!['pending', 'synchronized', 'failed'].includes(String(record.status)) ||
      !Number.isInteger(record.revision) || !Number.isInteger(record.attempts)) {
    throw new Error('运行态同步响应无效')
  }
  return value as AccountRuntimeSync
}

export async function getAccountRuntimeSync(id: number, signal?: AbortSignal): Promise<AccountRuntimeSync | null> {
  return parseAccountRuntimeSync(await apiGet<unknown>(`/account/${id}/runtime-sync`, signal))
}

export async function retryAccountRuntimeSync(id: number, signal?: AbortSignal): Promise<AccountRuntimeSync | null> {
  return parseAccountRuntimeSync(await apiSend<unknown>('POST', `/account/${id}/runtime-sync/retry`, undefined, signal))
}
