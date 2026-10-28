/** 通用格式化助手。 */

/** 带正负号的整数百分数（净敞口/敞口用）。 */
export function signedPct(v: number): string {
  return `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(0)}%`
}

/**
 * 紧凑金额：`>=1亿` 用「亿」、`>=1万` 用「万」，其余千分位整数、极小值两位小数。
 *
 * 方向由调用方另行表达，这里只格式化幅度。
 */
export function fmtMoney(v: number): string {
  const a = Math.abs(v)
  if (a >= 1e8) return `${(a / 1e8).toFixed(1)}亿`
  if (a >= 1e4) return `${(a / 1e4).toFixed(1)}万`
  if (a >= 1) return Math.round(a).toLocaleString('zh-CN')
  return a.toFixed(2)
}

/** 将后端币种代码转换为面向用户的简短金额单位。 */
export function displayCurrencyUnit(currency: string | null | undefined): string {
  const normalized = currency?.trim() ?? ''
  return normalized.toUpperCase() === 'CNY' ? '￥' : normalized
}

/**
 * 给金额数字缀简短货币记号：人民币用前缀「￥」，其它币种用代码空格后缀。
 *
 * 只用于行内钱数的「就地确认」；头部权益另用渠道声明的全称当权威锚。
 * 不把插件计价币缩成「U」，以免公开核心误把渠道代码显示成私有记号。
 */
export function withCurrency(numStr: string, currency: string): string {
  const normalized = currency.trim()
  if (normalized.toUpperCase() === 'CNY') return `￥${numStr}`
  return normalized ? `${numStr} ${normalized}` : numStr
}

/** 金额直接带货币记号（`fmtMoney` + `withCurrency` 的组合便捷式）。 */
export function money(v: number, currency: string): string {
  return withCurrency(fmtMoney(v), currency)
}

/** 滑点 bps：带号、有利为正。 */
export function fmtBps(v: number): string {
  return `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}bps`
}

/**
 * 滑点着色（三态，红绿只留给行情涨跌，故用 accent/ink/warn 表执行质量）。
 *
 * 有利为正：``> +0.05`` 吃到价格改善＝accent 蓝「表扬极」；``< -0.05`` 付出成本＝琥珀「报警极」；
 * 其间视作持平＝中性，安静。补上「表扬极」后，好成交不再和平庸成交同为一片灰。
 */
export function bpsCls(v: number): string {
  if (v > 0.05) return 'text-accent'
  if (v < -0.05) return 'text-warn'
  return 'text-ink-3'
}

/** 把过去的时间戳（ms）格式化为「N 秒前 / N 分钟前」。 */
export function timeAgo(ts: number | null, now = Date.now()): string {
  if (ts == null) return '—'
  const sec = Math.max(0, Math.round((now - ts) / 1000))
  if (sec < 60) return `${sec} 秒前`
  const min = Math.round(sec / 60)
  if (min < 60) return `${min} 分钟前`
  const hr = Math.round(min / 60)
  if (hr < 24) return `${hr} 小时前`
  return `${Math.round(hr / 24)} 天前`
}
