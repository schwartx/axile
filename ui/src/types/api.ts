/**
 * 手写 API DTO —— 对齐后端 `axile/server/db/models` 的 *Public 模型。
 *
 * 这是前端的类型真源（后端未生成 openapi.d.ts）。字段命名保持 snake_case
 * 与后端一致，避免映射层。
 */

/** 运行时注册的交易渠道标识。 */
export type TradeChannel = string


/** 算法引用。 */
export interface AlgorithmRef {
  method: string
  params: Record<string, unknown>
}

/** 渠道账户配置字段。 */
export interface ChannelAccountFieldConstraints {
  endpoint?: {
    scheme: 'required' | 'optional' | 'forbidden'
    allowed_schemes: Array<'tcp' | 'http' | 'https' | 'ftp' | 'ws' | 'wss'>
    port: 'required' | 'optional'
    allow_path: boolean
  } | null
  number?: {
    gt?: number | null
    gte?: number | null
  } | null
}

export interface ChannelAccountFieldClipboard {
  role: 'trading' | 'market-data' | 'rpc' | 'proxy'
  group?: string | null
}

export interface ChannelAccountField {
  name: string
  label: string
  kind: 'text' | 'identifier' | 'secret' | 'endpoint' | 'directory' | 'money' | 'boolean' | 'select'
  width: 'half' | 'full'
  required: boolean
  placeholder?: string
  help?: string
  default?: unknown
  options?: Array<{ value: string; label: string; description?: string }>
  presentation?: 'segmented' | 'conditional_reveal'
  visible_when?: { field: string; equals: unknown }
  constraints?: ChannelAccountFieldConstraints | null
  clipboard?: ChannelAccountFieldClipboard | null
}

export interface DirectoryEntry {
  name: string
  path: string
}

export interface DirectoryListing {
  path: string | null
  parent: string | null
  entries: DirectoryEntry[]
}

/** 单个渠道的依赖可用性，对应 `ChannelCapabilityPublic`。 */
export interface ChannelCapability {
  connection_validation?: { steps: Array<{ id: string; label: string }>; notice: string } | null
  channel: TradeChannel
  label: string
  description: string
  icon: string
  /** 该渠道可选依赖是否已全部安装。 */
  available: boolean
  /** 尚未安装的第三方包名；`available` 为 true 时为空。 */
  missing_packages: string[]
  /** 安装该渠道依赖所用 extra 名（`uv sync --extra <extra>`）。 */
  install_extra: string | null
  calendar: {
    calendar_id: string
    label: string
  } | null
  portfolio: {
    market_label: string
    example_symbols: string[]
  }
  market: string
  schedule: {
    kind: 'continuous' | 'cn_stock' | 'cn_futures'
    windows?: { start: string; end: string }[]
    night?: {
      label: string
      range_label: string
      close: string[]
      m15: string[]
      m60: string[]
    } | null
  }
  currency: string
  units: {
    quantity_kind: 'share' | 'contract' | 'base_asset' | 'custom'
    quantity_label: string
    quantity_max_decimals: number
    price_label: string
    notional_label: string
  }
  ui: {
    account_connect_lead: string
    leverage_title: string
    leverage_note: string
    long_leverage_label: string
    short_leverage_label: string
    show_short_leverage: boolean
    position_value_label?: string
  }
  defaults: {
    account_control_preset: string
    long_leverage: number
    short_leverage: number
    execution_timeout: number
    trade_algorithm: AlgorithmRef
    empty_positions_algorithm: AlgorithmRef | null
  }
  leverage: {
    min: number
    max: number
    step: number
  }
  account_form: {
    fields: ChannelAccountField[]
    notices: Array<{ tone: 'info' | 'warning'; text: string }>
  }
}

/** 算法适用槽位：`trade` 主交易 / `empty` 清仓，对齐后端 `AlgorithmSlot`。 */
export type AlgorithmSlot = 'trade' | 'empty'

/** 单个已注册算法的元数据，对应后端 `AlgorithmPublic`。 */
export interface AlgorithmInfo {
  name: string
  label: string
  description: string
  default_params: Record<string, unknown>
  params_schema: Record<string, unknown>
  /** 支持的渠道；`null` 表示全渠道通用。 */
  channels: TradeChannel[] | null
  /** 适用槽位；`null` 表示两槽通用，空数组表示两槽都不适用。 */
  slots: AlgorithmSlot[] | null
  /** 是否为内置默认算法。 */
  builtin: boolean
}

