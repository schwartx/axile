import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'

const ui = new URL('../../', import.meta.url).pathname
const hooks = `
  import { mock, expect } from 'bun:test';
  const slots = []; let at = 0;
  mock.module('react', () => ({
    useState(initial) { const i = at++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }]; },
    useRef(initial) { const i = at++; return slots[i] ??= { current: initial }; },
    useCallback: fn => fn, useEffect() {}, useId: () => 'leverage',
  }));
`
function check(script: string) {
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw new Error(result.stderr || String(result.error))
  expect(result.status).toBe(0)
}

test('杠杆只写改动字段，重复点击不重复提交，取消和失败保留正确状态', () => {
  check(hooks + `
    const ui = ${JSON.stringify(ui)};
    const jsx = (type, props) => ({ type, props });
    mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx, Fragment: 'Fragment' }));
    mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx, Fragment: 'Fragment' }));
    for (const [path, name] of [
      ['components/ui/ErrorNotice.tsx', 'ErrorNotice'],
      ['components/ui/OverflowText.tsx', 'OverflowText'], ['components/ui/InkRewrite.tsx', 'InkRewrite'],
      ['components/ui/Select.tsx', 'Select'], ['features/account/LeverageInput.tsx', 'LeverageInput'],
      ['features/account/AccountConnectionStatus.tsx', 'AccountConnectionStatus'],
    ]) mock.module(ui + path, () => ({ [name]: name }));
    const channel = { ui: { show_short_leverage: true }, leverage: { min: 0, max: 5, step: 0.5 }, defaults: { long_leverage: 1, short_leverage: 1 } };
    mock.module(ui + 'stores/channels.ts', () => ({ useChannelDescriptor: () => channel, useChannelCatalogStore() {} }));
    const domain = { accounts: [{ account_id: 7, name: 'demo', long_leverage: 3, short_leverage: 2 }] };
    const useDomainStore = select => select(domain);
    useDomainStore.setState = update => Object.assign(domain, update(domain));
    useDomainStore.getState = () => ({ ...domain, refreshAccounts: async () => {} });
    mock.module(ui + 'stores/domain.ts', () => ({ useDomainStore }));
    mock.module(ui + 'stores/liveConnection.ts', () => ({ useAccountConnection() {}, useLiveConnection() {} }));
    mock.module(ui + 'stores/connectionAlerts.ts', () => ({ useConnectionAlerts: {} }));
    mock.module(ui + 'features/account/useConnectionFeedback.ts', () => ({ useConnectionFeedback() {} }));
    mock.module(ui + 'features/dashboard/display.ts', () => ({ channelLabel() {} }));
    const patches = []; let finish; let fail = false;
    mock.module(ui + 'lib/api/accounts.ts', () => ({ updateAccount: async (id, patch) => {
      expect(id).toBe(7); patches.push(patch);
      if (fail) throw new Error('保存失败');
      await new Promise(resolve => { finish = resolve });
      return { ...domain.accounts[0], ...patch, algorithm: null, empty_positions_algorithm: null };
    } }));
    const { AccountLeverageControl } = await import(ui + 'features/portfolio/PortfolioTrialAccounts.tsx');
    const changes = [];
    const render = () => { at = 0; return AccountLeverageControl({ account: domain.accounts[0], disabled: false, onLeverageChange: (id, value, invalid) => changes.push([id, value, invalid]) }) };
    function all(node) { return Array.isArray(node) ? node.flatMap(all) : node?.props ? [node, ...all(node.props.children), ...all(node.props.footer)] : [] }
    let nodes;
    const refresh = () => { nodes = all(render()) };
    const button = label => nodes.find(n => n.type === 'button' && (n.props.children === label || n.props['aria-label'] === label || n.props.children?.props?.text === label));
    const input = suffix => nodes.find(n => n.type === 'LeverageInput' && n.props.id.endsWith(suffix));
    refresh();
    expect(input('-long').props.value).toBe('3');
    expect(nodes.some(n => n.type === 'CenteredModal')).toBe(false);
    input('-long').props.onChange('-1'); refresh();
    expect(button('保存到账户').props.disabled).toBe(true);
    button('保存到账户').props.onClick(); await new Promise(setImmediate); expect(patches).toHaveLength(0);
    input('-long').props.onChange('4'); refresh(); input('-short').props.onChange('0'); refresh();
    expect(patches).toHaveLength(0);
    expect(changes.at(-1)).toEqual([7, { long_leverage: 4, short_leverage: 0 }, false]);
    button('保存到账户').props.onClick(); button('保存到账户').props.onClick(); refresh();
    expect(patches).toEqual([{ long_leverage: 4, short_leverage: 0 }]);
    expect(nodes.find(n => n.type === 'fieldset').props.disabled).toBe(true);
    finish(); await new Promise(setImmediate); refresh();
    expect(domain.accounts[0].long_leverage).toBe(4); expect(domain.accounts[0].short_leverage).toBe(0);
    expect(input('-long').props.value).toBe('4');
    expect(changes.at(-1)).toEqual([7, null, false]);
    input('-long').props.onChange('2'); refresh();
    button('还原').props.onClick(); refresh(); expect(patches).toHaveLength(1);
    expect(input('-long').props.value).toBe('4');
    input('-long').props.onChange('3.5'); refresh(); fail = true;
    button('保存到账户').props.onClick(); await new Promise(setImmediate); refresh();
    expect(input('-long').props.value).toBe('3.5');
    expect(changes.at(-1)).toEqual([7, { long_leverage: 3.5, short_leverage: 0 }, false]);
    expect(nodes.find(n => n.type === 'ErrorNotice').props.error.message).toBe('保存失败');
    button('还原').props.onClick(); refresh(); fail = false; channel.ui.show_short_leverage = false;
    input('-long').props.onChange('2'); refresh();
    expect(nodes.filter(n => n.type === 'LeverageInput')).toHaveLength(1);
    button('保存到账户').props.onClick(); finish(); await new Promise(setImmediate); refresh();
    expect(patches.at(-1)).toEqual({ long_leverage: 2 });
  `)
})

