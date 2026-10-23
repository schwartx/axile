import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { RecordTable, type RecordColumn } from '@/features/account/RecordTable'
import { positionAttributes, positionColumns, positionColumnText, positionCommonNumber, positionCurrencyUnit, positionDecimalText, positionDetails, positionDirection, positionNumber, positionText, positionValueLabel, recordObject } from '@/features/account/positionRecordsModel'
import type { PositionColumn } from '@/features/account/positionRecordsModel'
import type { ChannelCapability, Position } from '@/types/api'

interface Props {
  positions: Position[]
  channel?: string
  units?: ChannelCapability['units']
  currency?: string
  loading?: boolean
  emptyText: string
}

function PositionRow({ position, channel, units, currency, columns }: Omit<Props, 'positions' | 'emptyText'> & { position: Position; columns: PositionColumn[] }) {
  const [expanded, setExpanded] = useState(false)
  const panel = useId()
  const attrs = positionAttributes(position)
  const extra = recordObject(position.extra)
  const provenance = recordObject(extra.market_value_provenance)
  const nativeRows = Array.isArray(extra.native_positions) ? extra.native_positions.map(recordObject) : []
  const quantity = (key: 'volume' | 'available_volume') => {
    const value = positionText(positionCommonNumber(position, key, channel))
    return value !== '—' && units?.quantity_label ? `${value} ${units.quantity_label}` : value
  }
  const value = positionDecimalText(positionCommonNumber(position, 'market_value', channel))
  const currencyUnit = positionCurrencyUnit(currency)
  const fields = positionDetails(position, channel)
  const evidence = [
    ['原生品种代码', extra.gm_symbol ?? extra.tq_symbol], ['计价币种', positionCurrencyUnit(extra.quote_asset)],
    ['组合持仓来源', extra.combination_origin],
    ['估值价格', extra.valuation_status === 'priced' ? positionDecimalText(extra.last_price) : provenance.source === 'tq_last_price' ? positionDecimalText(provenance.price) : null], ['合约乘数', extra.volume_multiple ?? provenance.multiplier],
    ['行情时间', extra.quote_update_time ?? provenance.quote_time], ['估值来源', extra.valuation_source ?? provenance.source],
    ['估值状态', extra.valuation_status], ['估值缺失原因', extra.valuation_reason ?? provenance.reason],
  ].filter(([, value]) => value != null && value !== '')
  return <tbody className="border-b border-line" data-activity-record>
    <tr className="h-16 text-[13px] [&_td]:whitespace-nowrap [&_td]:px-3 [&_td]:py-3 [&_td:first-child]:pl-0 [&_td:last-child]:pr-0" data-activity-row>
      <td className="font-medium"><OverflowText text={position.symbol ?? '—'} className="w-full" /></td>
      <td>{positionDirection(position)}</td>
      <td className="num text-right" data-numeric><OverflowText text={quantity('volume')} /></td>
      <td className="num text-right" data-numeric><OverflowText text={quantity('available_volume')} /></td>
      <td className="num text-right" data-numeric><OverflowText text={value !== '—' && currencyUnit ? `${value} ${currencyUnit}` : value} /></td>
      {columns.map(column => {
        const pnl = column.pnl ? positionNumber(attrs[column.key]) : null
        return <td key={column.key} data-numeric className={`num text-right ${pnl == null || pnl === 0 ? '' : pnl > 0 ? 'text-up' : 'text-down'}`}><OverflowText text={positionColumnText(position, column, channel)} /></td>
      })}
      <td className="sticky right-0 w-8 bg-surface"><button type="button" aria-expanded={expanded} aria-controls={panel} aria-label={`${expanded ? '收起' : '展开'}持仓 ${position.symbol} ${positionDirection(position)}`}
        onClick={() => setExpanded(value => !value)} className="flex min-h-8 w-full cursor-pointer items-center justify-end text-ink-3 hover:text-ink-1"><ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} /></button></td>
    </tr>
    <tr><td colSpan={6 + columns.length} className="p-0"><div id={panel} aria-hidden={!expanded} inert={!expanded} className={`grid transition-[grid-template-rows,visibility] duration-200 motion-reduce:transition-none ${expanded ? 'visible grid-rows-[1fr]' : 'invisible grid-rows-[0fr]'}`}>
      <div className="sticky left-0 min-h-0 min-w-0 max-w-[calc(92vw-44px)] overflow-hidden"><div className="@container pb-4 text-xs text-ink-2">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 @xl:grid-cols-2 @4xl:grid-cols-3">
          {[...fields.map(field => [field.label, field.text]), ...evidence].map(([label, text]) => <div key={String(label)} className="grid min-w-0 grid-cols-[140px_minmax(0,1fr)] gap-x-3"><dt className="text-ink-3">{String(label)}</dt><dd className="min-w-0 break-all">{String(text)}</dd></div>)}
        </dl>
        {nativeRows.length > 0 && <div className="mt-3 border-t border-line pt-3"><p className="mb-2 text-ink-3">柜台持仓分项</p>{nativeRows.map((row, index) => <p key={index} className="py-1">{({ '1': '投机', '2': '套利', '3': '套保', '5': '做市' } as Record<string, string>)[String(row.hedge_flag)] ?? '投机套保未记录'} · {row.position_date === '1' ? '今仓记录' : row.position_date === '2' ? '昨仓记录' : '持仓记录'} · 持仓 {positionText(row.volume)} · 今仓 {positionText(row.today_volume)} · 持仓盈亏 {positionDecimalText(row.position_profit)} · 占用保证金 {positionDecimalText(row.margin)}</p>)}</div>}
        {fields.length === 0 && evidence.length === 0 && nativeRows.length === 0 && <p className="text-ink-3">暂无额外持仓信息</p>}
      </div></div>
    </div></td></tr>
  </tbody>
}

export function PositionRecordsTable(props: Props) {
  const nativeColumns = positionColumns(props.channel, props.positions)
  const columns: RecordColumn[] = [
    { key: 'symbol', label: '品种', grow: 1, sample: 'SHFE.rb2610', values: props.positions.map(position => position.symbol ?? ''), maxWidth: 180 },
    { key: 'direction', label: '方向', sample: '多头' },
    { key: 'volume', label: '持仓', grow: 1, numeric: true, sample: '99,999.99999999 手' },
    { key: 'available', label: '可用', grow: 1, numeric: true, sample: '99,999.99999999 手' },
    { key: 'market_value', label: positionValueLabel(props.channel, props.positions), grow: 1, numeric: true, sample: '999,999.99 瑞郎' },
    ...nativeColumns.map(column => ({ key: column.key, label: column.label, grow: 1, numeric: true, sample: column.key === 'today_yesterday' ? '999 / 999' : '99,999.99',
      values: props.positions.map(position => positionColumnText(position, column, props.channel)) })),
    { key: 'details', label: '详情' },
  ]
  return <RecordTable columns={columns} kind="position" loading={props.loading} empty={!props.positions.length} emptyText={props.emptyText}>
    {props.positions.map((position, index) => <PositionRow key={JSON.stringify([position.symbol, position.direction, index])} {...props} position={position} columns={nativeColumns} />)}
  </RecordTable>
}