/* ============================ 账户 ============================ */

/** 账户读取信息；连接凭证只暴露已配置状态，绝不回传原文。 */
export interface AccountRuntimeSync {
  status: 'pending' | 'synchronized' | 'failed'
  revision: number
  attempts: number
  last_error: string | null
  last_attempt_at: string | null
  synchronized_at: string | null
}

export interface AccountConnection {
  status: 'unknown' | 'ok' | 'error'
  observed_at: string | null
  message: string | null
  label?: string | null
}

export interface ConnectionPresentation {
  title?: string | null
  summary?: string | null
  sections?: { title?: string | null; text?: string | null; fields?: { label: string; value: string }[]; steps?: string[] }[]
  actions?: { label: string; intent: string }[]
}

export interface AccountConnectionDetail extends AccountConnection {
  error: Record<string, unknown> | null
  presentation?: ConnectionPresentation | null
}

export interface Account {
  connection?: AccountConnection
  copied_from_account_id?: number | null
  copied_from_account_name?: string | null
  runtime_sync?: AccountRuntimeSync | null
  backtest_weight_type?: 'ts' | 'cs'
  backtest_fee_rate?: number
  id: number | null
  name: string
  market: string
  trade_channel: TradeChannel
  account_control_preset: string
  account_control_override: AccountControlOverride | null
  account_configured: boolean
  connection_values?: Record<string, unknown>
  is_started: boolean
  cron_expr: string
  supplement?: { count: number; interval_minutes: number } | null
  has_pending_supplement?: boolean
  remark: string | null
  brokerage: string
  weight_precision: number
  long_leverage: number | null
  short_leverage: number | null
  algorithm: Record<string, unknown>
  empty_positions_algorithm: Record<string, unknown> | null
  trade_rules: Record<string, unknown> | null
  forbidden_symbols: string[] | null
  risk_symbols: string[] | null
  feishu_configured: boolean
  execution_notification_code: string | null
  execution_notification_status: 'none' | 'default' | 'function'
  notification_state: {
    last_success_at: string | null
    execution_id: string | null
    last_attempt_event_type?: string | null
    last_attempt_at?: string | null
    last_attempt_execution_id?: string | null
    last_attempt_ok?: boolean | null
    last_attempt_error?: string | null
  } | null
  portfolio_id: number | null
  write_empty_record: number | null
  /** 执行层总超时（秒）。一次执行跑满该额度即硬中断（不撤单），与算法级 `max_wait_seconds` 不同层。 */
  execution_timeout: number
  updated_at: string
  created_at: string
}

export interface PerformanceSettings {
  backtest_weight_type: 'ts' | 'cs'
  backtest_fee_rate: number
}

export interface PerformancePoint {
  account_equity?: number | null
  date: string
  observed_at?: string
  record_id?: number | null
  execution_id?: string | null
  account_return: number | null
  portfolio_return: number | null
  account_daily_return: number | null
  portfolio_daily_return: number | null
  target_portfolio_return?: number | null
  target_portfolio_daily_return?: number | null
  sizing_fallback?: boolean
  difference: number | null
}

export interface AccountPerformance {
  backtest_included: boolean
  settings: PerformanceSettings
  engine_version: string
  range: '30' | '90' | 'all'
  baseline: string | null
  end: string | null
  record_count: number
  observation_count: number
  used_record_count: number
  target_used_record_count?: number
  invalid_asset_count: number
  skips: { count: number; missing_target: number; missing_ticks: number; first_time: string | null; last_time: string | null } | null
  target_skips?: AccountPerformance['skips']
  sizing_fallback_count?: number | null
  sizing_fallback_ranges?: Array<{ start: string; end: string; count: number }>
  points: PerformancePoint[]
  executions?: import('@/lib/api/performance').CostExecutionRow[]
  bindings: Array<{ time: string; portfolio_id: number | null }>
  calendar: PerformanceCalendar
}

export interface CalendarRange { start: string; end: string }
export interface PerformanceCalendar {
  status: 'available' | 'partial' | 'unavailable' | 'not_required'
  calendar_id: string | null
  label: string | null
  closed_ranges: CalendarRange[]
  unavailable_ranges: CalendarRange[]
}

export type AccountControlTrigger = 'wait' | 'block'

export interface AccountControlRule {
  limit: number
  on_trigger: AccountControlTrigger
}

