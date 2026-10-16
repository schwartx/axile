import type { CurrentOrder } from '@/lib/api/orders'

export interface OrderColumn { key: string; label: string; numeric?: boolean; text: (order: CurrentOrder) => string }
const number = (value: unknown, money = false) => typeof value === 'number' && Number.isFinite(value)
  ? value.toLocaleString('zh-CN', { minimumFractionDigits: money ? 2 : 0, maximumFractionDigits: money ? 2 : 12 }) : '—'
const enums: Record<string, Record<string, string>> = {
  submit_status: { '0': '报单已提交', '1': '撤单已提交', '2': '改单已提交', '3': '已接受', '4': '报单被拒绝', '5': '撤单被拒绝', '6': '改单被拒绝' },
  order_duration: { '0': '未知', '1': '立即成交剩余撤销', '2': '全部成交否则撤销', '3': '当日有效', '4': '本节有效', '5': '指定日期有效', '6': '撤销前有效', '7': '集合竞价有效', '8': '盘后定价' },
  order_qualifier: { '0': '未知', '1': '对方最优', '2': '己方最优', '3': '最优五档剩余撤销', '4': '最优五档剩余转限价' },
  order_style: { '0': '未知', '1': '按数量', '2': '按金额', '3': '按比例', '4': '目标数量', '5': '目标金额', '6': '目标比例' },
  offset_leg2: { OPEN: '开仓', CLOSE: '平仓', CLOSETODAY: '平今' },
  force_close_reason: { '0': '非强平', '1': '资金不足', '2': '客户超仓', '3': '会员超仓', '4': '持仓非整数倍', '5': '违规', '6': '其他', '7': '自然人临近交割', '8': '风控资金不足', '9': '本地资金不足', a: '本地违规（未检查）', b: '本地违规' },
  contingent_condition: { '1': '立即', '2': '止损', '3': '止盈', '4': '预埋单', '5': '最新价大于触发价', '6': '最新价大于等于触发价', '7': '最新价小于触发价', '8': '最新价小于等于触发价', '9': '卖一价大于触发价', A: '卖一价大于等于触发价', B: '卖一价小于触发价', C: '卖一价小于等于触发价', D: '买一价大于触发价', E: '买一价大于等于触发价', F: '买一价小于触发价', H: '买一价小于等于触发价' },
  hedge_flag: { '1': '投机', '2': '套利', '3': '套保', '5': '做市', '6': '投机/套保', '7': '套保/投机' },
  position_side: { LONG: '多头', SHORT: '空头', BOTH: '单向', '0': '未知', '1': '多头', '2': '空头' },
  native_order_type: { '1': '市价', '2': '限价', '3': '最优价', '4': '最新价', '5': '最新价浮动一跳', '6': '最新价浮动两跳', '7': '最新价浮动三跳', '8': '卖一价', '9': '卖一价浮动一跳', A: '卖一价浮动两跳', B: '卖一价浮动三跳', C: '买一价', D: '买一价浮动一跳', E: '买一价浮动两跳', F: '买一价浮动三跳', G: '五档价', LIMIT: '限价', MARKET: '市价', STOP: '止损限价', STOP_MARKET: '止损市价', TAKE_PROFIT: '止盈限价', TAKE_PROFIT_MARKET: '止盈市价', TRAILING_STOP_MARKET: '移动止损' },
  time_condition: { '1': '立即成交剩余撤销', '2': '本节有效', '3': '当日有效', '4': '指定日期有效', '5': '撤销前有效', '6': '集合竞价有效', IOC: '立即成交剩余撤销', GFS: '本节有效', GFD: '当日有效', GTC: '撤销前有效', GFA: '集合竞价有效' },
  volume_condition: { '1': '任意数量', '2': '最小数量', '3': '全部数量', ANY: '任意数量', MIN: '最小数量', ALL: '全部数量' },
  time_in_force: { GTC: '撤销前有效', IOC: '立即成交剩余撤销', FOK: '全部成交否则撤销', GTX: '只做挂单', GTD: '指定时间有效' },
  working_type: { MARK_PRICE: '标记价格', CONTRACT_PRICE: '合约最新价' },
  order_business: { '0': '普通交易', '200': '融资买入', '201': '融券卖出', '202': '买券还券', '203': '卖券还款', '204': '直接还券', '205': '融资平仓', '206': '融券平仓', '207': '担保品买入', '208': '担保品卖出', '209': '担保品转入', '210': '担保品转出', '402': '可转债转股', '403': '可转债回售', '404': '回售撤销' },
}

