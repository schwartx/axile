import { describe, expect, it } from 'bun:test'

import {
  accountConfigVtName,
  describeLeverage,
  describeSymbolControl,
  readAccountConfigSummary,
  writeAccountConfigSummary,
} from './configSummary'
import type { Account } from '@/types/api'

describe('describeLeverage', () => {
  it('多空分设；整数不带小数点', () => {
    expect(describeLeverage(3, 2, true)).toBe('多 3× / 空 2×')
    expect(describeLeverage(2.5, 0, true)).toBe('多 2.5× / 空 0×')
  })

  it('未设置为 —；隐藏空头档时只报多头', () => {
    expect(describeLeverage(null, null, true)).toBe('多 — / 空 —')
    expect(describeLeverage(3, 2, false)).toBe('3×')
    expect(describeLeverage(null, null, false)).toBe('—')
  })
})

describe('describeSymbolControl', () => {
  it('无控制为未设限', () => {
    expect(describeSymbolControl(null, null)).toBe('未设限')
    expect(describeSymbolControl([], [])).toBe('未设限')
  })

  it('按类计数，缺类不报', () => {
    expect(describeSymbolControl(['rb2610', 'ag2612'], null)).toBe('禁投 2')
    expect(describeSymbolControl(null, ['cu2610'])).toBe('风险 1')
    expect(describeSymbolControl(['rb2610'], ['ag2612', 'cu2610'])).toBe('禁投 1、风险 2')
  })
})

describe('accountConfigVtName', () => {
  it('按种类 + 账户生成共享名', () => {
    expect(accountConfigVtName(7, 'algorithm')).toBe('account-config-algorithm-7')
    expect(accountConfigVtName(7, 'empty')).toBe('account-config-empty-7')
  })
})

describe('配置摘要缓存', () => {
  const acc = {
    long_leverage: 3,
    short_leverage: 2,
    forbidden_symbols: ['rb2610'],
    risk_symbols: null,
    algorithm: { method: 'TWAP', params: {} },
    empty_positions_algorithm: { method: 'TARGET-POS-TASK', params: {} },
  } as unknown as Account

  it('未写入时读出 null；写入后按真源计算四项', () => {
    expect(readAccountConfigSummary(424242)).toBeNull()
    writeAccountConfigSummary(424242, acc, { showShortLeverage: true })
    expect(readAccountConfigSummary(424242)).toEqual({
      leverage: '多 3× / 空 2×',
      symbols: '禁投 1',
      algorithm: 'TWAP',
      emptyAlgorithm: 'TARGET-POS-TASK',
    })
  })

  it('清仓未设置为「未设置」', () => {
    writeAccountConfigSummary(424243, { ...acc, empty_positions_algorithm: null } as Account, {
      showShortLeverage: true,
    })
    expect(readAccountConfigSummary(424243)?.emptyAlgorithm).toBe('未设置')
  })

  it('后写覆盖先写（保存响应直接写新值）', () => {
    writeAccountConfigSummary(424242, { ...acc, long_leverage: 5 } as Account, {
      showShortLeverage: true,
    })
    expect(readAccountConfigSummary(424242)?.leverage).toBe('多 5× / 空 2×')
  })
})
