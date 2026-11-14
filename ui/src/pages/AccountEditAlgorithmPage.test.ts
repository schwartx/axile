import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'

test.each([[true, 'trade'], [false, 'trade'], [true, 'empty'], [false, 'empty']] as const)('通用设置、清仓入口和保存取消（缓存预览：%s，入口：%s）', (preview, slot) => {
  const script = `
    import { expect, mock } from 'bun:test';
    const ui = ${JSON.stringify(new URL('../', import.meta.url).pathname)};
    const slots = []; const effects = []; let at = 0;
    mock.module('react', () => ({
      useState(initial) { const i = at++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
        return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }]; },
      useRef: value => ({ current: value }), useCallback: fn => fn, useEffect: fn => { effects.push(fn); },
    }));
    const jsx = (type, props) => ({ type, props });
    mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx }));
    mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx }));
    mock.module('react-router', () => ({ useParams: () => ({ id: '7' }), useViewTransitionState: () => false }));
    mock.module('lucide-react', () => ({ Info() {} }));
    const account = { account_id: 7, name: 'demo', trade_channel: 'test', execution_timeout: 300,
      algorithm: { name: 'twap', params: { max_wait_seconds: 60 } } };
    const patches = [];
    mock.module(ui + 'lib/api/accounts.ts', () => ({ getAccount: async () => account,
      updateAccount: async (_, patch) => { patches.push(patch); Object.assign(account, patch); return { ...account } } }));
    mock.module(ui + 'lib/hooks/usePolling.ts', () => ({ usePolling: () => ({ data: account }) }));
    mock.module(ui + 'stores/domain.ts', () => ({ useDomainStore: select => select({ accounts: [], refreshAccounts() {} }) }));
    const messages = [];
    mock.module(ui + 'stores/ui.ts', () => ({ useToastStore: select => select({ toast(message) { messages.push(message) } }) }));
    mock.module(ui + 'stores/channels.ts', () => ({ getChannelDescriptor: () => null,
      useChannelDescriptor: () => ({ defaults: { trade_algorithm: account.algorithm }, ui: {} }) }));
    mock.module(ui + 'features/account/algorithmNameTransition.ts', () => ({
      readAlgorithmAccountPreview: () => ${preview ? 'account' : 'null'}, writeAlgorithmAccountPreview() {}, algorithmTransitionSlot: () => ${JSON.stringify(slot)},
    }));
    mock.module(ui + 'features/account/configSummary.ts', () => ({ accountConfigVtName() {}, readAccountConfigSummary() {}, writeAccountConfigSummary() {} }));
    mock.module(ui + 'features/setup/algorithms.ts', () => ({ algorithmRefOf: v => v ?? null, describeAlgorithmRef: v => v?.name ?? null, validateAlgorithmRef: () => null }));
    mock.module(ui + 'features/account/futuresClosePolicy.ts', () => ({ defaultClosePolicy: () => ({}) }));
    mock.module(ui + 'features/account/useAccountRuntimeSync.ts', () => ({ useAccountRuntimeSync: () => ({ invalidate() {}, acceptSaved() {} }), runtimeSyncMessage: () => '' }));
    mock.module(ui + 'lib/viewTransition.ts', () => ({ withViewTransition: fn => fn() }));
    for (const [path, names] of [
      ['features/account/editUi.tsx', ['EditError', 'EditLoading', 'EditSaveBar', 'EditSynopsis', 'Row', 'Section', 'Toggle']],
      ['features/account/pageHead.tsx', ['AccountPageTitle']], ['features/setup/AlgorithmEditor.tsx', ['AlgorithmEditor']],
      ['features/account/FuturesClosePolicyEditor.tsx', ['FuturesClosePolicyEditor']], ['components/ui/Tooltip.tsx', ['Tooltip']],
      ['features/account/RuntimeSyncStatus.tsx', ['RuntimeSyncStatus']], ['features/replay/ReplayRecordingStatus.tsx', ['ReplayRecordingStatus']],
      ['features/account/AlgorithmWorkbench.tsx', ['AlgorithmWorkbench']],
      ['features/account/AlgorithmScopePicker.tsx', ['AlgorithmScopePicker']],
      ['features/account/ExecutionTimeoutInput.tsx', ['ExecutionTimeoutInput']],
      ['components/ui/Segmented.tsx', ['Segmented']],
    ]) mock.module(ui + path, () => Object.fromEntries(names.map(name => [name, name])));
    const { AccountEditAlgorithmPage } = await import(ui + 'pages/AccountEditAlgorithmPage.tsx');
    const form = AccountEditAlgorithmPage();
    const render = () => { at = 0; const tree = form.type(form.props); for (const effect of effects.splice(0)) effect(); return tree; };
    function all(node) { return Array.isArray(node) ? node.flatMap(all) : node?.props ? [node, ...all(node.props.children), ...all(node.props.footer), ...all(node.props.saveBar)] : [] }
    let nodes = all(render());
    nodes = all(render());
    const toggle = () => nodes.find(n => n.type === 'Toggle');
    const bar = () => nodes.find(n => n.type === 'EditSaveBar');
    const timeout = () => nodes.find(n => n.type === 'ExecutionTimeoutInput');
    expect(nodes.filter(n => n.type === 'ExecutionTimeoutInput')).toHaveLength(1);
    expect(timeout().props.value).toBe('300');
    expect(bar().props.saveLabel).toBe('保存全部');
    const common = nodes.find(n => n.props['aria-label'] === '通用');
    expect(all(common).find(n => n.type === 'ExecutionTimeoutInput')).toBeDefined();
    expect(all(common).find(n => n.type === 'Toggle')).toBeDefined();
    expect(nodes.find(n => n.type === 'AlgorithmWorkbench').props.initialSlot).toBe(${JSON.stringify(slot)});
    expect(nodes.find(n => n.type === 'AlgorithmWorkbench').props.showEmpty).toBe(true);
    toggle().props.onClick(); nodes = all(render());
    const pending = bar().props.changes;
    bar().props.onCancel(); nodes = all(render());
    const cancelled = toggle().props.on === false && bar().props.changes.length === 0;
    toggle().props.onClick(); nodes = all(render());
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); nodes = all(render());
    const saved = toggle().props.on === true && bar().props.changes.length === 0;
    toggle().props.onClick(); nodes = all(render());
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve));
    nodes = all(render());
    const schedule = () => nodes.find(n => n.type === 'AlgorithmWorkbench');
    const rule = { key: 'draft-only', start: '09:30', end: '10:00', algorithm: { method: 'TWAP', params: { slices: 4 } } };
    schedule().props.onScheduleChange([rule]); nodes = all(render());
    const schedulePending = bar().props.changes;
    bar().props.onCancel(); nodes = all(render());
    const scheduleCancelled = schedule().props.schedule.length === 0 && bar().props.changes.length === 0;
    schedule().props.onScheduleChange([rule]); nodes = all(render());
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); nodes = all(render());
    const scheduleSaved = schedule().props.schedule.length === 1 && bar().props.changes.length === 0;
    timeout().props.onChange('180'); nodes = all(render());
    expect(bar().props.changes).toEqual(['执行超时 5 分 → 3 分']);
    bar().props.onCancel(); nodes = all(render());
    expect(timeout().props.value).toBe('300');
    expect(bar().props.changes).toEqual([]);
    for (const value of ['0', '-1', '1.5', 'abc', 'NaN']) {
      timeout().props.onChange(value); nodes = all(render());
      expect(timeout().props.invalid).toBe(true);
      expect(bar().props.blocked).toBe(true);
      bar().props.onSave(); await new Promise(resolve => setImmediate(resolve));
      expect(patches).toHaveLength(3);
    }
    timeout().props.onChange('180'); nodes = all(render());
    expect(bar().props.blocked).toBe(false);
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); nodes = all(render());
    expect(timeout().props.value).toBe('180');
    expect(bar().props.changes).toEqual([]);
    expect(account.algorithm.params.max_wait_seconds).toBe(60);
    timeout().props.onChange(''); nodes = all(render());
    expect(bar().props.changes).toEqual([]);
    expect(bar().props.blocked).toBe(false);
    toggle().props.onClick(); nodes = all(render());
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); nodes = all(render());
    expect(timeout().props.value).toBe('180');
    const picker = () => nodes.find(n => n.type === 'AlgorithmScopePicker');
    expect(bar().props.changes).toEqual([]);
    expect(bar().props.blocked).toBe(false);
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve));
    expect(patches).toHaveLength(5);
    picker().props.onAdd('ag'); nodes = all(render());
    expect(picker().props.products).toEqual(['ag']);
    const product = () => nodes.find(n => n.type === 'AlgorithmWorkbench' && n.props.showEmpty === false);
    expect(product().props.showEmpty).toBe(false);
    product().props.onTradeChange({ method: 'TWAP', params: { slices: 3 } }); nodes = all(render());
    product().props.onScheduleChange([rule]); nodes = all(render());
    expect(bar().props.changes).toEqual(['品种算法已改']);
    picker().props.onChange(''); nodes = all(render());
    expect(product().props.trade.params.slices).toBe(3);
    bar().props.onCancel(); nodes = all(render());
    expect(picker().props.products).toEqual([]);
    picker().props.onAdd('ag'); nodes = all(render());
    product().props.onTradeChange({ method: 'TWAP', params: { slices: 3 } }); nodes = all(render());
    product().props.onScheduleChange([rule]); nodes = all(render());
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); nodes = all(render());
    expect(patches.at(-1)).toEqual({ product_algorithms: { ag: { algorithm: { method: 'TWAP', params: { slices: 3 } },
      algorithm_schedule: [{ start: '09:30', end: '10:00', algorithm: { method: 'TWAP', params: { slices: 4 } } }] } } });
    expect(bar().props.changes).toEqual([]);
    picker().props.onRestoreDefault(); nodes = all(render());
    expect(picker().props.value).toBe('');
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); nodes = all(render());
    expect(patches.at(-1)).toEqual({ product_algorithms: {} });
    expect(messages.at(-1)).toContain('已保存，后续调仓生效');
    patches.splice(-2);
    schedule().props.onEmptyChange({ method: 'TWAP', params: { slices: 2 } }); nodes = all(render());
    expect(bar().props.changes).toEqual(['清仓算法已改']);
    bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); nodes = all(render());
    expect(patches.pop()).toEqual({ empty_positions_algorithm: { method: 'TWAP', params: { slices: 2 } } });
    console.log(JSON.stringify({ pending, cancelled, saved, schedulePending, scheduleCancelled, scheduleSaved, patches }));
  `
  const result = spawnSync(process.execPath, ['-e', script], { cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  if (result.status !== 0) throw Error(result.stderr || String(result.error))
  expect(JSON.parse(result.stdout)).toEqual({ pending: ['执行回放录制：开启'], cancelled: true, saved: true,
    schedulePending: ['交易时段已改'], scheduleCancelled: true, scheduleSaved: true,
    patches: [{ replay_recording_enabled: true }, { replay_recording_enabled: false }, {
      algorithm_schedule: [{ start: '09:30', end: '10:00', algorithm: { method: 'TWAP', params: { slices: 4 } } }],
    }, { execution_timeout: 180 }, { replay_recording_enabled: true }] })
})