export interface AccountControlRuleOverride {
  limit?: number | null
  on_trigger?: AccountControlTrigger | null
  /** 为真时显式解除该规则（解析为不限制），与 limit/on_trigger 互斥；min_interval_ms 不允许。 */
  unlimited?: boolean
}

export interface AccountControlScope {
  per_minute?: AccountControlRule | null
  per_day?: AccountControlRule | null
  min_interval_ms?: AccountControlRule | null
}

export interface AccountControlScopeOverride {
  per_minute?: AccountControlRuleOverride | null
  per_day?: AccountControlRuleOverride | null
  min_interval_ms?: AccountControlRuleOverride | null
}

export interface AccountControlOperationPolicy {
  priority: number
  account: AccountControlScope
  symbol?: AccountControlScope | null
}

export interface AccountControlOperationOverride {
  priority?: number | null
  account?: AccountControlScopeOverride | null
  symbol?: AccountControlScopeOverride | null
}

export interface AccountControlOverride {
  timezone?: string | null
  operations: Record<string, AccountControlOperationOverride>
  groups: Record<string, AccountControlScopeOverride>
}

export interface AccountControlPolicy {
  timezone: string
  operations: Record<string, AccountControlOperationPolicy>
  groups: Record<string, AccountControlScope>
}

export interface AccountControlPresetOption {
  key: string
  display_name: string
  description: string
}

export interface AccountControlOperationMeta {
  key: string
  display_name: string
  description: string
  category: string
  groups: string[]
}

export interface AccountControlGroupMeta {
  key: string
  display_name: string
  description: string
}

export interface AccountControlPolicyEditorModel {
  preset_key: string
  preset_display_name: string
  compatible_presets: AccountControlPresetOption[]
  timezone_display_name: string
  override: AccountControlOverride | null
  preset_policy: AccountControlPolicy
  effective_policy: AccountControlPolicy
  operations: AccountControlOperationMeta[]
  groups: AccountControlGroupMeta[]
}

/** 仪表盘聚合中的单账户项，对应 `AccountDashboardItemPublic`。 */
export interface AccountDashboardItem {
  connection?: AccountConnection
  account_id: number
  name: string
  remark: string | null
  market: string
  trade_channel: TradeChannel
  is_started: boolean
  portfolio_id: number | null
  is_scheduled: boolean
  next_run_time: string | null
  total_asset: number
  currency: string
  holdings_count: number
  /** 各持仓市值绝对值降序（最多 12），用于敞口条。 */
  position_weights: number[]
  /** 已发布的全区间绩效；金额、日收益与曲线同版。 */
  performance?: PerformanceSummary
  /** 最近一次账户资产观测时间；无快照为 null。 */
  /** 当前 total_asset 的真实资产观测时间；null 表示尚未查询，performance 为独立历史绩效。 */
  asset_observed_at?: string | null
  /** 该观测日之前的绩效日末权益；无观测或无更早绩效时为 null。 */
  previous_close?: number | null
  /** 最近一次执行是否成功（1/0）；无记录为 null。 */
  last_is_success: number | null
  last_exec_at: string | null
  /** 最近一次执行器输出状态（SUCCEEDED/BLOCKED/FAILED 等）；无记录为 null。 */
  last_output_status?: string | null
  /** 持仓相对目标待调整品种数；缺快照或目标为 null，不无证推定 0。 */
  off_symbol_count?: number | null
  /** 当前在途执行标识（取自后端并发锁，唯一真源）；无在途执行为 null。 */
  running_execution_id?: string | null
  /** 在途执行种类（rebalance/clear_positions 等）；无在途执行为 null。 */
  running_kind?: string | null
  /** 在途执行阶段（triggered/snapshot/planning/executing/settling/queued）；无在途执行为 null。 */
  running_phase?: string | null
  /** queued | running | terminating；无在途为 null。 */
  running_status?: 'queued' | 'running' | 'terminating' | null
  /** running 背后排队的下一张票；没有 pending 为 null。 */
  pending_execution_id?: string | null
  pending_kind?: string | null
}

export interface PerformanceSummary {
  snapshot_id: string | null
  status: 'empty' | 'pending' | 'ready' | 'stale' | 'failed'
  computed_at: string | null
  observed_at: string | null
  account_equity: number | null
  account_daily_return: number | null
  points: PerformancePoint[]
}