export function attributeText(order: CurrentOrder, key: string): string {
  const value = order.attributes?.[key]
  if (value == null || value === '') return '—'
  if (typeof value === 'boolean') return value ? '是' : ['is_online', 'is_dead', 'is_error'].includes(key) ? '尚未确认' : '否'
  if (key === 'trading_day') return String(value).replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3')
  if (typeof value === 'number') return number(value, key === 'filled_amount' || key === 'cum_quote')
  return enums[key]?.[value] ?? String(value)
}

const column = (key: string, label: string, numeric = false): OrderColumn => ({ key, label, numeric, text: order => attributeText(order, key) })
const columns: Record<string, OrderColumn[]> = {
  ctp: [column('trading_day', '交易日'), column('hedge_flag', '投机 / 套保')],
  tq: [{ key: 'conditions', label: '执行条件', text: order => [attributeText(order, 'time_condition'), attributeText(order, 'volume_condition')].filter(value => value !== '—').join(' / ') || '—' }],
  gm: [column('filled_amount', '已成金额', true), column('order_business', '业务类型')],
  external: [column('native_order_type', '订单类型'), column('position_side', '持仓方向'), column('reduce_only', '只减仓')],
}
export const averageText = (order: CurrentOrder) => number(order.avg_price)
export function orderColumns(channel?: string): OrderColumn[] { return channel ? columns[channel] ?? columns.external : [] }

const detailLabels: Record<string, string> = {
  trading_day: '交易日', hedge_flag: '投机 / 套保', native_order_type: '原生订单类型', original_order_type: '原始订单类型',
  time_condition: '时间条件', volume_condition: '数量条件', min_volume: '最小成交量', gtd_date: '指定有效日期',
  contingent_condition: '触发条件', stop_price: '触发价', submit_status: '报单提交状态', active_time: '激活时间',
  suspend_time: '挂起时间', native_update_time: '柜台更新时间', cancel_time: '撤单时间', order_local_id: '本地报单编号',
  force_close_reason: '强平原因', exchange_order_id: '柜台 / 交易所委托编号', ex_ord_id: '交易所委托编号',
  offset_leg2: '第二腿开平', is_online: '已确认报入交易所', is_dead: '已确认结束', is_error: '已确认错单',
  position_side: '持仓方向', order_business: '业务类型', order_style: '委托风格', order_duration: '委托时间属性',
  order_qualifier: '委托成交属性', ord_rej_reason: '拒绝原因代码', algo_order_id: '算法单编号', value: '委托金额',
  percent: '委托比例', target_volume: '目标数量', target_value: '目标金额', target_percent: '目标比例',
  time_in_force: '有效期', good_till_date: '到期时间戳（毫秒）', cum_quote: '累计成交金额', price_match: '价格匹配',
  self_trade_prevention_mode: '自成交防护', working_type: '触发价格类型', price_protect: '价格保护',
  reduce_only: '只减仓', close_position: '触发后全平', activate_price: '移动止损激活价', price_rate: '回调比例（%）',
}
export function orderDetails(order: CurrentOrder): { key: string; label: string; text: string }[] {
  return Object.entries(detailLabels).filter(([key]) => order.attributes?.[key] != null)
    .map(([key, label]) => ({ key, label, text: attributeText(order, key) }))
}
