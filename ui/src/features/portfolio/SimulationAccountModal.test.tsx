/// <reference types="bun" />
import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'
import { simulationAccountDraft, validateSimulationAccount } from '@/features/portfolio/SimulationAccountModal'
import type { ChannelCapability, SimulationAccount } from '@/types/api'

const channel = { leverage: { min: 0, max: 5, step: 0.5 } } as ChannelCapability
const account: SimulationAccount = {
  account: { total_asset: 1000, available_cash: 500, market_value: null, currency: 'CNY', source: 'fixture', update_time: '2026-11-17', extra: { account_id: 'local' }, positions: [
    { symbol: 'SHFE.ag', volume: 2, available_volume: 1, market_value: null, avg_price: 100, direction: '多头', extra: { multiplier: 15, tick: 1 } },
  ] },
  quotes: [{ symbol: 'SHFE.ag', last_price: 100, volume: 10, turnover: 1000, bids: [[99, 2]], asks: [[101, 3]], timestamp: 123, update_time: '2026-11-17', extra: { multiplier: 15, tick: 1 } }],
  long_leverage: 1, short_leverage: 0,
}

test('模拟账户完整往返，空白未知值保留为 null，不修改输入对象', () => {
  const before = structuredClone(account)
  const draft = simulationAccountDraft(account)
  const expected = structuredClone(account)
  expected.account.market_value = 200
  expected.account.positions[0]!.market_value = 200
  expect(validateSimulationAccount(draft, channel)).toEqual({ value: expected, issues: [], marketValue: 200 })
  draft.positions[0]!.symbol = 'changed'
  expect(account).toEqual(before)
  draft.positions[0]!.symbol = account.account.positions[0]!.symbol
  draft.account.market_value = '  '
  expect(validateSimulationAccount(draft, channel).value?.account.market_value).toBe(200)
})

test('所有页签同时验证，空数值、非有限数、持仓量和重复行不能漏过', () => {
  const draft = simulationAccountDraft(account)
  draft.account.available_cash = ' '
  draft.account.total_asset = 'Infinity'
  draft.account.currency = ' '
  draft.positions[0]!.available_volume = '3'
  draft.positions.push({ ...draft.positions[0]!, id: 'duplicate' })
  draft.quotes[0]!.last_price = 'NaN'
  draft.quotes.push({ ...draft.quotes[0]!, id: 'duplicate' })
  const result = validateSimulationAccount(draft, channel)
  expect(result.value).toBeUndefined()
  for (const field of ['account.available_cash', 'account.total_asset', 'account.currency', `positions.${draft.positions[0]!.id}.available_volume`, 'positions.duplicate.symbol', `quotes.${draft.quotes[0]!.id}.last_price`, 'quotes.duplicate.symbol']) {
    expect(result.issues.some((issue) => issue.field === field)).toBe(true)
  }
  expect(new Set(result.issues.map((issue) => issue.section))).toEqual(new Set(['account', 'positions', 'quotes']))
})

test('方向、同标的不同方向、杠杆范围和步进逐项验证', () => {
  const draft = simulationAccountDraft(account)
  draft.positions.push({ ...draft.positions[0]!, id: 'short', direction: '空头' })
  expect(validateSimulationAccount(draft, channel).issues).toEqual([])
  draft.positions[0]!.direction = 'invalid'
  draft.positions[0]!.volume = '-1'
  draft.short_leverage = '5.5'
  draft.long_leverage = '1.2'
  const issues = validateSimulationAccount(draft, channel).issues
  expect(issues.some((issue) => issue.field.endsWith('.direction'))).toBe(true)
  expect(issues.some((issue) => issue.field.endsWith('.volume'))).toBe(true)
  expect(issues.find((issue) => issue.field === 'long_leverage')?.message).toContain('步进')
  expect(issues.find((issue) => issue.field === 'short_leverage')?.message).toContain('≤')
  for (const value of ['', '-0.5', 'Infinity']) {
    draft.long_leverage = value
    expect(validateSimulationAccount(draft, channel).issues.some((issue) => issue.field === 'long_leverage')).toBe(true)
  }
  const offsetChannel = { ...channel, leverage: { min: 0.25, max: 2.25, step: 0.5 } }
  draft.long_leverage = '0.5'
  expect(validateSimulationAccount(draft, offsetChannel).issues.some((issue) => issue.field === 'long_leverage')).toBe(true)
})

