import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'

// Isolated browser; every API response is synthetic and no request reaches a trading service.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright')
const base = process.env.CURVE_PREVIEW_URL ?? 'http://127.0.0.1:1441'
const output = process.env.CURVE_SCREENSHOTS ?? '/tmp/axon-curve-transition'
mkdirSync(output, { recursive: true })
const points = Array.from({ length: 180 }, (_, i) => {
  const date = new Date(Date.UTC(2026, 0, 1) + i * 864e5).toISOString()
  const value = i * .001 + Math.sin(i / 9) * .008
  return { date: date.slice(0, 10), observed_at: date, account_return: i === 90 ? null : value, portfolio_return: value * 1.05, account_daily_return: .001, portfolio_daily_return: .00105, difference: 0 }
})
const settings = { backtest_weight_type: 'cs', backtest_fee_rate: 0 }
const result = { backtest_included: true, settings, engine_version: 'fixture', range: 'all', baseline: points[0].observed_at, end: points.at(-1).observed_at, record_count: 180, observation_count: 180, used_record_count: 180, invalid_asset_count: 0, gap: null, points, executions: [], bindings: [] }
const snapshot = { status: 'ready', snapshot_id: 'fixture-one', source_version: 1, snapshot_version: 1, logic_version: 'fixture', engine_version: 'fixture', computed_at: result.end, data_until: result.end, settings, error: null, retry_at: null, result, daily_costs: {}, events: [], event_count: 0 }
const summary = { snapshot_id: snapshot.snapshot_id, status: 'ready', computed_at: result.end, observed_at: result.end, account_equity: 118000, account_daily_return: .001, points }
const account = { id: 2, account_id: 2, name: '曲线过渡验证', remark: null, market: 'ctp', trade_channel: 'ctp', is_started: false, portfolio_id: null, is_scheduled: false, next_run_time: null, total_asset: 118000, currency: 'CNY', holdings_count: 0, position_weights: [], performance: summary, last_is_success: null, last_exec_at: null, last_output_status: null, ...settings, leverage: 1, symbols: [], cron: null }
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH })
const reports = [], errors = []

