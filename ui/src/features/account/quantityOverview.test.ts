import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { HoldingsComparison } from '@/features/account/HoldingsComparison'
import { HoldingsPreviewTable } from '@/features/account/HoldingsPreviewTable'
import { HoldingsView } from '@/features/account/HoldingsView'
import { holdingPreviewRows, signalHoldingPreviewRows } from '@/features/account/holdingDetailModel'
import { rebalanceHeadline } from '@/features/account/holdingPreview'
import { portfolioTargetSummary } from '@/features/portfolio/portfolioCardSummary'
import { SignalOrderEvidence, SignalOrderInstructions, SignalRecordList, SignalTargets } from '@/features/signal/SignalRecordList'
import type { SignalRecord } from '@/features/signal/signalApi'
import { rebalancePlanOfServer } from '@/lib/derive'
import { formatObservedAt } from '@/lib/scheduleTime'
import type { AccountRebalancePlan, TargetSizingRow, TargetWeightSnapshot } from '@/types/api'

const targets = { '513050.SH': 34700, '513090.SH': 1900 }
const comparison: AccountRebalancePlan = {
  target_mode: 'quantity', off_symbol_count: 2,
  observed_at: '2026-11-09T01:17:00Z', target_calculated_at: '2026-11-09T01:17:00Z',
  rows: Object.entries(targets).map(([symbol, target_quantity]) => ({
    symbol, current_weight: null, target_weight: null, current_quantity: 0, target_quantity,
    action: 'open', side: 'buy', aligned: false,
  })),
}
const snapshot: TargetWeightSnapshot = {
  target_mode: 'quantity', raw_targets: targets, weights: {}, account_weights: null,
  calculated_at: comparison.target_calculated_at, source: 'execution', execution_id: 'quantity', context_account_id: 1,
}

test('持仓预览默认填满表格，有品种时提供三个列宽分隔条', () => {
  const holdings = holdingPreviewRows([], 0, snapshot, true, comparison.rows)
  const renderPreview = (rows: typeof holdings | null, columnWidths?: number[]) => renderToStaticMarkup(createElement(HoldingsPreviewTable, {
    holdings: rows, quantityMode: true, positionValueLabel: '名义价值', columnWidths, onColumnWidthsChange: () => {},
  }))
  const html = renderPreview(holdings)
  expect(html).toContain('class="w-full table-auto')
  expect(html.match(/role="separator"/g)).toHaveLength(3)
  for (const label of ['品种', '持仓/可用', '目标']) {
    expect(html).toContain(`aria-label="调整${label}列宽"`)
  }
  expect(html).not.toContain('rounded-full ring-2 ring-surface')
  expect(html).toMatch(/aria-label="向上滚动持仓表" tabindex="-1"/)
  expect(html).toMatch(/aria-label="向下滚动持仓表" tabindex="-1"/)
  expect(renderPreview([])).not.toContain('向下滚动持仓表')
  expect(renderPreview(null)).not.toContain('role="separator"')
  expect(renderPreview([])).not.toContain('role="separator"')
  expect(renderPreview(holdings, [100, 80, 80])).toContain('class="w-full table-auto')
})

test('数量目标空仓保留待建仓结论与买卖统计，不依赖百分比行', () => {
  expect(portfolioTargetSummary(snapshot).activeCount).toBe(2)
  expect(rebalanceHeadline(comparison, 0)).toBe('空仓 · 2 只待建仓')
  const plan = rebalancePlanOfServer(comparison)
  expect(plan).toMatchObject({ off: 2, buys: 2, sells: 0, flips: 0 })
  expect(plan.rows).toEqual([])
  const sell: AccountRebalancePlan = { ...comparison, off_symbol_count: 1, rows: [
    { ...comparison.rows[0], current_quantity: 100, target_quantity: -100, action: 'flip', side: 'sell' },
  ] }
  expect(rebalancePlanOfServer(sell)).toMatchObject({ off: 1, buys: 0, sells: 1, flips: 1 })
  expect(rebalanceHeadline(sell, 1)).toBe('1 只待调整')
})

test('已到位、真实空仓和未计算保持各自结论，空图表不能覆盖偏离', () => {
  expect(rebalanceHeadline({ ...comparison, off_symbol_count: 0, rows: comparison.rows.map(row => ({
    ...row, current_quantity: row.target_quantity, action: 'aligned', side: 'none', aligned: true,
  })) }, 2)).toBe('已调仓到位')
  expect(rebalanceHeadline({ ...comparison, off_symbol_count: 0, rows: [] }, 0)).toBe('空仓 · 与目标一致')
  expect(rebalanceHeadline({ ...comparison, rows: [] }, 0)).toBe('2 只待调整')
  expect(rebalanceHeadline(null, null)).toBe('偏离尚未计算')
  expect(rebalanceHeadline({ ...comparison, off_symbol_count: null }, 0)).toBe('偏离尚未计算')
})

