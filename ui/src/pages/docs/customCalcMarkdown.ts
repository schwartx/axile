export interface ContextField { name: string; type: string; desc: string }
export interface ContextMode { item: string; sample: string; real: string }
export interface CalcExample { key: string; title: string; desc: string; code: string; advanced?: boolean }

export const EXECUTION_TRIGGERS = [
  '在组合编辑器中点击“试跑”',
  '在组合详情中手动刷新目标',
  '在账户设置流程中预览组合目标',
  '真实调仓开始前计算本次目标',
]

export const CONTEXT_MODES: ContextMode[] = [
  { item: '账户与行情', sample: '固定样例资产，任意标的价格均为 100', real: '当前账户、持仓、行情和订单' },
  { item: 'executor', sample: '仅实现通用查询的样例 executor', real: '当前渠道完整的真实 executor' },
  { item: '运行位置', sample: '一次性子进程', real: '账户的常驻 worker' },
  { item: '渠道副作用', sample: '不连接真实渠道', real: '主动调用交易方法会立即作用于真实渠道' },
]

export const CONTEXT_FIELDS: ContextField[] = [
  { name: 'settings', type: 'ContextSettings', desc: '只读执行配置：long_leverage、short_leverage、weight_precision；杠杆已解析渠道默认值。工作台的临时杠杆只覆盖本次试跑，显式保存后才修改账户。无账户样例为 1、1、0.01；权重模式在执行层应用杠杆和精度，数量模式跳过这两项缩放' },
  { name: 'account', type: 'UnifiedAccountAssets', desc: '本次计算内缓存的账户资产快照' },
  { name: 'positions', type: 'list[Position]', desc: 'account.positions 的便捷入口' },
  { name: 'get_positions(symbol=None, direction=None)', type: 'list[Position]', desc: '按标的和多空方向筛选持仓' },
  { name: 'get_quote(symbol)', type: 'UnifiedPriceData', desc: '获取并在本次计算内缓存统一行情' },
  { name: 'get_price(symbol)', type: 'float', desc: '获取统一行情中的最新成交价' },
  { name: 'get_pending_orders(symbol=None)', type: 'list[UnifiedOrder]', desc: '查询全部或指定标的的未完成订单' },
  { name: 'query_trades(symbol, order_id)', type: 'list[TradeRecord]', desc: '查询指定订单的成交明细' },
  { name: 'executor', type: '渠道执行器', desc: '完整的当前账户渠道执行器，供高级代码直接使用' },
]

export const CONTRACT_CODE = `def calculate_portfolio(context):
    # 权重是目标仓位，不是委托数量
    return {"rb2610": 0.5, "ag2612": -0.25}`

export const CALC_EXAMPLES: CalcExample[] = [
  { key: 'fixed', title: '固定目标权重', desc: '不依赖账户状态；适合固定配置或作为最小模板。', code: `def calculate_portfolio(context):
    return {"rb2610": 0.5, "ag2612": 0.5}` },
  { key: 'quantity', title: '直接指定目标数量', desc: '期货使用整手；支持小数数量的渠道使用原生交易单位。目标不随账户权益和杠杆缩放。', code: `def calculate_portfolio(context):
    return {"mode": "quantity", "targets": {"rb2701": 3, "ag2612": -2}}` },
  { key: 'position', title: '根据当前持仓调整目标', desc: '使用统一持仓模型，不依赖具体交易渠道。', code: `def calculate_portfolio(context):
    rb_positions = context.get_positions("rb2610")
    has_rb = any(position.volume > 0 for position in rb_positions)
    return {"rb2610": 0.25 if has_rb else 0.5}` },
  { key: 'price', title: '根据最新价格选择标的', desc: '同一标的的行情在一次计算内只查询一次。', code: `def calculate_portfolio(context):
    rb_price = context.get_price("rb2610")
    ag_price = context.get_price("ag2612")
    return {"rb2610": 1.0} if rb_price < ag_price else {"ag2612": 1.0}` },
  { key: 'tq-contract', title: '读取 TQ 合约信息', desc: 'TQ 会把最小变动价位和合约乘数放在统一行情的 extra 中。', code: `def calculate_portfolio(context):
    quote = context.get_quote("KQ.m@SHFE.rb")
    multiplier = float(quote.extra["volume_multiple"])
    price_tick = float(quote.extra["price_tick"])
    return {quote.symbol: min(1.0, 100.0 / (multiplier * price_tick))}` },
  { key: 'executor', title: '直接使用渠道 executor', desc: '仅在通用 Context 无法表达渠道专有能力时使用；可用方法取决于当前渠道实现。', advanced: true, code: `def calculate_portfolio(context):
    executor = context.executor
    quote = executor.get_market_data(["600000.SH"])["600000.SH"]
    return {quote.symbol: 0.5}` },
]

