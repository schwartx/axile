import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'

test('未添加的输入保持局部状态，校验气泡随修改收起，仅网络错误提供重试', () => {
  const script = `
    import { expect, mock } from 'bun:test';
    const ui = ${JSON.stringify(new URL('../../', import.meta.url).pathname)};
    const states = [], dependencies = [], cleanups = [], effects = [], animations = []; let at = 0, refAt = 0;
    const productButton = { focus() {}, animate(frames, options) { const animation = { frames, options, cancel() {} }; animations.push(animation); return animation } };
    let reduceMotion = false;
    globalThis.window = { matchMedia: () => ({ matches: reduceMotion }) };
    globalThis.requestAnimationFrame = () => 0;
    globalThis.cancelAnimationFrame = () => {};
    mock.module('react', () => ({
      useState(initial) { const i = at++; if (!(i in states)) states[i] = initial;
        return [states[i], value => { states[i] = typeof value === 'function' ? value(states[i]) : value }]; },
      useRef: () => ({ current: refAt++ === 0 ? { querySelector: () => productButton } : refAt === 2 ? { offsetWidth: 34 } : null }),
      useEffect(effect, next) { const i = at++; const previous = dependencies[i];
        if (!previous || next.some((value, j) => value !== previous[j])) {
          effects.push(() => { cleanups[i]?.(); cleanups[i] = effect() }); dependencies[i] = next;
        }
      },
    }));
    const jsx = (type, props) => ({ type, props });
    mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx }));
    mock.module('react/jsx-dev-runtime', () => ({ jsxDEV: jsx }));
    mock.module('lucide-react', () => ({ Plus() {}, Trash2() {}, LoaderCircle() {} }));
    mock.module(ui + 'components/ui/Tooltip.tsx', () => ({ Tooltip: 'Tooltip' }));
    mock.module(ui + 'components/ui/Segmented.tsx', () => ({ Segmented: 'Segmented' }));
    mock.module(ui + 'components/ui/ConfirmModal.tsx', () => ({ ConfirmModal: 'ConfirmModal' }));
    const { ApiError } = await import(ui + 'lib/api/client.ts');
    let networkFailed = true;
    mock.module(ui + 'lib/api/system.ts', () => ({ validateAlgorithmScope: async (_, code) => {
      if (code === 'f') throw new ApiError(422, '未识别品种 f');
      if (code === 'rb' && networkFailed) throw new TypeError('Failed to fetch');
      return { code: 'SHFE.' + code };
    } }));
    const { AlgorithmScopePicker } = await import(ui + 'features/account/AlgorithmScopePicker.tsx');
    const added = [], products = []; let selected = ''; let restored = false;
    function all(node) { return Array.isArray(node) ? node.flatMap(all) : node?.props
      ? [node, ...all(node.props.children), ...all(node.props.content), ...all(node.props.trailing)] : [] }
    let nodes;
    function render() { at = 0; refAt = 0; nodes = all(AlgorithmScopePicker({ channel: 'ctp',
      capability: { pattern: '[a-z]+', example: 'ag', description: '期货品种代码' },
      products, value: selected, onAdd: code => { added.push(code); products.push(code); selected = code }, onChange() {}, onRestoreDefault() { restored = true },
    })); for (const effect of effects.splice(0)) effect(); }
    const input = () => nodes.find(n => n.type === 'input');
    const bubble = () => nodes.find(n => n.type === 'Tooltip');
    const add = () => nodes.find(n => n.type === 'button' && n.props['aria-label'] === '确认添加品种');
    const form = () => nodes.find(n => n.props.id === 'algorithm-product-form');
    const trigger = () => nodes.find(n => n.props['aria-controls'] === 'algorithm-product-form');
    const confirmation = () => nodes.find(n => n.type === 'ConfirmModal');
    const open = () => { nodes.find(n => n.props['aria-controls'] === 'algorithm-product-form').props.onClick(); render(); };
    const enter = value => { input().props.onChange({ target: { value } }); render(); };
    const settle = async () => { await Bun.sleep(350); render(); };
    render(); expect(form().props['aria-hidden']).toBe(true); open();
    expect(nodes.find(n => n.type === 'Segmented').props.trailing).toBeDefined();
    expect(form().props.className).toContain('absolute inset-0');
    expect(nodes.find(n => n.props['aria-label'] === '删除 ag 的品种配置')).toBeUndefined();
    selected = 'SHFE.ag'; render();
    const restore = nodes.find(n => n.props['aria-label'] === '删除 ag 的品种配置');
    expect(restore.props.className).toContain('ml-auto');
    expect(restore.props.className).toContain('hover:text-warn');
    expect(restore.props.className).toContain('active:bg-warn/20');
    restore.props.onClick(); render(); expect(restored).toBe(false);
    expect(confirmation().props.spec.title).toBe('删除 ag 的品种配置？');
    expect(confirmation().props.spec.danger).toBe(true);
    confirmation().props.onClose(); render();
    expect(confirmation().props.spec).toBe(null); expect(restored).toBe(false);
    restore.props.onClick(); render();
    const spec = confirmation().props.spec;
    confirmation().props.onClose(); spec.onConfirm(); render();
    expect(restored).toBe(true); expect(confirmation().props.spec).toBe(null);
    selected = ''; render();
    expect(form().props['aria-hidden']).toBe(false); enter('f');
    expect(bubble().props.open).toBe(false);
    expect(add().props['aria-busy']).toBe(true);
    expect(add().props.disabled).toBe(true);
    await settle();
    expect(bubble().props.open).toBe(true);
    expect(input().props['aria-invalid']).toBe(true);
    expect(nodes.find(n => n.props.id === 'algorithm-code-status').props.children).toBe('');
    expect(nodes.find(n => n.type === 'button' && n.props.children === '重试')).toBeUndefined();
    add().props.onClick(); expect(added).toEqual([]);
    enter('ag'); expect(bubble().props.open).toBe(false);
    input().props.onFocus(); render();
    expect(bubble().props.open).toBe(true);
    expect(nodes.find(n => n.props.id === 'algorithm-code-help').props.children).toBe('期货品种代码');
    input().props.onBlur(); render(); expect(bubble().props.open).toBe(false);
    await settle(); expect(add().props.disabled).toBe(false);
    add().props.onClick(); render();
    expect(added).toEqual(['SHFE.ag']); expect(input().props.value).toBe('');
    expect(form().props['aria-hidden']).toBe(true);
    expect(trigger().props.inert).toBe(true);
    expect(nodes[0].props.className).toContain('h-12');
    expect(animations.at(-1).frames).toEqual([{ width: '260px' }, { width: '34px' }]);
    expect(animations.at(-1).options.duration).toBe(200);
    animations.at(-1).onfinish(); render();
    expect(trigger().props.inert).toBe(false);
    expect(nodes.find(n => n.type === 'Segmented').props.value).toBe('SHFE.ag');
    open(); enter('rb'); await settle();
    expect(bubble().props.open).toBe(true);
    const retry = nodes.find(n => n.type === 'button' && n.props.children === '重试');
    expect(retry).toBeDefined(); networkFailed = false; retry.props.onClick(); render();
    await settle(); expect(bubble().props.open).toBe(false); expect(add().props.disabled).toBe(false);
    expect(added).toEqual(['SHFE.ag']);
    input().props.onKeyDown({ key: 'Escape', preventDefault() {}, stopPropagation() {} }); render();
    expect(form().props['aria-hidden']).toBe(true); expect(input().props.value).toBe('');
    expect(trigger().props.inert).toBe(false); expect(added).toEqual(['SHFE.ag']);
    reduceMotion = true; open(); enter('rb'); await settle(); add().props.onClick(); render();
    expect(added).toEqual(['SHFE.ag', 'SHFE.rb']); expect(trigger().props.inert).toBe(false);
    expect(animations).toHaveLength(1);
    expect(nodes.find(n => n.type === 'Segmented').props.value).toBe('SHFE.rb');
    for (const cleanup of cleanups) cleanup?.();
  `
  const result = spawnSync(process.execPath, ['-e', script], {
    cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000,
  })
  expect(result.status, result.stderr || String(result.error)).toBe(0)
})