test('数量对照直接显示当前和目标股数，并保留品种详情入口', () => {
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: comparison, target: {}, equity: 0, assetLabel: '市值',
    quantityLabel: '股', onSelect: () => {},
  }))
  expect(html).toContain('数量目标 → 可执行目标')
  expect(html).toContain('数量标尺逐品种缩放')
  expect(html).toContain('多34,700股')
  expect(html).toContain('多1,900股')
  expect(html).toContain('0股')
  expect(html).toContain('aria-label="查看 513050.SH 持仓与目标"')
  expect(html).not.toContain('当前空仓，且无目标持仓')
  expect(html).not.toContain('净敞口')
  expect(html.match(/left:50%;width:44%/g)).toHaveLength(2)
})

test('数量对照保留小数、空头和未知持仓，不把未知数量当零', () => {
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: { ...comparison, off_symbol_count: 1, rows: [
      { ...comparison.rows[0], symbol: 'XAUUSD', current_quantity: null, target_quantity: -0.015, side: 'sell' },
    ] }, target: {}, equity: 0, assetLabel: '市值',
  }))
  expect(html).toContain('空0.015')
  expect(html).toContain('—')
  expect(html).toContain('待确认')
  expect(html).toContain('持仓待确认')
  expect(html).not.toContain('0股')
  expect(html).not.toContain('chart-hatch absolute')
  expect(html).not.toContain('style="left:')
})

test('数量摘要与状态取服务端数量，已到位保持中性并保留资产', () => {
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: { ...comparison, off_symbol_count: 1, rows: [
      { ...comparison.rows[0], current_quantity: 8600, target_quantity: 7500, action: 'reduce', side: 'sell' },
      { ...comparison.rows[1], current_quantity: 1800, target_quantity: 1800, action: 'aligned', aligned: true, side: 'none' },
    ] }, target: {}, rawTargets: { '513050.SH': 7500 }, equity: 100000, assetLabel: '资产', quantityLabel: '股',
  }))
  expect(html).toContain('2 只')
  expect(html).toContain('1 到位')
  expect(html).toContain('当前 持有 2 只')
  expect(html).toContain('资产 10.0万')
  expect(html).toContain('实际 多8,600股')
  expect(html).toContain('待调整')
  expect(html).toContain('text-ink-3"> · 到位')
  expect(html).not.toContain('净敞口')
})

test('数量翻向保留两侧标尺，小数目标沿用数量解释与取整依据', () => {
  const evidence: TargetSizingRow = {
    symbol: 'XAUUSD', sizing_mode: 'quantity', status: 'SIZED', reason_code: 'COMMON.SIZING.QUANTIZED',
    strategy_weight: null, account_weight: null, account_multiplier: null, weight_precision: null,
    equity: null, reference_price: null, unit_multiplier: null, unit_notional: null, target_notional: null,
    raw_quantity: -0.0159, target_quantity: -0.015, current_quantity: 0.01,
    quantity_step: 0.001, min_quantity: 0.001, min_notional: null,
  }
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: { ...comparison, rows: [
      { ...comparison.rows[0], symbol: 'XAUUSD', current_quantity: 0.01, target_quantity: -0.015, action: 'flip', side: 'sell' },
    ] }, target: {}, equity: 0, assetLabel: '资产',
    sizing: { status: 'available', calculated_at: null, execution_id: null, rows: { XAUUSD: evidence } },
  }))
  expect(html).toContain('text-up">多0.01')
  expect(html).toContain('text-down">空0.015')
  expect(html).toContain('bg-warn-mid')
  expect(html).toContain('left:6%;width:44%')
  expect(html).toContain('多0.01')
  expect(html).toContain('空0.0159 → 空0.015 · 按0.001单位取整')
  expect(html).toContain('aria-expanded="false"')
  expect(html).toContain('直接数量目标 空0.0159')
  expect(html).not.toContain('权益')
})

