import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'

test('组合卡片与向导保留多空配色并显示方向和单位，权重模式保留敞口', () => {
  const result = spawnSync(process.execPath, ['-e', `
    import { mock, expect } from 'bun:test';
    import { createElement } from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    import * as Router from 'react-router';
    const ui = ${JSON.stringify(new URL('../', import.meta.url).pathname)};
    mock.module('react-router', () => ({ ...Router, useViewTransitionState: () => false }));
    mock.module(ui + 'features/portfolio/CreatePortfolioModal.tsx', () => ({ CreatePortfolioModal: () => null }));
    const portfolio = { id: 1, name: 'PFO_TEST', market: '期货' };
    const snapshot = { target_mode: 'quantity', raw_targets: { FG701: 2, MA701: -1, SM701: -1 },
      weights: {}, calculated_at: '2026-11-06T14:11:00+08:00', source: 'manual', context_account_id: 7 };
    mock.module(ui + 'features/portfolio/usePortfoliosTargets.ts', () => ({
      usePortfoliosTargets: () => ({ 1: { snapshot, loading: false, error: null, refresh: async () => {} } }),
    }));
    mock.module(ui + 'lib/hooks/usePolling.ts', () => ({
      usePolling: () => ({ data: snapshot, loading: false, error: null, stale: false, refresh: async () => {} }),
    }));
    const { useDomainStore } = await import(ui + 'stores/domain.ts');
    const { useChannelCatalogStore } = await import(ui + 'stores/channels.ts');
    const account = { account_id: 7, name: 'test-account', portfolio_id: 1, market: '期货', trade_channel: 'ctp' };
    function seed(store, patch) { store.setState(patch); Object.assign(store.getInitialState(), store.getState()) }
    seed(useDomainStore, { portfolios: [portfolio], accounts: [account], portfoliosError: null, accountsError: null });
    seed(useChannelCatalogStore, { channels: [
      { channel: 'ctp', market: 'ctp', label: '天勤', portfolio: { market_label: '期货' }, units: { quantity_label: '手' } },
      { channel: 'gm', market: 'ashare', label: '掘金', portfolio: { market_label: 'A股' }, units: { quantity_label: '股' } },
    ] });
    const { PortfoliosPage } = await import(ui + 'pages/PortfoliosPage.tsx');
    const { PortfolioChoiceCard } = await import(ui + 'features/setup/PortfolioChoiceCard.tsx');
    const render = () => renderToStaticMarkup(createElement(Router.MemoryRouter, null, createElement(PortfoliosPage)));
    const choice = () => renderToStaticMarkup(createElement(PortfolioChoiceCard, {
      portfolio, selected: true, onSelect() {}, onPreview() {},
    }));
    const text = html => html.replace(/<[^>]*>/g, '');
    let html = render();
    expect(text(html)).toContain('目标持仓3 个品种');
    expect(html).toContain('多 2 手'); expect(html).toContain('空 1 手');
    expect(html).not.toContain('目标口径'); expect(html).not.toContain('总敞口');
    expect(html).toContain('text-up'); expect(html).toContain('text-down');
    expect(choice()).toContain('多 2 手');
    snapshot.context_account_id = null;
    expect(render()).toContain('多 2 手');
    portfolio.market = 'A股'; account.trade_channel = 'gm'; snapshot.context_account_id = 7;
    snapshot.raw_targets = { A: 200, B: -100 };
    expect(render()).toContain('多 200 股'); expect(choice()).toContain('空 100 股');
    snapshot.raw_targets = { XAUUSD: 0.015, SMALL: -0.0000000001 };
    portfolio.market = 'custom'; snapshot.context_account_id = null;
    html = render();
    expect(html).toContain('多 0.015'); expect(html).toContain('空 0.0000000001');
    expect(html).not.toContain(' 股'); expect(html).not.toContain(' 手');
    snapshot.raw_targets = {};
    expect(text(render())).toContain('目标持仓0 个品种');
    snapshot.target_mode = 'weight'; snapshot.weights = { A: 0.2, B: -0.1 };
    html = render();
    expect(html).toContain('总敞口'); expect(html).toContain('净敞口');
    expect(html).toContain('+20.0%'); expect(html).toContain('-10.0%');
    expect(html).not.toContain('目标口径');
  `], { cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  expect(result.status, result.stderr || String(result.error)).toBe(0)
})
