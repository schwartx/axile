import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'

import { flowOf } from '@/features/setup/steps'
import { SignalLeverageFields } from '@/features/signal/SignalLeverageFields'
import { SignalNotificationSetup } from '@/features/signal/SignalNotificationSetup'
import { configToSignalDraft, initialSignalDraft, positionVolumePatch, signalAccountConfig, signalAccountErrors, signalConfig, signalEquityPatch, signalQuoteConfig, signalQuoteErrors, signalQuoteOptions, TUSHARE_API_URL } from '@/features/signal/signalData'
import { AcctConfirm } from '@/pages/setup/AccountSteps'
import { SignalSetupPage } from '@/pages/setup/SignalSetupPage'
import { useChannelCatalogStore } from '@/stores/channels'
import { useWizardStore } from '@/stores/wizard'
import type { ChannelCapability } from '@/types/api'

test('信号向导依次进入行情、资金、定时、通知和确认', () => {
  const path = '/setup/acct/signal'
  const { steps } = flowOf(path)
  expect(steps.map((step) => step.label)).toEqual([
    '选渠道', '账户与组合', '设置行情', '资金与持仓', '定时', '通知', '确认',
  ])
  for (const [index, step] of steps.entries()) {
    const search = new URL(step.path, 'http://localhost').search
    expect(flowOf(index === 0 ? step.path : path, search, true).index).toBe(index)
  }
})

test('信号向导先选择市场，并在单独的一步配置通知', () => {
  const catalog = useChannelCatalogStore.getInitialState()
  const previousChannels = catalog.channels
  catalog.channels = [{
    channel: 'ctp', market: 'ctp', portfolio: { market_label: '期货', example_symbols: [] },
    schedule: { kind: 'cn_futures' },
  } as unknown as ChannelCapability, {
    channel: 'rebalance_signal', leverage: { min: 0, max: 125, step: 0.1 },
  } as ChannelCapability]
  try {
    const render = (step: number) => renderToStaticMarkup(createElement(MemoryRouter, {
      initialEntries: [`/setup/acct/signal?step=${step}`],
    }, createElement(SignalSetupPage)))

    const market = render(2)
    expect(market).toContain('aria-label="市场"')
    expect(market).toContain('role="radio" aria-checked="false" tabindex="0"')
    expect(market).toContain('>期货</button>')
    expect(market).not.toContain('>做多杠杆</label>')
    expect(market).not.toContain('>做空杠杆</label>')
    expect(market).toContain('先选择市场，再查看同市场的组合。')
    const assets = render(4)
    expect(assets).toContain('aria-pressed="true" aria-label="跟随目标"')
    expect(assets).toContain('定时计算成功后，将目标记为信号持仓。')
    expect(assets).toContain('aria-label="跟随目标说明"')
    expect(render(5)).toContain('请先在「账户与组合」选择市场。')

    const notification = render(6)
    expect(notification).toContain('aria-label="通知方式"')
    expect(notification).not.toContain('不通知')
    expect(notification).toContain('飞书 Webhook')
    expect(notification).toContain('自定义函数')
    expect(notification).toContain('>跳过通知</button>')
    expect(notification).toContain('disabled="">下一步</button>')
    expect(notification).not.toContain('排程预览')
    const confirmation = render(7)
    expect(confirmation).toContain('账户设置 · 7 / 7')
    expect(confirmation).toContain('暂不绑定组合')
    expect(confirmation).toContain('>确认</div>')
    expect(confirmation).toContain('>跟随目标</dt><dd>已开启</dd>')
    expect(confirmation).not.toContain('试算目标数量')
    expect(confirmation).toContain('>创建账户</button>')
    expect(confirmation).toContain('>通知</dt><dd>未配置</dd>')
  } finally {
    catalog.channels = previousChannels
  }
})

test('调仓信号开户杠杆沿用市场的做多和做空设置', () => {
  const render = (showShort: boolean, longLabel: string) => renderToStaticMarkup(createElement(SignalLeverageFields, {
    limits: { min: 0, max: 125, step: 0.1 }, value: { long: '1', short: '1' }, onChange: () => {},
    ui: { show_short_leverage: showShort, long_leverage_label: longLabel, short_leverage_label: '做空杠杆' },
  }))
  const stock = render(false, '仓位系数（做多）')
  expect(stock).toContain('>仓位系数（做多）</label>')
  expect(stock).not.toContain('>做空杠杆</label>')
  const futures = render(true, '做多杠杆')
  expect(futures).toContain('>做多杠杆</label>')
  expect(futures).toContain('>做空杠杆</label>')
})

test('交易账户确认页已绑定组合也直接创建账户', () => {
  const wizard = useWizardStore.getInitialState()
  const previousAccount = wizard.acct
  wizard.acct = { ...previousAccount, name: '交易账户', portfolioId: 7 }
  try {
    const confirmation = renderToStaticMarkup(createElement(MemoryRouter, {
      initialEntries: ['/setup/acct/confirm'],
    }, createElement(AcctConfirm)))
    expect(confirmation).toContain('确认并创建账户')
    expect(confirmation).toContain('#7')
    expect(confirmation).toContain('>创建账户</button>')
    expect(confirmation).not.toContain('计算目标')
  } finally {
    wizard.acct = previousAccount
  }
})