test('数量目标未知时不画调整段、不宣称到位；空列表沿用共同空态', () => {
  const props = { positions: [], executablePlan: { ...comparison, rows: [
    { ...comparison.rows[0], current_quantity: 100, target_quantity: null },
  ] }, target: {}, equity: 0, assetLabel: '资产', quantityLabel: '股' }
  const html = renderToStaticMarkup(createElement(HoldingsView, props))
  expect(html).toContain('实际 多100股')
  expect(html).toContain('待确认')
  expect(html).not.toContain('chart-hatch absolute')
  expect(renderToStaticMarkup(createElement(HoldingsView, {
    ...props, executablePlan: { ...comparison, rows: [] },
  }))).toContain('当前空仓，且无目标持仓')
})

test('权重模式的买卖统计与百分比行保留', () => {
  const weightPlan: AccountRebalancePlan = { ...comparison, target_mode: 'weight', off_symbol_count: 1, rows: [
    { ...comparison.rows[0], current_weight: 0.1, target_weight: 0.2 },
  ] }
  expect(rebalancePlanOfServer(weightPlan)).toMatchObject({ off: 1, buys: 1, rows: [{ cur: 10, tgt: 20, amount: 10 }] })
  expect(rebalanceHeadline(weightPlan, 1)).toBe('1 只待调整')
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: weightPlan, target: { '513050.SH': 0.2 }, equity: 100, assetLabel: '权益',
  }))
  expect(html).toContain('账户目标 → 可执行目标')
  expect(html).toContain('多10.0%')
  expect(html).toContain('多20.0%')
  expect(html).toContain('净敞口')
  expect(html).not.toContain('逐品种缩放')
})

test('信号账户复用数量表，未知持仓和实际空仓显示不同，快照数量无执行偏离', () => {
  const render = (holdings: ReturnType<typeof signalHoldingPreviewRows>) => renderToStaticMarkup(createElement(HoldingsComparison, {
    table: { holdings, quantityMode: true, quantityLabel: '股', showAvailableVolume: false },
  }))
  const unknown = signalHoldingPreviewRows(undefined, 0, targets)
  const empty = signalHoldingPreviewRows([], 0, targets)
  expect(unknown[0]).toMatchObject({ volume: null, snapshotQuantity: 34700 })
  expect(empty[0]).toMatchObject({ volume: 0, snapshotQuantity: 34700 })

  const positions = [{ symbol: '600000.SH', direction: '多头', volume: 500 }]
  const compared = signalHoldingPreviewRows(positions, 0, targets)
  expect(compared[0]).toMatchObject({ volume: 500, snapshotQuantity: 0 })
  expect(signalHoldingPreviewRows(positions, 0, null)[0].snapshotQuantity).toBeUndefined()

  const html = render(compared.map(row => ({ ...row, availableVolume: 200 })))
  expect(html).toContain('>持仓</span>')
  expect(html).toContain('>多500</span>')
  expect(html).not.toContain('持仓/可用')
  expect(html).not.toContain('多500/')
  expect(html).toContain('>目标</span>')
  expect(html).toContain('多34,700股')
  expect(html).not.toContain('待调整')
  expect(html.match(/<th /g)).toHaveLength(3)
  expect(html).toContain('aria-label="调整持仓列宽"')
  expect(html).toContain('aria-label="调整持仓摘要与表格宽度"')
  expect(html).not.toContain('重置列宽')
})

test('信号持仓对照区分已换算的空仓和未换算', () => {
  const positions = [{ symbol: '513050.SH', direction: '多头', volume: 500 }]
  expect(signalHoldingPreviewRows(positions, 0, {})[0].snapshotQuantity).toBe(0)
  expect(signalHoldingPreviewRows(positions, 0, null)[0].snapshotQuantity).toBeUndefined()
})


test('信号明细区分目标移除与实仓未知', () => {
  const record: SignalRecord = {
    id: 'signal', created_at: '2026-11-27T01:30:00Z', scheduled_at: '2026-11-27T01:30:00Z',
    trigger_source: 'scheduler', status: 'SUCCEEDED', error: null, quantity_unit: '股',
    targets: { '600000.SH': 4600 }, node_change: {
      status: 'compared', changes: { '000001.SZ': { before: 1000, after: 0, delta: -1000 } },
    },
    adjustment: { status: 'unknown' }, followed_target: false, notification_status: 'succeeded', notification_error: null,
  }
  const html = renderToStaticMarkup(createElement(SignalTargets, { record, detailed: true }))
  expect(html).toContain('600000.SH')
  expect(html).toContain('000001.SZ')
  expect(html).toContain('较上一节点')
  expect(html).toContain('1,000 → 0')
  expect(html).toContain('-1,000')
  expect(html).toContain('实仓未知，待调整不可用')
})


