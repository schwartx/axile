import { expect, test } from 'bun:test'

import { flowOf } from '@/features/setup/steps'
import { initialSignalDraft, signalAccountConfig, validateSignalQuoteSetup } from '@/features/signal/signalData'

test('信号向导依次进入行情、资金、定时和确认', () => {
  const path = '/setup/acct/signal'
  const { steps } = flowOf(path)
  expect(steps.map((step) => step.label)).toEqual([
    '选渠道', '账户与组合', '设置行情', '资金与持仓', '定时与通知', '试算与确认',
  ])
  for (const [index, step] of steps.entries()) {
    const search = new URL(step.path, 'http://localhost').search
    expect(flowOf(index === 0 ? step.path : path, search, true).index).toBe(index)
  }
})

test('行情和资金各自验证本步配置', () => {
  expect(() => validateSignalQuoteSetup(initialSignalDraft.quoteCode)).toThrow('替换默认模板')
  expect(() => validateSignalQuoteSetup(' ')).toThrow('请填写行情函数')
  expect(() => validateSignalQuoteSetup('def fetch_quotes(symbols):\n    return {}')).not.toThrow()

  expect(() => signalAccountConfig(initialSignalDraft)).toThrow('账户权益')
  const draft = { ...initialSignalDraft, quoteCode: '', equity: '100000', cash: '0' }
  expect(signalAccountConfig(draft).manual_assets).toEqual({
    total_asset: 100000, available_cash: 0, currency: 'CNY', positions: null,
  })
})