/** `GET /account/dashboard` 响应。 */
export interface AccountDashboard {
  data: AccountDashboardItem[]
}

/** 服务端按渠道可执行规则计算的账户持仓对照。 */
export interface AccountRebalancePlanRow {
  symbol: string
  current_weight: number
  target_weight: number
  current_quantity: number | null
  target_quantity: number | null
  action: 'aligned' | 'increase' | 'reduce' | 'open' | 'close' | 'flip'
  side: 'buy' | 'sell' | 'none'
  aligned: boolean
}

export interface AccountRebalancePlan {
  rows: AccountRebalancePlanRow[]
  off_symbol_count: number | null
  observed_at: string | null
  target_calculated_at: string | null
}

/** `GET /account/{id}/next_run_time`，对应 `AccountNextRunPublic`。 */
export interface AccountNextRun {
  account_id: number
  is_scheduled: boolean
  next_run_time: string | null
  /** 服务端按北京时间计算的未来执行时刻，最多三项、升序排列。 */
  next_run_times: string[]
  /** 按交易日历过滤后，未来真正会执行的时刻。 */
  next_execution_times: string[]
}

/* ============================ 组合 ============================ */

/** 组合完整信息，对应 `PortfolioPublic`。 */
export interface Portfolio {
  id: number | null
  name: string
  market: string
  description: string | null
  custom_calc_py_code: string
  status: string | null
  tag: string | null
  updated_at: string
  created_at: string
  /** 当前绑定该组合的账户 ID（可空）。 */
  account_id: number | null
}

/** 轻量组合信息，对应 `PortfolioLitePublic`（列表项，无策略）。 */
export interface PortfolioLite {
  id: number | null
  name: string
  market: string
  description: string | null
  custom_calc_py_code: string
  status: string | null
  tag: string | null
  updated_at: string
  created_at: string
}

/** `GET /portfolio/` 响应。 */
export interface PortfolioList {
  data: PortfolioLite[]
}

/** 目标权重映射：symbol → weight（可负=空头）。 */
export type LatestWeights = Record<string, number>

export type TargetSizingAvailability = 'available' | 'pending_execution' | 'legacy' | 'unavailable'

/** 规划时冻结的原生价格到审计金额的换算依据。 */
export interface PriceValueConversion {
  price_currency: string
  value_currency: string
  rate: number
  reference: Record<string, unknown>
}

/** 单品种从策略目标到可执行数量的完整换算证据。 */
export interface TargetSizingRow {
  symbol: string
  sizing_mode: string
  status: string
  reason_code: string
  strategy_weight: number | null
  account_weight: number | null
  account_multiplier: number | null
  weight_precision: number | null
  equity: number | null
  reference_price: number | null
  unit_multiplier: number | null
  unit_notional: number | null
  target_notional: number | null
  raw_quantity: number | null
  target_quantity: number | null
  current_quantity: number | null
  quantity_step: number | null
  min_quantity: number | null
  min_notional: number | null
  value_conversion?: PriceValueConversion | null
}

export interface TargetSizing {
  status: TargetSizingAvailability
  execution_id: string | null
  calculated_at: string | null
  rows: Record<string, TargetSizingRow>
}

/** 最近一次成功计算的目标权重快照；`calculated_at=null` 表示从未计算。 */
export interface TargetWeightSnapshot {
  weights: LatestWeights
  /** 渠道量化后的目标数量（与 weights 同 key）；渠道未提供时为 null。 */
  quantities?: LatestWeights | null
  /** 组合脚本直接给出的策略权重。 */
  strategy_weights?: LatestWeights | null
  /** 经过账户多空杠杆与精度后的执行器权重。 */
  account_weights?: LatestWeights | null
  /** 当次 execution 持久化的数量换算依据；旧记录不做现价反推。 */
  sizing?: TargetSizing | null
  calculated_at: string | null
  source: 'manual' | 'execution' | null
  execution_id: string | null
  context_account_id: number | null
}

/** 自定义组合脚本校验结果，对应后端 `ValidateCustomCalcResponse`。 */
export interface ValidateCustomCalcResult {
  /** 脚本是否成功执行并返回合法权重。 */
  valid: boolean
  /** 成功时脚本返回的目标权重（symbol → weight）。 */
  target: LatestWeights | null
  /** 失败时的错误摘要。 */
  error: string | null
  /** 失败时的完整 traceback，供「展开」查看。 */
  traceback: string | null
  /** 出错的用户代码行号（1 基）；无法定位到用户代码时为 null。 */
  error_line: number | null
  /** 语法错误时的列偏移（1 基）；其它为 null。 */
  error_offset: number | null
  /** 异常类型名，如 SyntaxError / RuntimeError。 */
  error_type: string | null
  /** 简洁错误消息，供在出错行内联展示。 */
  error_message: string | null
}