test('信号记录只收起无变化的零目标品种，保留清仓指示和结转账本', () => {
  const record: SignalRecord = {
    id: 'signal', created_at: '2026-11-27T04:42:56Z', scheduled_at: null,
    trigger_source: 'manual', status: 'SUCCEEDED', error: null, quantity_unit: '股',
    targets: { '000001.SZ': 0, '000002.SZ': 1400, '600000.SH': 0 },
    node_change: { status: 'compared', changes: { '600036.SH': { before: 100, after: 0, delta: -100 } } },
    adjustment: {
      kind: 'order', basis: 'signal_holdings', status: 'compared',
      changes: {
        '000001.SZ': { before: 0, after: 0, delta: 0 },
        '000002.SZ': { before: 0, after: 1400, delta: 1400 },
        '600000.SH': { before: 900, after: 0, delta: -900 },
      },
    },
    followed_target: true,
    follow_ledger: { status: 'settled', equity: 99945.4, cash: 46687.4, fee: 31.35 },
    notification_status: 'succeeded', notification_error: null,
  }
  const html = renderToStaticMarkup(createElement(SignalTargets, { record, detailed: true }))
  expect(html).not.toContain('000001.SZ')
  expect(html).toContain('000002.SZ')
  expect(html).toContain('600000.SH')
  expect(html).toContain('600036.SH')
  expect(html).toContain('aria-label="信号品种范围"')
  expect(html).toContain('本次信号')
  expect(html).toContain('全部品种')
  expect(html).toContain('bg-up/15 text-up">买入')
  expect(html).toContain('bg-down/15 text-down">卖出')
  expect(html).toContain('900 股')
  expect(html.indexOf('按结转前信号持仓')).toBeLessThan(html.indexOf('<table'))
  expect(html).not.toContain('31.35')
  const pricedRecord = {
    ...record,
    sizing_prices: { '000002.SZ': 8.75 },
    settlement_prices: { '000002.SZ': 9.5, '600000.SH': 10.25 },
  }
  const priced = renderToStaticMarkup(createElement(SignalTargets, { record: pricedRecord, detailed: true }))
  expect(priced).toContain('换算价')
  expect(priced).toContain('结转价（¥）')
  expect(priced).toMatch(/8\.75<\/td><td[^>]*>9\.5<\/td><td[^>]*>13,300\.00/)
  expect(priced).toMatch(/—<\/td><td[^>]*>10\.25<\/td><td[^>]*>9,225\.00/)
  const scheduledTargets = renderToStaticMarkup(createElement(SignalTargets, { record }))
  expect(scheduledTargets).toContain('000001.SZ')
  expect(scheduledTargets).toContain('¥31.35')

  const renderList = (compact: boolean, items: SignalRecord[]) => renderToStaticMarkup(createElement(SignalRecordList, {
    compact,
    poll: {
      data: { items, next_cursor: null }, error: null, loading: false, refreshing: false,
      updatedAt: null, stale: false, refresh: async () => {},
    },
  }))
  const overview = renderList(true, [record])
  expect(overview).toContain('手动生成')
  expect(overview).not.toContain('<details')
  expect(overview).not.toContain('信号目标持仓')
  const second = { ...record, id: 'second', targets: { '600519.SH': 100 }, node_change: null, adjustment: null }
  const noOrders: SignalRecord = { ...second, id: 'no-orders', follow_ledger: null, adjustment: { kind: 'order', status: 'compared', changes: {} } }
  const unknown: SignalRecord = { ...noOrders, id: 'unknown', adjustment: { kind: 'order', status: 'unknown' } }
  const failed: SignalRecord = { ...noOrders, id: 'failed', status: 'FAILED', error: '计算失败原因' }
  const journal = renderList(false, [record, second, noOrders, unknown, failed])
  expect(journal).toContain('aria-label="信号记录列表"')
  expect(journal).toContain('aria-expanded="false"')
  expect(journal).toContain('aria-controls=')
  expect(journal).toContain('inert=""')
  expect(journal).toContain('border-l-2 border-accent')
  const summaries = [...journal.matchAll(/<button[^>]+aria-expanded="false"[^>]*>.*?<\/button>/g)].map(match => match[0])
  expect(summaries).toHaveLength(4)
  for (const content of ['权益 ¥', '99,945.4', '可用资金 ¥', '46,687.4', '手续费 ¥', '31.35', '2 单']) {
    expect(summaries[0]).toContain(content)
  }
  expect(summaries[0]).not.toContain('3 单')
  expect(summaries[2]).toContain('无需调仓')
  expect(summaries[3]).toContain('无下单指示')
  expect(journal).toContain('计算失败原因')
  expect(journal).toContain('信号目标持仓')
  expect(journal).toContain('600000.SH')
  expect(journal).toContain('600519.SH')

  const scheduled = renderList(true, [{ ...record, scheduled_at: record.created_at }])
  expect(scheduled).toContain('<details')
  expect(scheduled).toContain('计划时间')
  expect(scheduled).not.toContain('信号目标持仓')
})