export const EXECUTOR_RULES = [
  '`context.executor` 不是只读代理，而是当前账户完整、共享且常驻的真实渠道执行器。',
  '计算目标不会自动执行返回的权重；但代码主动调用 `execute`、`empty_positions` 或渠道下单方法会立即产生真实交易副作用。',
  '成功执行后 executor、回调和模块全局状态会继续复用；不要无意修改属性、遗留回调或启动永久后台线程。',
  '自行创建的资源应在 `finally` 中释放。脚本失败、超时或 worker 异常后，系统会丢弃 worker，并在下次调用时重建。',
]

export const CONTRACT_RULES = [
  '函数必须且只能接收一个 `context` 参数。',
  '旧的 `dict[str, float]` 按权重解释；数量模式显式返回 `{"mode": "quantity", "targets": {"rb2701": 3}}`。品种键必须为字符串，目标值必须是有限数字，布尔值不算数字。',
  '正数表示目标多头，负数表示目标空头，`0.5` 表示半仓；空字典 `{}` 表示目标空仓。',
  '返回值表示完整目标持仓，不是买卖增量。数量模式跳过杠杆、权重精度和资金换算，保留渠道数量规则和执行控制；同一返回值不混用两种口径。',
  '标的格式由渠道决定；使用组合创建时针对该市场生成的默认代码最稳妥。',
]

function table(headers: string[], rows: string[][]): string {
  return [`| ${headers.join(' | ')} |`, `| ${headers.map(() => '---').join(' | ')} |`, ...rows.map((row) => `| ${row.join(' | ')} |`)].join('\n')
}

function bullets(items: string[]): string { return items.map((item) => `- ${item}`).join('\n') }

export function buildCustomCalcMarkdown(): string {
  const modes = table(['能力', '样例上下文', '真实账户'], CONTEXT_MODES.map((mode) => [mode.item, mode.sample, mode.real]))
  const fields = table(['字段或方法', '类型', '含义'], CONTEXT_FIELDS.map((field) => [`\`${field.name}\``, `\`${field.type}\``, field.desc]))
  const examples = CALC_EXAMPLES.map((example) => `### ${example.title}${example.advanced ? '（高级）' : ''}\n\n${example.desc}\n\n\`\`\`python\n${example.code}\n\`\`\``).join('\n\n')
  return `# 自定义组合函数

自定义组合函数计算各品种的目标权重或目标数量。权重模式按账户配置换算数量，数量模式直接声明目标持仓；之后共用调仓执行链路。

## 何时执行

${bullets(EXECUTION_TRIGGERS)}

普通页面读取只显示最近一次成功保存的目标快照，不会执行用户函数。

## 样例上下文与真实账户

${modes}

> 真实账户“试跑”只表示 Axile 不会自动执行函数返回的目标；如果函数主动调用 executor 的交易方法，仍会产生真实交易。

## 函数契约

\`\`\`python
${CONTRACT_CODE}
\`\`\`

${bullets(CONTRACT_RULES)}

## Context 通用能力

优先使用通用能力。\`account\` 和行情缓存只在当前这一次函数调用中有效。

${fields}

## 完整 executor（高级）

${bullets(EXECUTOR_RULES)}

## 示例

${examples}
`
}