/* ============================ 执行 ============================ */

/** 账户资产快照，取自执行记录 `raw_result.account_assets`（容错、字段可缺）。 */
export interface AccountAssets {
  total_asset?: number
  currency?: string
  positions?: Position[]
  [k: string]: unknown
}

export interface AccountAssetSnapshot {
  id: number | null
  account_id: number
  assets: AccountAssets
  source: 'execution' | 'manual'
  execution_id: string | null
  created_at: string
}

export interface AccountAssetSnapshotList {
  data: AccountAssetSnapshot[]
  count: number
}

/** 单个持仓（字段随渠道而异，容错处理）。 */
export interface Position {
  symbol?: string
  /** 持仓市值（幅度，恒非负；方向由 `direction` 表达）。 */
  market_value?: number | null
  /** 持仓方向：``多头`` / ``空头``（部分渠道可能给英文）。缺失按多头处理。 */
  direction?: string
  /** 持仓数量。 */
  volume?: number
  /** 当前可用于交易的持仓数量。 */
  available_volume?: number
  avg_price?: number
  [k: string]: unknown
}

/** 执行记录，对应 `ExecuteRecordPublic`。 */
export interface ExecuteRecord {
  id: number | null
  execution_id: string | null
  raw_input: Record<string, unknown> & {
    channel_type?: string
    curr_target?: LatestWeights
    last_target?: LatestWeights
  }
  raw_result: Record<string, unknown> & {
    account_assets?: AccountAssets
    /** 执行任务终态：``TERMINATED`` / ``SUCCEEDED`` 等（后端 records.py 写入）。用于把「终止」与「失败」区分开。 */
    task_status?: string
    /** 执行类型：``rebalance`` / ``clear_positions``。用于把成功「清仓」与「空跑」区分开。 */
    execution_kind?: string
    /** 执行器输出状态（SUCCEEDED/NOOP/BLOCKED/PARTIAL/FAILED）。 */
    status?: string
    error?: string
  }
  is_success: number
  created_at: string
}

/** `GET /account/execute_records/{id}` 响应。 */
export interface ExecuteRecordList {
  data: ExecuteRecord[]
  count: number
}

/** 账户-组合绑定记录，对应 `PortfolioAccountPublic`。 */
export interface PortfolioAccountRecord {
  account_id: number
  portfolio_id: number | null
  created_at: string
}

/** `GET /account/portfolio_records/{id}` 响应。 */
export interface PortfolioAccountList {
  data: PortfolioAccountRecord[]
  count: number
}

/** 执行任务状态枚举，对应 `ExecutionTaskStatus`。 */
export type ExecutionTaskStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'TERMINATING'
  | 'TERMINATED'
  | 'SUCCEEDED'
  | 'FAILED'

/** 立即执行触发响应，对应 `ExecutionTriggerResponse`。 */
export interface ExecutionTrigger {
  message: string
  execution_id: string
  account_id: number
  accepted?: 'created' | 'coalesced'
}

/** 执行状态轮询响应，对应 `ExecutionStatusPublic`。 */
export interface ExecutionStatus {
  execution_id: string
  account_id: number
  execution_kind: 'rebalance' | 'clear_positions' | null
  status: ExecutionTaskStatus
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  /** 执行器输出状态（SUCCEEDED/NOOP/BLOCKED/PARTIAL/FAILED）；任务未结束或无记录时为 null。 */
  output_status?: string | null
  record_id: number | null
  is_success: number | null
  cancel_requested_at: string | null
  cancel_reason: string | null
  terminate_mode: string | null
}

/** 终止执行响应，对应 `ExecutionTerminateResponse`。 */
export interface ExecutionTerminate {
  message: string
  account_id: number
  execution_id: string | null
  status: ExecutionTaskStatus | null
}