test('连接和草稿状态约束全部试跑入口，连接就绪不自动运行，杠杆改动使旧结果过期', () => {
  check(hooks + `
    const ui = ${JSON.stringify(ui)};
    const domain = { accounts: [{ account_id: 7, market: '期货', long_leverage: 3, short_leverage: 2 }] };
    const channels = [{ market: 'ctp', portfolio: { market_label: '期货' } }];
    const connection = { status: 'unknown' }; const operation = { pending: null };
    mock.module(ui + 'stores/domain.ts', () => ({ useDomainStore: select => select(domain) }));
    mock.module(ui + 'stores/channels.ts', () => ({ useChannelCatalogStore: select => select({ channels }) }));
    mock.module(ui + 'stores/liveConnection.ts', () => ({ useAccountConnection: () => connection }));
    mock.module(ui + 'stores/connectionOperations.ts', () => ({ useConnectionOperation: () => operation }));
    const requests = [];
    mock.module(ui + 'lib/api/portfolios.ts', () => ({ validateCustomCalc: async body => { requests.push(body); return { valid: true, target: { A: body.leverage_overrides?.long_leverage ?? 1 } } } }));
    const { useCustomCalcValidation } = await import(ui + 'features/portfolio/useCustomCalcValidation.ts');
    const render = () => { at = 0; return useCustomCalcValidation('return {}', 'ctp') };
    let trial = render(); let calls = 0;
    const execute = async () => { calls++; return { valid: true, target: { A: 1 } } };
    expect(trial.canRun).toBe(true); trial.setAccountId(7); trial = render();
    expect(trial.canRun).toBe(false); await trial.run(execute); expect(calls).toBe(0);
    connection.status = 'disconnected'; trial = render();
    expect(trial.canRun).toBe(false); await trial.run(execute); expect(calls).toBe(0);
    connection.status = 'ready'; trial = render(); expect(calls).toBe(0);
    expect(trial.canRun).toBe(true); await trial.run(execute); trial = render();
    expect(calls).toBe(1); expect(trial.stale).toBe(false);
    domain.accounts[0].long_leverage = 4; trial = render(); expect(trial.stale).toBe(true);
    trial.setLeverageDraft(7, { long_leverage: 4, short_leverage: 0 }, false); trial = render();
    expect(trial.canRun).toBe(true); await trial.run(); trial = render();
    expect(requests).toHaveLength(1); expect(requests[0].leverage_overrides).toEqual({ long_leverage: 4, short_leverage: 0 });
    expect(trial.stale).toBe(false);
    domain.accounts[0].short_leverage = 0; trial.setLeverageDraft(7, null, false); trial = render();
    expect(trial.stale).toBe(false);
    trial.setLeverageDraft(7, { long_leverage: 5, short_leverage: 0 }, false); trial = render(); expect(trial.stale).toBe(true);
    trial.setLeverageDraft(7, null, true); trial = render();
    expect(trial.canRun).toBe(false); await trial.run(execute); expect(calls).toBe(1);
    trial.setAccountId(null); trial = render(); expect(trial.canRun).toBe(true);
    trial.setAccountId(7); trial = render(); expect(trial.canRun).toBe(false);
    trial.setLeverageDraft(7, null, false); operation.pending = 'reconnect'; trial = render();
    expect(trial.canRun).toBe(false); await trial.run(execute); expect(calls).toBe(1);
    operation.pending = null; trial = render(); expect(trial.canRun).toBe(true);
  `)
})