test('盘口逐档验证价格、数量和非有限数', () => {
  for (const [price, volume] of [['', '2'], ['0', '2'], ['1', '-2'], ['1e400', '2'], ['1', 'NaN']] as const) {
    const draft = simulationAccountDraft(account)
    draft.quotes[0]!.bids = [[price, volume]]
    expect(validateSimulationAccount(draft, channel).issues.some((issue) => issue.field.includes('.bids.0.'))).toBe(true)
  }
  const draft = simulationAccountDraft(account)
  draft.quotes[0]!.update_time = ' '
  draft.quotes[0]!.timestamp = ''
  draft.account.currency = 'US DT'
  expect(validateSimulationAccount(draft, channel).issues).toHaveLength(3)
})

test('保存前对齐渠道币种、资金、正价格、配套行情和期货参数约束', () => {
  const draft = simulationAccountDraft(account)
  draft.account.total_asset = '-1'
  draft.account.currency = 'USDT'
  draft.positions[0]!.avg_price = '-1'
  draft.positions[0]!.symbol = 'missing'
  draft.quotes[0]!.last_price = '0'
  draft.quotes[0]!.volume_multiple = ''
  const issues = validateSimulationAccount(draft, { ...channel, currency: 'CNY', units: { quantity_kind: 'contract' } } as ChannelCapability).issues
  for (const suffix of ['total_asset', 'currency', 'avg_price', 'symbol', 'last_price', 'volume_multiple', 'price_tick']) {
    expect(issues.some((issue) => issue.field.endsWith(suffix))).toBe(true)
  }
})