test('概览左栏直接显示上次信号的买卖操作与品种', () => {
  const record = {
    created_at: '2026-11-27T03:00:22Z',
    quantity_unit: '股',
    adjustment: {
      kind: 'order', basis: 'signal_holdings', status: 'compared',
      changes: {
        '600000.SH': { before: 0, after: 300, delta: 300 },
        '510300.SH': { before: 500, after: 0, delta: -500 },
      },
    },
  } as const
  const html = renderToStaticMarkup(createElement(SignalOrderInstructions, { record }))
  expect(html).toContain('最近一次信号')
  expect(html).toContain('aria-label="最近一次信号的买卖列表"')
  expect(html).toContain('overflow-y-auto overscroll-contain')
  expect(html).toContain('aria-label="向下滚动买卖列表"')
  expect(html).toContain('aria-haspopup="dialog" aria-label="展开买卖列表"')
  expect(html.indexOf('展开买卖列表')).toBeLessThan(html.indexOf('最近一次信号的买卖列表'))
  expect(html).not.toContain('下单指示')
  expect(html).toMatch(/>买入<\/span>.*>300 股<\/span>.*>600000\.SH<\/span>/)
  expect(html).toMatch(/>卖出<\/span>.*>500 股<\/span>.*>510300\.SH<\/span>/)
  expect(html).toContain('11:00:22</time></p>')
  expect(formatObservedAt(record.created_at, Date.parse('2026-11-27T04:00:00Z'), 'second')).toBe('今天 11:00:22')
  expect(html).not.toContain('按结转前信号持仓')
  const empty = { ...record, adjustment: { ...record.adjustment, changes: {} } }
  const emptyHtml = renderToStaticMarkup(createElement(SignalOrderInstructions, { record: empty }))
  expect(emptyHtml).toContain('无需下单')
  expect(emptyHtml).not.toContain('展开买卖列表')
})


test('信号弹窗按结转价汇总金额，缺价时保留未知并标记未结转测算', () => {
  const record = {
    created_at: '2026-11-27T04:42:56Z', quantity_unit: '股', adjustment: null,
    settlement_prices: { '600000.SH': 9.5, '510300.SH': 10.25 },
    follow_ledger: { status: 'settled', equity: 99945.4, cash: 46687.4, fee: 31.35 },
  } as const
  const orders: [string, { delta: number }][] = [
    ['600000.SH', { delta: 300 }], ['510300.SH', { delta: -500 }],
  ]
  const render = (value: Pick<SignalRecord, keyof typeof record>) =>
    renderToStaticMarkup(createElement(SignalOrderEvidence, { record: value, orders }))
  const html = render(record)
  expect(html).toMatch(/<dt[^>]*>买入金额（¥）<\/dt><dd[^>]*>2,850\.00/)
  expect(html).toMatch(/<dt[^>]*>卖出金额（¥）<\/dt><dd[^>]*>5,125\.00/)
  expect(html).toMatch(/<dt[^>]*>手续费（¥）<\/dt><dd[^>]*>31\.35/)
  const old = render({ ...record, settlement_prices: null })
  expect(old).toMatch(/<dt[^>]*>买入金额（¥）<\/dt><dd[^>]*>—/)
  expect(old).toContain('31.35')
  const estimate = render({
    ...record, follow_ledger: { ...record.follow_ledger, status: 'insufficient_cash', fee: 0 },
    settlement_prices: { '510300.SH': 10.25 },
  })
  expect(estimate).toContain('资金不足，本次未跟随')
  expect(estimate).toContain('测算金额（¥）')
  expect(estimate).toMatch(/<dt[^>]*>测算买入金额（¥）<\/dt><dd[^>]*>—/)
  expect(estimate).toMatch(/<dt[^>]*>测算卖出金额（¥）<\/dt><dd[^>]*>5,125\.00/)
  expect(estimate).toMatch(/<dt[^>]*>手续费（¥）<\/dt><dd[^>]*>0\.00/)
})