async function createPage({ width = 1440, reducedMotion = 'no-preference', colorScheme = 'light', delay = 0, fail = false, version = false, many = false, empty = false, single = false, scale = 1, large = false } = {}) {
  const fixturePoints = large ? Array.from({ length: 5000 }, (_, i) => ({ ...points[i % points.length], observed_at: new Date(Date.UTC(2026, 0, 1) + i * 3600000).toISOString() })) : points
  const fixtureResult = { ...result, points: fixturePoints, observation_count: fixturePoints.length, end: fixturePoints.at(-1).observed_at }
  const fixtureSnapshot = { ...snapshot, result: fixtureResult }
  const fixtureAccount = { ...account, performance: { ...summary, points: fixturePoints } }
  const context = await browser.newContext({ viewport: { width, height: 1000 }, deviceScaleFactor: scale, reducedMotion, colorScheme, recordVideo: { dir: `${output}/videos`, size: { width: 1440, height: 1000 } } })
  const page = await context.newPage()
  page.on('pageerror', error => { errors.push(error.message); console.error('Browser error:', error.message) })
  await page.route(`${base}/api/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    let json = { data: [], count: 0 }
    if (path.endsWith('/init/status')) json = { configured: true, environment: 'test', values: { environment: 'test', algorithm_modules: [], algorithm_directories: [] } }
    else if (path.endsWith('/capabilities/channels')) json = []
    else if (path.endsWith('/account/dashboard')) {
      const item = empty ? { ...account, performance: { ...summary, points: [] } } : fixtureAccount
      json = { data: many ? Array.from({ length: 12 }, (_, i) => ({ ...item, id: i + 1, account_id: i + 1, name: `曲线过渡验证 ${i + 1}` })) : [item] }
    } else if (/\/account\/\d+$/.test(path)) json = account
    else if (path.endsWith('/snapshot')) {
      if (delay) await new Promise(resolve => setTimeout(resolve, delay))
      if (fail) return route.fulfill({ status: 503, json: { detail: '测试网络失败' } }).catch(() => {})
      json = single ? { ...snapshot, result: { ...result, observation_count: 1, points: points.slice(0, 1) } }
        : version ? { ...snapshot, snapshot_id: 'fixture-two', result: { ...result, points: points.map(p => ({ ...p, account_return: p.account_return == null ? null : p.account_return * .85 })) } } : fixtureSnapshot
    } else if (path.endsWith('/rebalance_plan')) json = { rows: [], current_gross_weight: 0, target_gross_weight: 0 }
    else if (path.endsWith('/target_snapshot')) json = { status: 'empty', rows: [], weights: [], error: null }
    else if (path.endsWith('/next_run_time')) json = { next_execution_times: [] }
    else if (path.endsWith('/events/stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': test\n\n' })
    else if (path.endsWith('/refresh')) json = { status: 'pending' }
    else if (request.method() !== 'GET') throw new Error(`Unexpected write ${request.method()} ${path}`)
    await route.fulfill({ json }).catch(() => {})
  })
  await page.addInitScript(() => {
    window.curveLongTasks = []
    new PerformanceObserver(list => window.curveLongTasks.push(...list.getEntries().map(e => ({ start: e.startTime, duration: e.duration })))).observe({ type: 'longtask', buffered: true })
  })
  return { page, context }
}

async function sample(page, action) {
  return page.evaluate(async action => {
    const samples = [], started = performance.now()
    if (action === 'back') history.back()
    else if (action !== 'observe') document.querySelector(action).click()
    let seen = false, lastSvg = null
    while (performance.now() - started < 1600) {
      const svg = document.querySelector('.performance-curve-flight'), path = svg?.querySelector('path')
      if (path) {
        lastSvg = svg
        seen = true
        const box = path.getBBox()
        samples.push({ layers: document.querySelectorAll('.performance-curve-flight').length, t: performance.now() - started, moving: svg.dataset.phase === 'moving', direction: svg.dataset.direction, d: path.getAttribute('d'), box: { x: box.x, y: box.y, width: box.width, height: box.height }, width: Number(path.getAttribute('stroke-width')), opacity: Number(path.style.opacity) })
      } else if (seen || performance.now() - started > 800) break
      await new Promise(requestAnimationFrame)
    }
    return { samples, endReason: lastSvg?.dataset.endReason, remaining: document.querySelectorAll('.performance-curve-flight').length, tasks: window.curveLongTasks.filter(e => e.start > started), animationTasks: window.curveLongTasks.filter(e => e.start > started + (samples.find(s => s.moving)?.t ?? Infinity)) }
  }, action)
}
const link = 'a[aria-label="查看全部区间累计绩效"]'
const settled = page => page.waitForFunction(() => !document.querySelector('.performance-curve-flight') && document.getAnimations().every(a => a.playState !== 'running' || a.effect.getTiming().iterations === Infinity))
const ready = page => page.locator('[data-testid="performance-chart"]').waitFor()

try {
  for (const scenario of [{}, { saved: 'daily' }, { saved: 'zoom' }, { colorScheme: 'dark' }, { width: 390 }, { reducedMotion: 'reduce' }, { hidden: true }, { keyboard: true }]) {
    console.log('Checking sidebar expansion', scenario)
    const { page, context } = await createPage(scenario)
    if (scenario.saved) {
      await page.goto(`${base}/accounts/2/history`)
      await ready(page)
      await settled(page)
      if (scenario.saved === 'daily') {
        await page.getByRole('button', { name: '每日', exact: true }).click()
        await page.getByRole('button', { name: '30 天', exact: true }).click()
      } else {
        await page.getByTestId('performance-chart').focus()
        await page.keyboard.press('+')
      }
      await page.getByRole('link', { name: '账户概览', exact: true }).click()
    } else await page.goto(`${base}/accounts/2`)
    await page.locator(link).waitFor()
    await settled(page)
    const sourceBox = await page.locator(link).locator('svg path').evaluate(e => {
      const box = e.getBBox(), rect = e.ownerSVGElement.getBoundingClientRect()
      return { x: rect.left + box.x, y: rect.top + box.y, width: box.width, height: box.height }
    })
    if (scenario.hidden) await page.locator(link).evaluate(e => e.style.display = 'none')
    const nav = page.getByRole('link', { name: '实盘绩效', exact: true })
    if (!await nav.isVisible()) await page.getByRole('button', { name: /导航|菜单/ }).first().click()
    await page.evaluate(() => {
      window.nativeCurveTransitions = 0
      const original = document.startViewTransition?.bind(document)
      if (original) document.startViewTransition = (...args) => { window.nativeCurveTransitions++; return original(...args) }
    })
    const sampling = sample(page, 'observe')
    if (scenario.keyboard) { await nav.focus(); await page.keyboard.press('Enter') }
    else await nav.click()
    const forward = await sampling
    await ready(page)
    await settled(page)
    if (scenario.hidden || scenario.reducedMotion) assert.equal(forward.samples.length, 0)
    else {
      assert.equal(forward.endReason, 'finished')
      assert.ok(forward.samples.length >= 8)
      assert.ok(forward.samples.every(s => s.direction === 'open' && s.layers === 1))
      for (const key of ['x', 'y', 'width', 'height']) assert.ok(Math.abs(forward.samples[0].box[key] - sourceBox[key]) < 2, `Source ${key}`)
      assert.ok(forward.samples.at(-1).box.width > sourceBox.width * (scenario.width ? 1.1 : 3))
      const moving = forward.samples.filter(s => s.moving)
      assert.ok(moving.at(-1).t - moving[0].t >= 310 && moving.at(-1).t - moving[0].t <= 400)
      assert.equal(await page.evaluate(() => window.nativeCurveTransitions), 0)
    }
    const backNav = page.getByRole('link', { name: '账户概览', exact: true })
    if (!await backNav.isVisible()) await page.getByRole('button', { name: /导航|菜单/ }).first().click()
    const returning = sample(page, 'observe')
    await backNav.click()
    const backward = await returning
    // Successful reverse proves even a saved daily/range/zoom state became full cumulative.
    if (!scenario.reducedMotion) assert.equal(backward.endReason, 'finished')
    await settled(page)
    assert.equal(await page.locator('.performance-curve-flight').count(), 0)
    reports.push({ sidebarExpansion: scenario, forward, backward, video: await page.video().path() })
    await context.close()
  }

  // Actual sidebar clicks, sampled independently so browser input is not replaced by history.back().
  for (const scenario of [
    { source: '/accounts/2', destination: '/accounts/2' },
    { source: '/', destination: '/' },
    { source: '/accounts/2', destination: '/' },
    { source: '/', destination: '/accounts/2' },
    { direct: true, destination: '/' },
    { direct: true, destination: '/accounts/2' },
    { direct: true, destination: '/', version: true },
    { direct: true, destination: '/', many: true, accountId: 12 },
    { direct: true, destination: '/', colorScheme: 'dark' },
    { direct: true, destination: '/', width: 390 },
    { direct: true, destination: '/', reducedMotion: 'reduce' },
    { direct: true, destination: '/accounts/2', keyboard: true },
  ]) {
    console.log('Checking sidebar', scenario)
    const { page, context } = await createPage(scenario)
    const id = scenario.accountId ?? 2
    await page.goto(base + (scenario.direct ? `/accounts/${id}/history` : scenario.source))
    if (!scenario.direct) {
      await page.locator(link).click()
    }
    await ready(page)
    await settled(page)
    const nav = page.getByRole('link', { name: scenario.destination === '/' ? '所有账户' : '账户概览', exact: true })
    // Narrow layout exposes sidebar through its normal menu control.
    if (!await nav.isVisible()) {
      await page.getByRole('button', { name: /导航|菜单/ }).first().click()
    }
    await page.evaluate(() => {
      window.nativeCurveTransitions = 0
      const original = document.startViewTransition?.bind(document)
      if (original) document.startViewTransition = (...args) => { window.nativeCurveTransitions++; return original(...args) }
    })
    const sampling = sample(page, 'observe')
    if (scenario.keyboard) { await nav.focus(); await page.keyboard.press('Enter') }
    else await nav.click()
    const backward = await sampling
    await settled(page)
    const target = page.locator(`${link}[href="/accounts/${id}/history"]`)
    if (scenario.reducedMotion) assert.equal(backward.samples.length, 0)
    else {
      assert.ok(backward.samples.length >= 6, JSON.stringify({ scenario, backward }))
      assert.equal(backward.endReason, 'finished')
      assert.ok(backward.samples.at(-1).box.width < backward.samples[0].box.width * (scenario.width ? .85 : 1 / 3))
      assert.equal(await target.evaluate(e => document.activeElement === e), true)
      assert.equal(await page.evaluate(() => window.nativeCurveTransitions), 0)
      assert.ok(backward.samples.every(s => s.layers === 1), 'Commit must not recapture or duplicate the flight')
      const endpoint = await target.locator('svg path').evaluate(e => {
        const box = e.getBBox(), rect = e.ownerSVGElement.getBoundingClientRect()
        return { x: box.x + rect.left, y: box.y + rect.top, width: box.width, height: box.height }
      })
      for (const key of ['x', 'y', 'width', 'height']) assert.ok(Math.abs(backward.samples.at(-1).box[key] - endpoint[key]) < 5, `Endpoint ${key}`)
      const moving = backward.samples.filter(s => s.moving)
      assert.ok(moving.at(-1).t - moving[0].t >= 230 && moving.at(-1).t - moving[0].t <= 320)
    }
    assert.equal(backward.remaining, 0)
    assert.notEqual(await target.locator('svg').evaluate(e => getComputedStyle(e).opacity), '0')
    reports.push({ sidebar: scenario, ...backward, video: await page.video().path() })
    await context.close()
  }

  for (const scenario of ['empty', 'missing', 'daily', 'zoom', 'range', 'scroll', 'resize', 'motion', 'navigate']) {
    console.log('Checking reverse lifecycle', scenario)
    const { page, context } = await createPage({ empty: scenario === 'empty' })
    await page.goto(`${base}/accounts/${scenario === 'missing' ? 12 : 2}/history`)
    await ready(page)
    await settled(page)
    if (scenario === 'daily') await page.getByRole('button', { name: '每日', exact: true }).click()
    if (scenario === 'range') await page.getByRole('button', { name: '30 天', exact: true }).click()
    if (scenario === 'zoom') { await page.getByTestId('performance-chart').focus(); await page.keyboard.press('+') }
    await settled(page)
    const sampling = sample(page, 'observe')
    await page.getByRole('link', { name: '所有账户', exact: true }).click()
    if (['scroll', 'resize', 'motion', 'navigate'].includes(scenario)) {
      await page.waitForFunction(() => document.querySelector('.performance-curve-flight')?.dataset.phase === 'moving')
      if (scenario === 'scroll') await page.mouse.wheel(0, 100)
      if (scenario === 'resize') await page.setViewportSize({ width: 1100, height: 900 })
      if (scenario === 'motion') await page.emulateMedia({ reducedMotion: 'reduce' })
      if (scenario === 'navigate') await page.locator(link).click()
    }
    const transition = await sampling
    await settled(page)
    if (['daily', 'zoom', 'range'].includes(scenario)) assert.equal(transition.samples.length, 0)
    if (scenario === 'empty') assert.equal(transition.endReason, 'target-empty')
    if (scenario === 'missing') assert.equal(transition.endReason, 'target-timeout')
    assert.equal(await page.locator('.performance-curve-flight').count(), 0)
    assert.equal(await page.locator(`${link}, [data-testid=performance-chart]`).evaluateAll(elements => elements.some(e => !!e.closest('[inert]'))), false)
    assert.equal(await page.locator('main svg[style*="opacity: 0"]').count(), 0)
    reports.push({ reverseLifecycle: scenario, endReason: transition.endReason, frames: transition.samples.length })
    await context.close()
  }

  for (const source of ['/', '/accounts/2']) {
    console.log('Checking source', source)
    const { page, context } = await createPage()
    await page.goto(base + source)
    await page.locator(link).waitFor({ timeout: 10000 }).catch(async error => { await page.screenshot({ path: `${output}/failure.png` }); console.error(await page.locator('body').innerText()); throw error })
    await Promise.all([page.waitForResponse(r => r.url().includes('/snapshot')), page.locator(link).focus()])
    const forward = await sample(page, link)
    await ready(page)
    assert.ok(forward.samples.length >= 8, `Forward frames at ${source}: ${forward.samples.length}, ${forward.endReason}`)
    assert.ok(forward.samples.at(-1).box.width > forward.samples[0].box.width * 3)
    assert.ok(forward.samples.every(frame => frame.width >= 1.6 && frame.width <= 1.8 && !frame.d.includes('NaN')))
    assert.ok(forward.samples.every(frame => (frame.d.match(/M/g) ?? []).length === 2), 'Gap must survive every frame')
    assert.equal(forward.remaining, 0)
    assert.equal(await page.locator('[data-testid=performance-chart]').evaluate(e => !!e.closest('[inert]')), false)
    const historyBefore = await page.evaluate(() => history.length)
    const backward = await sample(page, 'back')
    assert.equal(await page.evaluate(() => history.length), historyBefore)
    assert.equal(new URL(page.url()).pathname, source)
    assert.ok(backward.samples.length >= 6, `Backward frames at ${source}: ${backward.samples.length}`)
    assert.equal(backward.samples[0].direction, 'close')
    assert.ok(backward.samples.at(-1).box.width < backward.samples[0].box.width / 3)
    assert.equal(await page.locator(link).evaluate(e => document.activeElement === e), true)
    assert.deepEqual(forward.animationTasks, [], 'No long tasks during geometric animation')
    reports.push({ source, forwardFrames: forward.samples.length, reverseFrames: backward.samples.length, navigationLongTasks: forward.tasks, animationLongTasks: forward.animationTasks })
    // Slowed only for reviewable stills; normal-speed checks above use production tokens.
    await page.evaluate(() => document.documentElement.style.setProperty('--curve-open-duration', '1600ms'))
    await page.locator(link).evaluate(e => e.click())
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${output}/${source === '/' ? 'fleet' : 'detail'}-expanding.png` })
    await settled(page)
    await page.screenshot({ path: `${output}/${source === '/' ? 'fleet' : 'detail'}-expanded.png` })
    await context.close()
  }

  for (const options of [{ delay: 1400 }, { delay: 180, version: true }, { delay: 160, fail: true }, { reducedMotion: 'reduce' }, { width: 390 }, { width: 390, colorScheme: 'dark' }, { width: 768, scale: 2 }, { empty: true }, { single: true }, { large: true }]) {
    console.log('Checking case', options)
    const { page, context } = await createPage(options)
    await page.goto(base)
    await page.locator(link).waitFor()
    const transition = await sample(page, link)
    if (options.reducedMotion || options.empty) assert.equal(transition.samples.length, 0)
    if (options.fail) {
      await page.getByRole('button', { name: '重试：绩效读取失败', exact: true }).waitFor()
      assert.ok(await page.getByTestId('performance-placeholder').count())
    } else if (options.single) await page.getByText('有效观测不足两条').waitFor()
    else await ready(page)
    await settled(page)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    assert.equal(await page.locator('.performance-curve-flight').count(), 0)
    if (options.large) assert.deepEqual(transition.animationTasks, [], '5000-point animation must not introduce a long task')
    await page.screenshot({ path: `${output}/case-${reports.length}.png` })
    reports.push({ options, frames: transition.samples.length })
    await context.close()
  }

  const { page, context } = await createPage({ many: true })
  await page.goto(base)
  const lastLink = `${link}[href="/accounts/12/history"]`
  await page.locator(lastLink).scrollIntoViewIfNeeded()
  const originalScroll = await page.locator('main').evaluate(e => e.scrollTop)
  await page.locator(lastLink).evaluate(e => e.click())
  await ready(page)
  await settled(page)
  const back = await sample(page, 'back')
  assert.equal(await page.locator('main').evaluate(e => e.scrollTop), originalScroll)
  assert.ok(back.samples.length > 5, 'Scrolled source returns with animation')
  // Navigation during the flight cancels it and removes all concealed state.
  await page.locator(lastLink).evaluate(e => { e.click(); setTimeout(() => history.back(), 70) })
  await page.waitForTimeout(650)
  assert.equal(await page.locator('.performance-curve-flight').count(), 0)
  assert.notEqual(await page.locator(lastLink).locator('svg').evaluate(e => getComputedStyle(e).opacity), '0')
  reports.push({ scrollRestored: originalScroll, interruptedNavigationClean: true })
  await context.close()
  {
    const { page, context } = await createPage()
    await page.goto(`${base}/accounts/2/history`)
    await ready(page)
    assert.equal(await page.locator('.performance-curve-flight').count(), 0, 'Direct URL has no source animation')
    await page.goto(base)
    await page.locator(link).waitFor()
    const modifiers = await page.locator(link).evaluate(e => ['ctrlKey', 'metaKey', 'shiftKey'].map(key => {
      const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, [key]: true })
      // Prevent the browser default only after React's handler, so the fixture creates no popups.
      let prevented
      const inspect = ev => { prevented = ev.defaultPrevented; ev.preventDefault() }
      document.getElementById('root').addEventListener('click', inspect, { once: true })
      e.dispatchEvent(event)
      return prevented
    }))
    assert.deepEqual(modifiers, [false, false, false])
    await page.locator(link).focus()
    await page.keyboard.press('Enter')
    const keyboard = await sample(page, 'observe')
    assert.ok(keyboard.samples.length > 0)
    await ready(page)
    await settled(page)
    const canvas = page.getByTestId('performance-chart')
    await canvas.focus()
    await page.keyboard.press('Home')
    await page.keyboard.press('ArrowRight')
    assert.ok(await canvas.evaluate(e => e.getContext('2d').getImageData(0, 0, e.width, e.height).data.some((value, i) => i % 4 === 3 && value > 0)))
    const original = await page.getByTestId('chart-viewport').innerText()
    await page.keyboard.press('+')
    assert.notEqual(await page.getByTestId('chart-viewport').innerText(), original)
    await page.keyboard.press('0')
    assert.equal(await page.getByTestId('chart-viewport').innerText(), original)
    reports.push({ directUrl: true, modifierLinks: true, keyboardEntry: true, chartInteractive: true })
    await context.close()
  }
  for (const interruption of ['scroll', 'resize', 'daily', 'range']) {
    const { page, context } = await createPage()
    await page.goto(base)
    await page.locator(link).waitFor()
    await Promise.all([page.waitForResponse(r => r.url().includes('/snapshot')), page.locator(link).focus()])
    await page.evaluate(selector => { document.querySelector(selector).click() }, link)
    await page.waitForFunction(() => document.querySelector('.performance-curve-flight')?.dataset.phase === 'moving')
    if (interruption === 'scroll') await page.locator('main').evaluate(e => e.scrollTop = 20)
    if (interruption === 'resize') await page.setViewportSize({ width: 1100, height: 900 })
    if (interruption === 'daily') await page.getByRole('button', { name: '每日', exact: true }).click()
    if (interruption === 'range') await page.getByRole('button', { name: '30 天', exact: true }).click()
    await settled(page)
    assert.equal(await page.locator('.performance-curve-flight').count(), 0)
    assert.equal(await page.locator('[data-testid=performance-chart]').evaluate(e => !!e.closest('[inert]') || getComputedStyle(e.parentElement).opacity === '0'), false)
    if (interruption === 'daily' || interruption === 'range') {
      const back = await sample(page, 'back')
      assert.equal(back.samples.length, 0, 'Changed graph must not shrink into an unrelated shape')
    }
    reports.push({ interruption, clean: true })
    await context.close()
  }
  assert.deepEqual(errors, [])
  writeFileSync(`${output}/report.json`, JSON.stringify(reports, null, 2))
  console.log(JSON.stringify({ status: 'passed', output, reports }, null, 2))
} finally { await browser.close() }
