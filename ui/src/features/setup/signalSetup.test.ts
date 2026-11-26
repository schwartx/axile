import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'

import { flowOf } from '@/features/setup/steps'
import { SignalNotificationSetup } from '@/features/signal/SignalNotificationSetup'
import { configToSignalDraft, initialSignalDraft, positionVolumePatch, refreshSignalQuoteExample, SIGNAL_CHANNEL, signalAccountConfig, signalAccountErrors, signalConfig, signalQuoteConfig, signalQuoteErrors, TUSHARE_API_URL } from '@/features/signal/signalData'
import { SignalSetupPage } from '@/pages/setup/SignalSetupPage'
import { useChannelCatalogStore } from '@/stores/channels'
import type { ChannelCapability } from '@/types/api'

test('信号向导依次进入行情、资金、定时、通知和确认', () => {
  const path = '/setup/acct/signal'
  const { steps } = flowOf(path)
  expect(steps.map((step) => step.label)).toEqual([
    '选渠道', '账户与组合', '设置行情', '资金与持仓', '定时', '通知', '试算与确认',
  ])
  for (const [index, step] of steps.entries()) {
    const search = new URL(step.path, 'http://localhost').search
    expect(flowOf(index === 0 ? step.path : path, search, true).index).toBe(index)
  }
})

test('信号向导复用定时预览，并在单独的一步配置通知', () => {
  const catalog = useChannelCatalogStore.getInitialState()
  const previousChannels = catalog.channels
  catalog.channels = [{ channel: SIGNAL_CHANNEL, schedule: { kind: 'continuous' } } as ChannelCapability]
  try {
    const render = (step: number) => renderToStaticMarkup(createElement(MemoryRouter, {
      initialEntries: [`/setup/acct/signal?step=${step}`],
    }, createElement(SignalSetupPage)))

    const timer = render(5)
    expect(timer).toContain('aria-label="自动生成信号" aria-checked="false"')
    expect(timer).toContain('排程预览 · 北京时间')
    expect(timer).toContain('<aside')
    expect(timer).not.toContain('补发')
    expect(timer).not.toContain('飞书 Webhook')

    const notification = render(6)
    expect(notification).toContain('aria-label="通知方式"')
    expect(notification).not.toContain('不通知')
    expect(notification).toContain('飞书 Webhook')
    expect(notification).toContain('自定义函数')
    expect(notification).toContain('>跳过通知</button>')
    expect(notification).toContain('disabled="">下一步</button>')
    expect(notification).not.toContain('排程预览')
    const confirmation = render(7)
    expect(confirmation).toContain('试算目标数量')
    expect(confirmation).toContain('>通知</dt><dd>未配置</dd>')
  } finally {
    catalog.channels = previousChannels
  }
})

test('飞书通知显示录入状态并默认明文显示 Webhook', () => {
  const render = (webhook: string) => renderToStaticMarkup(createElement(SignalNotificationSetup, {
    mode: 'feishu', webhook, code: '', heading: null, footer: null,
    onModeChange: () => {}, onWebhookChange: () => {}, onCodeChange: () => {},
  }))
  expect(render('  ')).toContain('role="status">待填写')
  const filled = render('fixture-key')
  expect(filled).toContain('role="status">已填写')
  expect(filled).toContain('aria-label="飞书 Webhook" type="text"')
  expect(filled).toContain('placeholder="可粘贴整条 webhook 链接"')
})

test('行情和资金各自验证本步配置', () => {
  expect(initialSignalDraft.quoteKind).toBe('gm')
  expect(() => signalQuoteConfig(initialSignalDraft)).toThrow('掘金 Token')
  expect(() => signalQuoteConfig({ ...initialSignalDraft, quoteKind: '' })).toThrow('请选择行情来源')
  const custom = { ...initialSignalDraft, quoteKind: 'custom' as const }
  expect(() => signalQuoteConfig(custom)).toThrow('示例行情用于测试')
  expect(() => signalQuoteConfig({ ...custom, quoteCode: ' ' })).toThrow('请填写行情函数')
  expect(signalQuoteConfig({ ...custom, quoteCode: 'def fetch_quotes(symbols):\n    return {}' }).kind).toBe('custom')

  expect(() => signalAccountConfig(initialSignalDraft)).toThrow('账户权益')
  const draft = { ...initialSignalDraft, quoteCode: '', equity: '100000', cash: '0' }
  expect(signalAccountConfig(draft).manual_assets).toEqual({
    total_asset: 100000, available_cash: 0, currency: 'CNY', positions: null,
  })
})