test('信号行情按市场筛选，外部渠道市场可用自定义函数', () => {
  const channels = ['gm', 'ctp', 'tq'].map((channel) => ({ channel, independent_market_data: true }) as ChannelCapability)
  const kinds = (market: string) => signalQuoteOptions(market, channels).map((source) => source.value)
  expect(kinds('ashare')).toEqual(['gm', 'tushare', 'custom'])
  expect(kinds('ctp')).toEqual(['ctp', 'tq', 'custom'])
  expect(kinds('external')).toEqual(['custom'])
  expect(signalQuoteOptions('ctp', channels.slice(2)).map((source) => source.value)).toEqual(['tq', 'custom'])
})

test('飞书通知提供可选的测试按钮并默认明文显示 Webhook', () => {
  const render = (webhook: string) => renderToStaticMarkup(createElement(SignalNotificationSetup, {
    mode: 'feishu', webhook, code: '', name: '草稿账户', market: 'A股', heading: null, footer: null,
    onModeChange: () => {}, onWebhookChange: () => {}, onCodeChange: () => {},
  }))
  const testButton = (html: string) => html.match(/<aside aria-label="通知测试">.*?<button ([^>]+)>/)?.[1]
  expect(testButton(render('  '))).toContain('disabled=""')
  const filled = render('fixture-key')
  expect(filled).toContain('aria-label="通知测试"')
  expect(filled).toContain('发送测试卡片')
  expect(testButton(filled)).not.toContain('disabled=""')
  expect(filled).toContain('aria-label="飞书 Webhook" type="text"')
  expect(filled).toContain('placeholder="可粘贴整条 webhook 链接"')
})

test('行情和资金各自验证本步配置', () => {
  expect(initialSignalDraft.quoteKind).toBe('gm')
  expect(() => signalQuoteConfig(initialSignalDraft)).toThrow('掘金 Token')
  expect(() => signalQuoteConfig({ ...initialSignalDraft, quoteKind: '' })).toThrow('请选择行情来源')
  const custom = { ...initialSignalDraft, quoteKind: 'custom' as const }
  expect(signalQuoteConfig(custom)).toEqual({ kind: 'custom', code: initialSignalDraft.quoteCode })
  expect(() => signalQuoteConfig({ ...custom, quoteCode: ' ' })).toThrow('请填写行情函数')
  expect(signalQuoteConfig({ ...custom, quoteCode: 'def fetch_quotes(symbols):\n    return {}' }).kind).toBe('custom')

  expect(() => signalAccountConfig(initialSignalDraft)).toThrow('账户权益')
  const draft = { ...initialSignalDraft, quoteCode: '', equity: '100000', cash: '0' }
  expect(signalAccountConfig(draft).manual_assets).toEqual({
    total_asset: 100000, available_cash: 0, currency: 'CNY', positions: null,
  })
})

test('资金与持仓校验标出对应行，并保留未提供与空仓两种状态', () => {
  const funded = { ...initialSignalDraft, equity: '100,000', cash: '80,000' }
  expect(signalAccountConfig(funded).manual_assets?.positions).toBeNull()
  expect(signalAccountConfig(funded).manual_assets?.total_asset).toBe(100000)
  const empty = { ...funded, positionsKnown: true }
  expect(signalAccountConfig(empty).manual_assets?.positions).toEqual([])
  const invalid = { ...empty, positions: [{ id: 'row', symbol: '600000.SH', direction: '多头' as const, volume: '1000', available: '2000' }] }
  expect(signalAccountErrors(invalid).positions).toEqual({ row: { available: '可用数量不能超过持仓数量' } })
  expect(() => signalAccountConfig(invalid)).toThrow('可用数量不能超过持仓数量')
  expect(() => signalAccountConfig({ ...funded, mode: 'function' })).toThrow('示例快照用于测试')
})

test('填写持仓数量时同步自动补全的可用数量，保留手填的零', () => {
  const row = { id: 'row', symbol: '600000.SH', direction: '多头' as const, volume: '', available: '' }
  const first = { ...row, ...positionVolumePatch(row, '1') }
  const completed = { ...first, ...positionVolumePatch(first, '1000') }
  expect(completed.available).toBe('1000')
  const draft = { ...initialSignalDraft, equity: '100000', cash: '80000', positionsKnown: true }
  expect(signalAccountConfig({ ...draft, positions: [completed] }).manual_assets?.positions?.[0]?.available_volume).toBe(1000)
  expect(positionVolumePatch({ ...completed, available: '0', availableFromVolume: false }, '2000')).toEqual({ volume: '2000' })
  expect(positionVolumePatch({ ...completed, available: '', availableFromVolume: false }, '2000').available).toBe('2000')
})