test('弹窗草稿跨页签保留、恢复可撤回、隐藏错误可定位，保存失败不关闭且取消不写回', () => {
  const script = `
    import { mock, expect } from 'bun:test';
    const slots = []; let at = 0; let effects = [];
    mock.module('react', () => ({
      Fragment: 'Fragment', useId: () => 'simulation',
      useState(initial) { const i = at++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }]; },
      useRef(initial) { const i = at++; return slots[i] ??= { current: initial }; },
      useEffect(effect) { effects.push(effect); },
    }));
    const jsx = (type, props) => ({ type, props });
    mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx, Fragment: 'Fragment' }));
    mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx, Fragment: 'Fragment' }));
    const ui = ${JSON.stringify(new URL('../../', import.meta.url).pathname)};
    for (const [path, name] of [['components/ui/AmountInput.tsx', 'AmountInput'], ['components/ui/CenteredModal.tsx', 'CenteredModal'], ['components/ui/Segmented.tsx', 'Segmented'], ['components/ui/Select.tsx', 'Select'], ['features/account/LeverageInput.tsx', 'LeverageInput']]) mock.module(ui + path, () => ({ [name]: name }));
    const { SimulationAccountModal } = await import(${JSON.stringify(import.meta.path.replace('.test.tsx', '.tsx'))});
    const original = ${JSON.stringify(account)};
    const preset = structuredClone(original); preset.account.available_cash = 900;
    const channel = { currency: 'CNY', label: '本地', ui: { long_leverage_label: '多头杠杆', short_leverage_label: '空头杠杆' }, leverage: { min: 0, max: 5, step: 0.5 }, simulation_account: preset };
    let saved = []; let closed = 0; let fail = false; let quoteStatus = 503;
    globalThis.fetch = async (url, request) => {
      expect(url).toBe('/api/v1/market-data/accounts/41/quotes');
      expect(JSON.parse(request.body)).toEqual({ symbols: ['SHFE.ag'], allow_stale: true });
      if (quoteStatus === 200) return Response.json({ data: { 'SHFE.ag': { ...original.quotes[0], last_price: 104 } } });
      const detail = quoteStatus === 504 ? '等待 CTP 行情回报超时' : 'CTP 行情订阅遭拒';
      return Response.json({ detail }, { status: quoteStatus });
    };
    const props = { channel, value: original, sources: [{ account_id: 41, name: 'CTP' }], onSave(value) { if (fail) throw new Error('存储已满'); saved.push(value) }, onClose() { closed++ } };
    function all(node) { return Array.isArray(node) ? node.flatMap(all) : node?.props ? [node, ...all(node.props.children), ...all(node.props.footer)] : [] }
    let nodes;
    function render() { at = 0; effects = []; nodes = all(SimulationAccountModal(props)); for (const effect of effects) effect(); }
    const button = label => nodes.find(n => n.type === 'button' && ([n.props.children].flat().join('') === label || n.props['aria-label'] === label));
    const field = name => nodes.find(n => n.props['aria-label'] === name || n.props.ariaLabel === name);
    const edit = (name, value) => { const node = field(name); node.props.onChange(node.type === 'AmountInput' ? value : { target: { value } }); render(); };
    const click = async label => { await button(label).props.onClick(); await new Promise(setImmediate); render(); };
    const tabs = () => nodes.find(n => n.type === 'Segmented');
    const tab = value => { tabs().props.onChange(value); render(); };
    render();
    expect(nodes.find(n => n.type === 'CenteredModal').props.open).toBe(true);
    expect(field('币种')).toBeUndefined();
    expect(field('账户权益').type).toBe('AmountInput');
    expect(field('账户权益').props.unit).toBe('元');
    edit('可用资金', '700');
    edit('持仓 1 持仓量 volume', '4');
    tab('quotes'); edit('行情 1 最新价 last_price', ''); tab('positions');
    expect(field('持仓 1 持仓量 volume').props.value).toBe('4');
    await click('保存配置'); expect(saved).toHaveLength(0); expect(closed).toBe(0);
    expect(tabs().props.value).toBe('quotes');
    expect(nodes.find(n => n.props.role === 'alert')).toBeDefined();
    edit('行情 1 最新价 last_price', '102');
    await click('恢复渠道预设'); expect(field('可用资金').props.value).toBe('900'); expect(saved).toHaveLength(0);
    await click('撤回恢复'); expect(field('可用资金').props.value).toBe('700'); expect(field('行情 1 最新价 last_price').props.value).toBe('102');
    await click('获取行情 1');
    expect(nodes.find(n => n.props.role === 'status').props.children).toContain('CTP 行情订阅遭拒');
    quoteStatus = 504;
    await click('获取行情 1');
    expect(nodes.find(n => n.props.role === 'status').props.children).toContain('等待 CTP 行情回报超时');
    expect(field('行情 1 最新价 last_price').props.value).toBe('102');
    quoteStatus = 200;
    await click('获取行情 1');
    expect(nodes.find(n => n.props.role === 'status').props.children).toContain('行情已填入草稿');
    expect(field('行情 1 最新价 last_price').props.value).toBe('104');
    fail = true; await click('保存配置'); expect(closed).toBe(0); expect(nodes.find(n => n.props.role === 'alert').props.children).toBe('存储已满');
    expect(field('可用资金').props.value).toBe('700'); expect(original.account.available_cash).toBe(500);
    await click('取消'); expect(closed).toBe(1); expect(saved).toHaveLength(0);
    fail = false; await click('保存配置'); expect(saved).toHaveLength(1); expect(closed).toBe(2);
    expect(saved[0].account.available_cash).toBe(700); expect(saved[0].account.positions[0].volume).toBe(4); expect(saved[0].quotes[0].last_price).toBe(104);
    tab('positions'); await click('添加持仓'); expect(field('持仓 2 标的 symbol')).toBeDefined();
    await click('删除持仓 2'); expect(field('持仓 2 标的 symbol')).toBeUndefined();
    tab('quotes'); await click('添加行情'); expect(field('行情 2 标的 symbol')).toBeDefined();
    await click('删除行情 2'); expect(field('行情 2 标的 symbol')).toBeUndefined();
  `
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  expect(result.status, result.stderr || String(result.error)).toBe(0)
})
