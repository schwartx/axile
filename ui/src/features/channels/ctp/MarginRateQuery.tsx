import { ContractRateQuery } from '@/features/channels/ctp/ContractRateQuery'
import { getMarginRates } from '@/features/channels/ctp/accountToolsApi'
import type { AccountToolProps } from '@/features/tools/ToolsButton'

const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 6 })
const amount = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 })

export function MarginRateQuery(props: AccountToolProps) {
  return <ContractRateQuery {...props} label="保证金" columns={['多头', '空头']} query={getMarginRates}
    renderRows={rates => <tr key={rates.instrument_id.toLowerCase()}>
      <th scope="row" className="py-1.5 text-left align-top font-medium">
        <span className="break-all">{rates.instrument_id}</span>
        {rates.is_relative && <span className="block text-xs font-normal text-warn">相对交易所</span>}
      </th>
      <td className="py-1.5 pl-2 text-right align-top">
        {percent.format(rates.long_ratio)}
        {rates.long_per_lot !== 0 && <span className="block text-xs text-ink-2">￥{amount.format(rates.long_per_lot)}/手</span>}
      </td>
      <td className="py-1.5 pl-2 text-right align-top">
        {percent.format(rates.short_ratio)}
        {rates.short_per_lot !== 0 && <span className="block text-xs text-ink-2">￥{amount.format(rates.short_per_lot)}/手</span>}
      </td>
    </tr>} />
}
