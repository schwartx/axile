import type { ChannelCapability, TradeChannel } from '@/types/api'

export interface PortfolioMarketOption {
  value: string
  label: string
  exampleSymbols: string[]
}

/** 优先使用上下文账户的渠道单位；样例或缺少账户信息时按组合市场解析。 */
export function portfolioQuantityUnit(
  channels: ChannelCapability[], market: string | null | undefined, channel?: TradeChannel,
): string {
  const descriptor = channels.find((item) => item.channel === channel)
    ?? channels.find((item) => item.market === market || item.portfolio.market_label === market)
  return descriptor?.units.quantity_label ?? ''
}

/** 按渠道注册顺序生成去重后的组合市场目录。 */
export function portfolioMarketOptions(channels: ChannelCapability[]): PortfolioMarketOption[] {
  const seen = new Set<string>()
  const options: PortfolioMarketOption[] = []
  for (const channel of channels) {
    if (seen.has(channel.market)) continue
    seen.add(channel.market)
    options.push({
      value: channel.market,
      label: channel.portfolio.market_label,
      exampleSymbols: channel.portfolio.example_symbols,
    })
  }
  return options
}

/** 根据渠道提供的标的生成最小可运行组合函数。 */
export function portfolioTemplate(option: PortfolioMarketOption): string {
  const weight = 1 / option.exampleSymbols.length
  const entries = option.exampleSymbols.map((symbol) => `${JSON.stringify(symbol)}: ${Number(weight.toFixed(6))}`)
  return `from axile.server.context import Context\n\ndef calculate_portfolio(context: Context) -> dict[str, float]:\n    # 返回 {品种: 目标权重}\n    return {${entries.join(', ')}}`
}
