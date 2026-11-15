import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'

test.each([[true, 'trade'], [false, 'trade'], [true, 'empty'], [false, 'empty']] as const)('固定品种算法保存、取消和删除（缓存预览：%s，入口：%s）', (preview, slot) => {
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
      algorithm: { method: 'SINGLE-MAKER', params: { max_wait_seconds: 60 } } };
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
    mock.module(ui + 'features/setup/algorithms.ts', () => ({ algorithmRefOf: v => v ?? null, describeAlgorithmRef: v => v?.method ?? null, validateAlgorithmRef: () => null }));
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
    const bar = () => nodes.find(n => n.type === 'EditSaveBar');
    const picker = () => nodes.find(n => n.type === 'AlgorithmScopePicker');
    const refresh = () => { nodes = all(render()); };
    const editor = () => nodes.find(n => n.type === 'AlgorithmEditor' && n.props.value?.method === 'TWAP');
    const save = async () => { bar().props.onSave(); await new Promise(resolve => setImmediate(resolve)); refresh(); };
    expect(bar().props.changes).toEqual([]);
    expect(nodes.filter(n => n.type === 'Section').map(n => n.props.label)).toEqual(['主交易算法', '清仓算法']);
    picker().props.onAdd('SHFE.ag'); refresh();
    expect(picker().props.value).toBe('SHFE.ag');
    expect(bar().props.changes).toEqual(['品种算法已改']);
    const product = nodes.filter(n => n.type === 'AlgorithmEditor' && n.props.slot === 'trade').at(-1);
    product.props.onChange({ method: 'TWAP', params: { total_duration: 30 } }); refresh();
    editor().props.onValidationError('参数错误'); refresh();
    expect(bar().props.blocked).toBe(true);
    await save(); expect(patches).toHaveLength(0);
    editor().props.onValidationError(null); refresh();
    await save();
    expect(patches).toEqual([{ product_algorithms: { 'SHFE.ag': { algorithm: { method: 'TWAP', params: { total_duration: 30 } } } } }]);
    expect(bar().props.changes).toEqual([]);
    expect(account.algorithm.params.max_wait_seconds).toBe(60);
    picker().props.onRestoreDefault(); refresh();
    expect(picker().props.products).toEqual([]);
    expect(bar().props.changes).toEqual(['品种算法已改']);
    bar().props.onCancel(); refresh();
    expect(picker().props.products).toEqual(['SHFE.ag']);
    expect(bar().props.changes).toEqual([]);
    picker().props.onChange('SHFE.ag'); refresh();
    picker().props.onRestoreDefault(); refresh();
    await save();
    expect(patches.at(-1)).toEqual({ product_algorithms: {} });
    picker().props.onAdd('SHFE.rb'); refresh();
    nodes.filter(n => n.type === 'AlgorithmEditor' && n.props.slot === 'trade').at(-1).props.onValidationError('非法'); refresh();
    expect(bar().props.blocked).toBe(true);
    bar().props.onCancel(); refresh();
    expect(picker().props.products).toEqual([]);
    expect(bar().props.blocked).toBe(false);
  `;
  const run = spawnSync('bun', ['--eval', script], { cwd: new URL('../../', import.meta.url).pathname, encoding: 'utf8' });
  expect(run.stderr).toBe('');
  expect(run.status).toBe(0);
})