/** 执行事件，对应 `ExecutionEventPublic`（保留常用字段，其余容错）。 */
export interface ExecutionEvent {
  id: number | null
  execution_id: string
  account_id: number
  channel: TradeChannel
  algorithm: string
  event_type: string
  status: 'INFO' | 'SUCCESS' | 'WARNING' | 'ERROR'
  reason_family: string
  reason_code: string
  symbol: string | null
  /** 渠道订单号（订单级事件才有）。 */
  order_id: string | null
  /** 客户端订单号。 */
  client_order_id: string | null
  ts_local_created: string
  seq: number
  details: Record<string, unknown>
}

/** `GET /account/executions/{id}/events` 响应。 */
export interface ExecutionEventList {
  data: ExecutionEvent[]
  count: number
}

/** 执行附件，对应 `ExecutionArtifactPublic`。 */
export interface ExecutionArtifact {
  id: number | null
  execution_id: string
  artifact_type: string
  schema_version?: number
  content: Record<string, unknown>
  created_at: string
}

/**
 * 账户快照读取来源标。
 *
 * `assumed`、`error`、`unavailable` 是已知的退化来源；渠道或仿真器可以提供其他
 * 有效来源标，前端必须保留其快照事实。
 */
export type AccountSnapshotSource = 'real' | 'assumed' | 'error' | 'unavailable' | (string & {})

/** 单笔成交明细。 */
export interface ExecTrade {
  price: number | null
  volume: number | null
  value: number | null
  native_value?: number | null
  price_currency?: string | null
  value_currency?: string | null
  time: string | null
  /** 手续费（取自成交 extra.commission）。 */
  fee: number
  fee_asset: string | null
}

/** 单笔订单（挂上按 order_id 归组的成交）。 */
export interface ExecOrder {
  order_id: string
  side: 'buy' | 'sell' | 'none'
  order_type: string
  price: number | null
  avg_price: number | null
  value_conversion?: PriceValueConversion | null
  volume: number | null
  filled_volume: number | null
  status: string
  client_order_id: string | null
  trades: ExecTrade[]
}

/** 逐只执行质量（TCA-lite）。 */
export interface SymbolTca {
  n_orders: number
  n_trades: number
  arrival_bid: number | null
  arrival_ask: number | null
  /** 到达价中间价（滑点基准）。 */
  arrival_mid: number | null
  /** 相对到达中间价的滑点（bps，有利为正）。 */
  slippage_bps: number | null
  /** 主/被动成交判读。 */
  liquidity: 'passive' | 'aggressive' | 'mixed' | null
  /** 成交率（Σfilled/Σordered）。 */
  fill_ratio: number | null
  /** 实付手续费。 */
  fee: number
  fee_asset: string | null
}

/** 单只对账行（`execution_summary.content.reconciliation.symbols[]`）。 */
export interface SymbolReconciliation {
  symbol: string
  status: string | null
  /** 意图带号目标持仓量。 */
  target: number | null
  /** 本次带号成交量（买正卖负）。 */
  filled: number
  /** 本次账户币种带号成交金额（买正卖负）；换算不可读时为空。 */
  filled_value: number | null
  value_currency?: string | null
  filled_native_value?: number
  native_currency?: string | null
  /** 本次原生币种成交加权均价；无成交时为 `null`。 */
  avg_price: number | null
  /** 执行前带号持仓。 */
  before: number | null
  /** 执行后带号持仓。 */
  after: number | null
  /** 本次实际净变动 `after - before`。 */
  moved: number | null
  /** 成交与实际变动之差，非零即滑点/费用/外部变动信号。 */
  drift: number | null
  /** 到位度 `after / target`；`target≈0` 时无意义为 `null`。 */
  attained_ratio: number | null
  /** 是否到位；`target` 缺失时为 `null`。 */
  reached: boolean | null
  /** 当次执行记录的目标数量换算依据。 */
  sizing: TargetSizingRow | null
  /** 逐只执行质量（TCA-lite）。 */
  tca: SymbolTca
  /** 订单→成交树。 */
  orders: ExecOrder[]
}

/** 逐只对账块（`execution_summary` 附件 `content.reconciliation`）。 */
export interface ExecutionReconciliation {
  account: {
    equity_before: number | null
    equity_after: number | null
    source_before: AccountSnapshotSource
    source_after: AccountSnapshotSource
  }
  symbols: SymbolReconciliation[]
}

/** `GET /account/executions/{id}/artifacts` 响应。 */
export interface ExecutionArtifactList {
  data: ExecutionArtifact[]
  count: number
}

/** 变更类接口的简单消息封装，对应 `Message`。 */
export interface Message {
  message: string
}