test('填写账户权益时同步自动补全的可用资金，保留手填和已保存的金额', () => {
  const first = { ...initialSignalDraft, ...signalEquityPatch(initialSignalDraft, '1') }
  expect(first.cash).toBe('1')
  const completed = { ...first, ...signalEquityPatch(first, '100000') }
  expect(signalAccountConfig(completed).manual_assets?.available_cash).toBe(100000)
  const manual = { ...completed, cash: '0', cashFromEquity: false }
  expect(signalEquityPatch(manual, '200000')).toEqual({ equity: '200000' })
  expect(signalEquityPatch({ ...manual, cash: '' }, '200000').cash).toBe('200000')
  const saved = configToSignalDraft({
    ...signalAccountConfig(completed), quote_source: { kind: 'gm', token: 'fixture', connection_mode: 'terminal', serv_addr: null },
  })
  expect(signalEquityPatch(saved, '200000')).toEqual({ equity: '200000' })
})

test('预置行情只提交当前渠道字段，并恢复自定义 API URL', () => {
  const gm = { ...initialSignalDraft, quoteKind: 'gm' as const, gmToken: 'gm-fixture' }
  expect(signalQuoteErrors(gm)).toEqual({ gmToken: null, gmAddress: '请填写终端 RPC 地址' })
  expect(signalQuoteErrors({ ...gm, gmToken: '', gmMode: 'terminal' })).toEqual({ gmToken: '请填写掘金 Token', gmTerminalPath: '请填写本机终端目录' })
  const terminalPath = 'C:\\Program Files\\GoldMiner3'
  const local = { ...gm, gmMode: 'terminal' as const, gmTerminalPath: terminalPath }
  expect(signalQuoteConfig(local)).toEqual({ kind: 'gm', token: 'gm-fixture', connection_mode: 'terminal', serv_addr: null, terminal_path: terminalPath })
  expect(signalQuoteConfig({ ...local, gmMode: 'service', gmAddress: '127.0.0.1:7001' })).toEqual({
    kind: 'gm', token: 'gm-fixture', connection_mode: 'service', serv_addr: '127.0.0.1:7001',
  })
  const legacy = { kind: 'gm', token: 'gm-fixture', connection_mode: 'terminal', serv_addr: null } as const
  expect(signalQuoteConfig(configToSignalDraft({ manual_assets: null, account_code: null, quote_source: legacy }))).toEqual(legacy)
  expect(() => signalQuoteConfig(gm)).toThrow('终端 RPC 地址')
  const url = 'https://quotes.example.test/v1/tushare'
  const tushare = { ...gm, equity: '10', cash: '10', quoteKind: 'tushare' as const, tushareToken: 'ts-fixture' }
  expect(signalQuoteConfig({ ...tushare, tushareUrl: '' })).toEqual({ kind: 'tushare', token: 'ts-fixture', url: TUSHARE_API_URL })
  const config = signalConfig({ ...tushare, tushareUrl: url })
  expect(config.quote_source).toEqual({ kind: 'tushare', token: 'ts-fixture', url })
  expect(signalConfig(configToSignalDraft(config))).toEqual(config)
})

test('期货行情只保存所选来源的字段，编辑时恢复已保存的配置', () => {
  const draft = {
    ...initialSignalDraft, equity: '100000', cash: '80000',
    md_front: 'tcp://quotes.example.test:15000', tq_username: 'fixture-user', tq_password: ' fixture-password ',
  }
  const ctp = signalConfig({ ...draft, quoteKind: 'ctp' })
  expect(ctp.quote_source).toEqual({ kind: 'ctp', md_front: draft.md_front })
  expect(signalConfig(configToSignalDraft(ctp))).toEqual(ctp)
  expect(signalQuoteErrors({ ...draft, quoteKind: 'ctp', md_front: 'https://quotes.example.test:15000' }).md_front).toBeTruthy()

  const tq = signalConfig({ ...draft, quoteKind: 'tq' })
  expect(tq.quote_source).toEqual({ kind: 'tq', tq_username: draft.tq_username, tq_password: draft.tq_password })
  expect(signalConfig(configToSignalDraft(tq))).toEqual(tq)
  expect(signalQuoteErrors({ ...draft, quoteKind: 'tq', tq_password: '' }).tq_password).toBe('请填写天勤密码')
})


test('开户提交保存期初现金和手续费，自定义账户函数关闭跟随', () => {
  const draft = { ...initialSignalDraft, cash: '100000', quoteKind: 'tushare' as const, tushareToken: 'fixture', followTarget: true }
  const config = signalConfig(draft, 'A股')
  expect(config.follow_target).toBe(true)
  expect(config.follow_fee_bps).toBe(5)
  expect(config.manual_assets?.available_cash).toBe(100000)
  expect(signalConfig({ ...draft, followFeeBps: '0' }, 'A股').follow_fee_bps).toBe(0)
  expect(signalAccountErrors({ ...draft, followFeeBps: '-1' }, 'A股').followFeeBps).toBeTruthy()
  expect(configToSignalDraft(config).followTarget).toBe(true)
  expect(configToSignalDraft(config).followFeeBps).toBe('5')
  expect(signalConfig({ ...draft, mode: 'function', accountCode: 'def fetch_account(context): return {}' }, 'A股').follow_target).toBe(false)
})