test('只替换原样的旧行情示例', () => {
  const oldExample = `from axile.executor.models.unified_price import UnifiedPriceData

def fetch_quotes(symbols: list[str]) -> dict[str, UnifiedPriceData]:
    # 每个标的返回 UnifiedPriceData。bids 和 asks 分别是 [(价格, 数量), ...]。
    raise NotImplementedError("请接入你的行情来源")
`
  const edited = oldExample.replace('raise NotImplementedError("请接入你的行情来源")', 'return {}')
  expect(refreshSignalQuoteExample(oldExample)).toBe(initialSignalDraft.quoteCode)
  expect(refreshSignalQuoteExample(edited)).toBe(edited)
})

test('原样的一档行情示例升级为五档，用户改过的代码保留', () => {
  const oneLevel = `from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.models.unified_price import UnifiedPriceData

def fetch_quotes(symbols: list[str]) -> dict[str, UnifiedPriceData]:
    # 测试用的演示行情。接入实际行情时，替换下方的价格和盘口数据。
    now = clock_now()
    return {
        symbol: UnifiedPriceData(
            symbol=symbol,
            last_price=10.0,
            bids=[(9.99, 100.0)],
            asks=[(10.01, 100.0)],
            volume=1000.0,
            timestamp=int(now.timestamp() * 1000),
            update_time=now.isoformat(),
        )
        for symbol in symbols
    }
`
  expect(refreshSignalQuoteExample(oneLevel)).toBe(initialSignalDraft.quoteCode)
  expect(() => signalQuoteConfig({ ...initialSignalDraft, quoteKind: 'custom', quoteCode: oneLevel })).toThrow('示例行情用于测试')
  const edited = oneLevel.replace('last_price=10.0', 'last_price=11.0')
  expect(refreshSignalQuoteExample(edited)).toBe(edited)
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

test('预置行情只提交当前渠道字段，并恢复自定义 API URL', () => {
  const gm = { ...initialSignalDraft, quoteKind: 'gm' as const, gmToken: 'gm-fixture' }
  expect(signalQuoteErrors({ ...gm, gmToken: '' })).toEqual({ gmToken: '请填写掘金 Token', gmTerminalPath: '请填写本机终端目录' })
  expect(signalQuoteErrors({ ...gm, gmMode: 'service' })).toEqual({ gmToken: null, gmAddress: '请填写终端 RPC 地址' })
  const terminalPath = 'C:\\Program Files\\GoldMiner3'
  const local = { ...gm, gmTerminalPath: terminalPath }
  expect(signalQuoteConfig(local)).toEqual({ kind: 'gm', token: 'gm-fixture', connection_mode: 'terminal', serv_addr: null, terminal_path: terminalPath })
  expect(signalQuoteConfig({ ...local, gmMode: 'service', gmAddress: '127.0.0.1:7001' })).toEqual({
    kind: 'gm', token: 'gm-fixture', connection_mode: 'service', serv_addr: '127.0.0.1:7001',
  })
  const legacy = { kind: 'gm', token: 'gm-fixture', connection_mode: 'terminal', serv_addr: null } as const
  expect(signalQuoteConfig(configToSignalDraft({ manual_assets: null, account_code: null, quote_source: legacy }))).toEqual(legacy)
  expect(() => signalQuoteConfig({ ...gm, gmMode: 'service' })).toThrow('终端 RPC 地址')
  const url = 'https://quotes.example.test/v1/tushare'
  const tushare = { ...gm, equity: '10', cash: '10', quoteKind: 'tushare' as const, tushareToken: 'ts-fixture' }
  expect(signalQuoteConfig({ ...tushare, tushareUrl: '' })).toEqual({ kind: 'tushare', token: 'ts-fixture', url: TUSHARE_API_URL })
  const config = signalConfig({ ...tushare, tushareUrl: url })
  expect(config.quote_source).toEqual({ kind: 'tushare', token: 'ts-fixture', url })
  expect(signalConfig(configToSignalDraft(config))).toEqual(config)
})