test('十五个候选账户只渲染当前账户摘要，样例不显示杠杆和连接操作', () => {
  check(`
    import { mock, expect } from 'bun:test';
    import { createElement } from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    const ui = ${JSON.stringify(ui)};
    const { useDomainStore } = await import(ui + 'stores/domain.ts');
    const { useChannelCatalogStore } = await import(ui + 'stores/channels.ts');
    const { useLiveConnection } = await import(ui + 'stores/liveConnection.ts');
    const accounts = Array.from({ length: 15 }, (_, i) => ({ account_id: i + 1, name: '候选账户' + i,
      market: '期货', trade_channel: 'ctp', portfolio_id: 9, long_leverage: 3, short_leverage: 0 }));
    const channel = { channel: 'ctp', label: '天勤', market: 'ctp', ui: { show_short_leverage: true }, defaults: { long_leverage: 1, short_leverage: 1 } };
    function seed(store, data) { store.setState(data); Object.assign(store.getInitialState(), data) }
    seed(useDomainStore, { accounts });
    seed(useChannelCatalogStore, { channels: [channel] });
    seed(useLiveConnection, { connected: true, frames: { 1: { status: 'ready', observed_at: null, error: null } } });
    const { PortfolioTrialAccounts } = await import(ui + 'features/portfolio/PortfolioTrialAccounts.tsx');
    const render = accountId => renderToStaticMarkup(createElement(PortfolioTrialAccounts, {
      accounts, accountId, portfolioId: 9, onSelect() {}, onLeverageChange() {}, disabled: false,
    }));
    const html = render(1);
    expect(html).toContain('候选账户0');
    expect(html).not.toContain('候选账户1');
    expect(html.match(/role="spinbutton"/g)).toHaveLength(2);
    expect(html).toContain('aria-valuenow="0"');
    expect(html).not.toContain('role="dialog"');
    expect(html).toContain('已连接');
    expect(html).toContain('rounded-chip bg-fill');
    expect(html).not.toContain('天勤 · 期货');
    expect(html).not.toContain('减少 1 倍杠杆');
    expect(html).not.toContain('增加 1 倍杠杆');
    expect(html).toContain('h-7 rounded-md');
    expect(html).toContain('减少 0.1 倍杠杆');
    const { LeverageInput } = await import(ui + 'features/account/LeverageInput.tsx');
    const full = renderToStaticMarkup(createElement(LeverageInput, { value: '3', onChange() {} }));
    expect(full).toContain('减少 1 倍杠杆');
    expect(full).toContain('增加 1 倍杠杆');
    expect(full.match(/<button/g)).toHaveLength(4);
    expect(html).toContain('role="switch"');
    expect(html).toContain('aria-checked="true"');
    expect(html).toContain('查看连接详情');
    channel.ui.show_short_leverage = false;
    expect(render(1).match(/role="spinbutton"/g)).toHaveLength(1);
    const sample = render(null);
    expect(sample).toContain('不连接真实渠道');
    expect(sample).not.toContain('role="switch"');
    expect(sample).not.toContain('role="spinbutton"');
    expect(sample.match(/<button/g)).toHaveLength(1);
  `)
})
