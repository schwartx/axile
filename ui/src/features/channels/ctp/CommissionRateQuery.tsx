import { ContractRateQuery } from '@/features/channels/ctp/ContractRateQuery'
import { getCommissionRates } from '@/features/channels/ctp/accountToolsApi'
import type { AccountToolProps } from '@/features/tools/ToolsButton'

const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 8 })
const amount = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 })

function CommissionCell({ ratio, perLot }: { ratio: number; perLot: number }) {
  return <td className="py-1.5 pl-2 text-right align-top">
    {ratio !== 0 && <span className="block">{percent.format(ratio)}</span>}
    {(perLot !== 0 || ratio === 0) && <span className="block text-xs text-ink-2">￥{amount.format(perLot)}/手</span>}
  </td>
}

export function CommissionRateQuery(props: AccountToolProps) {
  return <ContractRateQuery {...props} label="手续费" columns={['开仓', '平仓', '平今']} query={getCommissionRates}
    renderRows={rates => <tr key={rates.instrument_id.toLowerCase()}>
      <th scope="row" className="break-all py-1.5 text-left align-top font-medium">{rates.instrument_id}</th>
      <CommissionCell ratio={rates.open_ratio} perLot={rates.open_per_lot} />
      <CommissionCell ratio={rates.close_ratio} perLot={rates.close_per_lot} />
      <CommissionCell ratio={rates.close_today_ratio} perLot={rates.close_today_per_lot} />
    </tr>} />
}
