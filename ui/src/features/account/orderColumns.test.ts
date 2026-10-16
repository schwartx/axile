import { describe, expect, test } from 'bun:test'
import { averageText, attributeText, orderColumns, orderDetails } from '@/features/account/orderColumns'
import type { CurrentOrder } from '@/lib/api/orders'
const order = (attributes: CurrentOrder['attributes'], avg_price?: number | null) => ({ attributes, avg_price } as CurrentOrder)

describe('order channel presentation', () => {
  test('channel columns stay fixed for missing values; private fields use the external contract', () => {
    expect(orderColumns('ctp').map(c => c.label)).toEqual(['交易日', '投机 / 套保'])
    expect(orderColumns('gm').map(c => c.label)).toEqual(['已成金额', '业务类型'])
    expect(orderColumns('private').map(c => c.label)).toEqual(['订单类型', '持仓方向', '只减仓'])
    expect(orderColumns('private').map(c => c.text(order({})))).toEqual(['—', '—', '—'])
    expect(attributeText(order({ reduce_only: false }), 'reduce_only')).toBe('否')
    expect(attributeText(order({ is_online: false }), 'is_online')).toBe('尚未确认')
  })
  test('native business codes, instructions and zero amounts are distinct from missing', () => {
    expect(attributeText(order({ order_business: '200' }), 'order_business')).toBe('融资买入')
    expect(attributeText(order({ filled_amount: 0 }), 'filled_amount')).toBe('0.00')
    expect(orderColumns('tq')[0].text(order({ time_condition: 'IOC', volume_condition: 'ALL' }))).toBe('立即成交剩余撤销 / 全部数量')
    expect(averageText(order({}, null))).toBe('—')
    expect(averageText(order({}, 104))).toBe('104')
    expect(orderDetails(order({ password: 'secret', time_in_force: 'GTX' }))).toEqual([{ key: 'time_in_force', label: '有效期', text: '只做挂单' }])
  })
})
