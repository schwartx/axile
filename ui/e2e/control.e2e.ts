import { expect, test } from '@playwright/test'
import type { AccountControlOverride } from '../src/types/api'
function snapshot() {
  const metric = (operation: string, value: number, limit: number, label: string, rule = 'per_day') => ({ id: `account:${operation}:${rule}`,  operation, scope: 'account', symbol: null, rule, value, limit, remaining: limit - value, estimated_wait_ms: 0, on_trigger: 'wait', label })
  const metrics = [metric('place_order', 123, 500, '今日报单'), metric('cancel_order', 80, 400, '今日撤单'), metric('query_order', 12, 5000, 'query_order')]
  return { stream_id: 'e2e', version: 1, kind: 'snapshot', data: [{ account_id: 1, execution_id: 'e', instance_id: 'g', seq: 1, sampled_at_ms: Date.now(), status: 'running', stale: false, incomplete: false, pending_policy: true, timezone: 'Asia/Shanghai', control_date: '2026-10-13', actual_policy: {}, saved_policy: {}, rules: [...metrics, metric('place_order', 3, 20, '本分钟报单', 'per_minute'), metric('cancel_order', 2, 30, '本分钟撤单', 'per_minute'), metric('query_order', 1, 120, '本分钟查单', 'per_minute')], metrics, waiting_total: 0, waiting: [], records: [] }] }
}
test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => { throw error })
  await page.route('**/api/v1/events/stream', (route) => route.fulfill({ status: 200, contentType: 'text/event-stream', body: `event: control.snapshot\ndata: ${JSON.stringify(snapshot())}\n\n` }))
  await page.route('**/api/v1/account/1/control/policy', (route) => route.fulfill({ json: {
    operations: ['place_order', 'cancel_order', 'query_order', ...Array.from({ length: 20 }, (_, i) => `extension_${i}`), 'global_only'].map((key) => ({ key, display_name: key.startsWith('extension_') ? `扩展操作${key.slice(10)}` : ({ place_order: '报单', cancel_order: '撤单', query_order: '查单' }[key] ?? key), category: '查询' })),
    effective_policy: { operations: Object.fromEntries(['place_order', 'cancel_order', 'query_order', ...Array.from({ length: 20 }, (_, i) => `extension_${i}`)].map((key) => [key, { account: {} }])) },
  } }))
  await page.goto('/e2e/control.html')
})
test('two-step add preserves search and saves extensions with unknown usage', async ({ page }) => {
  const card = page.getByRole('button', { name: /^流控/ })
  await card.focus(); await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '账户流控' })
  await expect(dialog.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  const sections = await dialog.locator('section').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('aria-label')))
  expect(sections.slice(0, 2)).toEqual(['当前等待', '摘要显示设置'])
  await dialog.getByRole('button', { name: '＋ 添加指标' }).click()
  const search = dialog.getByRole('textbox', { name: '搜索操作' })
  await expect(search).toBeFocused()
  await search.fill('扩展操作9')
  await dialog.getByRole('button', { name: '添加扩展操作9', exact: true }).click()
  await dialog.getByRole('button', { name: '返回操作列表' }).click()
  await expect(search).toHaveValue('扩展操作9')
  await dialog.getByRole('button', { name: '添加扩展操作9', exact: true }).click()
  await dialog.getByRole('button', { name: '添加扩展操作9今日', exact: true }).click()
  await expect(dialog.getByText('最多显示三项，移除一项后可添加')).toBeVisible()
  await expect(dialog.getByRole('group', { name: '扩展操作9时间窗口' }).getByRole('button', { name: '今日', exact: true })).toBeFocused()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(card).toContainText('扩展操作9 未知')
  await expect(card).toBeFocused()
  await page.reload()
  await expect(page.getByRole('button', { name: /^流控/ })).toContainText('extension_9 未知')
})

test('cancel, empty draft, duplicate windows and keyboard editing', async ({ page }) => {
  const card = page.getByRole('button', { name: /^流控/ })
  await card.click()
  const dialog = page.getByRole('dialog', { name: '账户流控' })
  await dialog.getByRole('button', { name: '移除撤单今日' }).click()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(card).toContainText('撤单额度 80/400')
  await card.click()
  await dialog.getByRole('button', { name: '＋ 添加指标' }).click()
  await dialog.getByRole('textbox').fill('下单')
  await page.keyboard.press('Tab'); await page.keyboard.press('Enter')
  await expect(dialog.getByRole('button', { name: '添加报单今日', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: '添加报单本分钟', exact: true }).click()
  await expect(dialog.getByRole('group', { name: '报单时间窗口' }).getByRole('button', { name: /已添加/ }).first()).toBeDisabled()
  await dialog.getByRole('button', { name: '移除报单本分钟' }).click()
  await dialog.getByRole('button', { name: '移除报单今日' }).click()
  await dialog.getByRole('button', { name: '移除撤单今日' }).click()
  await expect(dialog.getByText(/请至少选择一项摘要/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  await page.keyboard.press('Escape')
  await card.click()
  await expect(dialog.getByRole('button', { name: '移除撤单今日' })).toBeVisible()
})

test('failed directory remains editable and retries; long lists scroll on narrow screens', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.route('**/api/v1/account/1/control/policy', (route) => route.fulfill({ status: 500, json: { detail: 'unavailable' } }))
  await page.getByRole('button', { name: /^流控/ }).click()
  const dialog = page.getByRole('dialog', { name: '账户流控' })
  await expect(dialog.getByText(/操作目录加载失败/)).toBeVisible()
  await dialog.getByRole('button', { name: '＋ 添加指标' }).click()
  await expect(dialog.getByRole('button', { name: '添加查单', exact: true })).toBeVisible()
  await page.unroute('**/api/v1/account/1/control/policy')
  await page.route('**/api/v1/account/1/control/policy', (route) => route.fulfill({ json: { operations: Array.from({ length: 25 }, (_, i) => ({ key: `extra_${i}`, display_name: `额外${i}`, category: '查询' })), effective_policy: { operations: Object.fromEntries(Array.from({ length: 25 }, (_, i) => [`extra_${i}`, {}])) } } }))
  await dialog.getByRole('button', { name: '重试：操作目录加载失败', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '添加额外0', exact: true })).toBeVisible()
  const list = dialog.getByRole('button', { name: '添加额外0', exact: true }).locator('../..')
  expect(await list.evaluate((node) => node.scrollHeight > node.clientHeight && node.clientHeight <= 240)).toBe(true)
  await expect(dialog.getByRole('heading', { name: '当前等待(0)' })).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('narrow screen and disconnect retain known values and policy notice', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await expect(page.getByText('分钟已用 3/20 · 今日已用 123/500 · 剩余 377', { exact: true })).toBeVisible()
  await expect(page.getByText(/已保存配置下次执行生效/)).toBeVisible()
  const trigger = page.getByRole('button', { name: /^流控/ })
  await expect(trigger.getByRole('img', { name: '统计待更新' })).toHaveCount(0)
  await page.getByRole('button', { name: '模拟断连' }).click()
  await expect(trigger).toContainText('123/500')
  await expect(trigger).not.toContainText('陈旧')
  await expect(trigger.getByRole('img', { name: '统计待更新' })).toBeVisible()
  await trigger.hover()
  await expect(page.getByRole('tooltip')).toContainText(/统计待更新 · 最后更新于 \d{2}:\d{2}:\d{2}/)
  await trigger.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('allowance usage and instrumented native submissions are shown independently', async ({ page }) => {
  const update = snapshot()
  Object.assign(update.data[0], { order_submissions: { scope: 'instrumented_ctp_calls', recorded: 3, sync_accepted: 1, sync_rejected: 1, unknown: 1, broker_acknowledged: 0, broker_rejected: 1 } })
  await page.route('**/api/v1/events/stream', route => route.fulfill({ status: 200, contentType: 'text/event-stream', body: `event: control.snapshot\ndata: ${JSON.stringify(update)}\n\n` }))
  await page.reload()
  await page.setViewportSize({ width: 375, height: 812 })
  await expect(page.getByRole('button', { name: /^流控/ })).toContainText('报单额度 123/500')
  await expect(page.getByText(/今日原生报单记录 3（已接入入口）/)).toContainText('同步拒绝 1 · 待确认 1')
  await page.getByRole('button', { name: /^流控/ }).click()
  const dialog = page.getByRole('dialog', { name: '账户流控' })
  await expect(dialog.getByText(/今日原生报单记录 3/)).toBeVisible()
  await expect(dialog.getByText(/今日原生报单记录 3/)).toContainText('柜台拒绝 1')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('timer and control share the modal heading, close icon and transition', async ({ page }) => {
  const timerTrigger = page.getByRole('button', { name: '调整自动执行' })
  await timerTrigger.click()
  const timer = page.getByRole('dialog', { name: '调整定时任务' })
  await expect(timer).toBeVisible()
  await expect(timer.getByRole('heading', { name: '定时任务' })).toHaveCSS('font-size', '18px')
  await expect(timer.getByRole('button', { name: '关闭', exact: true })).toHaveText('✕')
  await expect(timer).toHaveCSS('transition-duration', '0.15s')
  await expect(timer).toHaveCSS('border-radius', '18px')
  await timer.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(timer).not.toBeVisible()
  await expect(timerTrigger).toBeFocused()
  const controlTrigger = page.getByRole('button', { name: /^流控/ })
  await controlTrigger.click()
  const control = page.getByRole('dialog', { name: '账户流控' })
  await expect(control).toBeVisible()
  await expect(control.getByRole('heading', { name: '账户流控' })).toHaveCSS('font-size', '18px')
  await expect(control.getByRole('button', { name: '关闭', exact: true })).toHaveText('✕')
  await expect(control).toHaveCSS('transition-duration', '0.15s')
  await expect(control).toHaveCSS('border-radius', '18px')
  await page.keyboard.press('Escape')
  await expect(control).not.toBeVisible()
  await expect(controlTrigger).toBeFocused()
})

test('page shows empty waiting state and keeps recent records collapsed', async ({ page }) => {
  const waitingToggle = page.getByRole('button', { name: '等待请求(0)', exact: true })
  await expect(waitingToggle).toHaveAttribute('aria-expanded', 'false')
  await waitingToggle.click()
  await expect(page.getByRole('region', { name: '等待请求', exact: true })).toContainText('暂无等待请求')
  const toggle = page.getByRole('button', { name: /^最近记录/ })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('region', { name: '最近记录', exact: true })).toContainText('暂无记录')
})

test('page displays waiting reasons and asynchronous rejection without opening the modal', async ({ page }) => {
  const base = snapshot()
  const current = { ...base, data: [{ ...base.data[0], waiting_total: 1,
    waiting: [{ id: 1, operation: 'place_order', symbol: 'rb2610', priority: 100, kind: 'quota', entered_at_ms: Date.now() - 1200, waited_ms: 1200, estimated_wait_ms: 800, hits: [{ message: '账户分钟额度已用 20/20' }] }],
    records: [{ event_uid: 'event-rejected', occurred_at_ms: Date.now(), operation: 'place_order', symbol: 'rb2610', decision: 'allowed', outcome: 'broker_rejected', metadata: { error_id: 31, error_msg: '柜台返回：资金不足' } }],
  }] }
  await page.route('**/api/v1/events/stream', (route) => route.fulfill({ status: 200, contentType: 'text/event-stream', body: `event: control.snapshot\ndata: ${JSON.stringify(current)}\n\n` }))
  await page.reload()
  const waitingToggle = page.getByRole('button', { name: '等待请求(1)', exact: true })
  await expect(waitingToggle).toHaveAttribute('aria-expanded', 'false')
  await waitingToggle.click()
  const waiting = page.getByRole('region', { name: '等待请求', exact: true })
  await expect(waiting).toContainText('rb2610')
  await expect(waiting).toContainText('已等待 1.2 秒')
  await expect(waiting).toContainText('账户分钟额度已用 20/20')
  await page.getByRole('button', { name: /^最近记录/ }).click()
  const records = page.getByRole('region', { name: '最近记录', exact: true })
  await expect(records).toContainText('柜台拒绝')
  await expect(records).toContainText('柜台返回：资金不足')
  await page.getByRole('button', { name: /^流控/ }).click()
  const dialog = page.getByRole('dialog', { name: '账户流控' })
  await expect(dialog.getByRole('heading', { name: '当前等待(1)' })).toBeVisible()
  await expect(dialog.getByRole('region', { name: '等待请求', exact: true })).toContainText('rb2610')
  await expect(dialog.getByRole('region', { name: '等待请求', exact: true })).toContainText('账户分钟额度已用 20/20')
})

test('reopening the live stream does not flash a stale warning', async ({ page }) => {
  const current = snapshot()
  current.data[0].pending_policy = false
  await page.route('**/api/v1/events/stream', (route) => route.fulfill({ status: 200, contentType: 'text/event-stream', body: `event: control.snapshot\ndata: ${JSON.stringify(current)}\n\n` }))
  await page.reload()
  const notice = page.getByText(/^当前实例用量/)
  await expect(notice).toHaveClass(/text-ink-3/)
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect(notice).not.toContainText('数据陈旧')
  await expect(notice).toHaveClass(/text-ink-3/)
})

test('historical selections stay editable while directory loads and closing discards draft', async ({ page }) => {
  await page.evaluate(() => localStorage.setItem(`axile:control:${location.origin}:1`, JSON.stringify(['account:historical_custom:per_day'])))
  await page.reload()
  let finish: (() => void) | undefined
  await page.route('**/api/v1/account/1/control/policy', async (route) => {
    await new Promise<void>((resolve) => { finish = resolve })
    await route.fulfill({ json: { operations: [{ key: 'historical_custom', display_name: '历史扩展', category: '扩展' }], effective_policy: { operations: {} } } }).catch(() => {})
  })
  const card = page.getByRole('button', { name: /^流控/ })
  await card.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('正在读取操作目录…')).toBeVisible()
  await dialog.getByRole('group', { name: 'historical_custom时间窗口' }).getByRole('button', { name: '本分钟', exact: true }).click()
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  finish?.()
  await expect(card).toContainText('historical_custom 未知')
  await expect(card).not.toContainText('本分钟')
  await page.unroute('**/api/v1/account/1/control/policy')
  await card.click()
  await expect(dialog.getByRole('group', { name: 'historical_custom时间窗口' }).getByRole('button', { name: '今日', exact: true })).toHaveAttribute('aria-pressed', 'true')
})

test('return restores operation list scroll and collapsed content is excluded from focus', async ({ page }) => {
  await page.getByRole('button', { name: /^流控/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: '＋ 添加指标' }).click()
  const target = dialog.getByRole('button', { name: '添加扩展操作19', exact: true })
  await target.scrollIntoViewIfNeeded()
  const list = target.locator('../..')
  const top = await list.evaluate((node) => node.scrollTop)
  expect(top).toBeGreaterThan(0)
  await target.click()
  await dialog.getByRole('button', { name: '返回操作列表' }).click()
  expect(await list.evaluate((node) => node.scrollTop)).toBe(top)
  await dialog.getByRole('button', { name: '收起', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '＋ 添加指标' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('link', { name: '完整流控详情 →' })).toBeFocused()
})

test('window switching retains the slider and animates its movement', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.getByRole('button', { name: /^流控/ }).click()
  const group = page.getByRole('group', { name: '报单时间窗口' })
  const thumb = group.locator('span[aria-hidden]').first()
  const original = await thumb.elementHandle()
  await group.getByRole('button', { name: '本分钟', exact: true }).click()
  expect(await thumb.evaluate((node, previous) => node === previous, original)).toBe(true)
  await expect(group.getByRole('button', { name: '本分钟', exact: true })).toHaveAttribute('aria-pressed', 'true')
  expect(await thumb.evaluate((node) => getComputedStyle(node).transitionProperty)).toContain('transform')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await group.getByRole('button', { name: '今日', exact: true }).click()
  expect(await thumb.evaluate((node) => getComputedStyle(node).transitionProperty)).toBe('none')
})

async function warningEditor(page: import('@playwright/test').Page, initial: AccountControlOverride | null = null) {
  let saved = initial
  const writes: Array<{ account_control_override: AccountControlOverride | null }> = []
  const operations = {
    place_order: { priority: 100, account: { per_day: { limit: 500, on_trigger: 'block' } }, symbol: null },
    cancel_order: { priority: 0, account: { per_day: { limit: 400, on_trigger: 'block' } }, symbol: null },
    query_order: { priority: 100, account: { per_day: { limit: 5000, on_trigger: 'block' } }, symbol: null },
  }
  const policy = { timezone: 'Asia/Shanghai', operations, groups: {} }
  const account = () => ({ id: 1, name: '预警测试账户', market: '期货', trade_channel: 'ctp', account_control_preset: 'default', account_control_override: saved })
  await page.route('**/api/v1/account/1', async route => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON()
      writes.push(body)
      saved = body.account_control_override
    }
    await route.fulfill({ json: account() })
  })
  await page.route('**/api/v1/account/dashboard', route => route.fulfill({ json: { data: [], count: 0 } }))
  await page.route('**/api/v1/account/1/control/policy', route => route.fulfill({ json: {
    preset_key: 'default', preset_display_name: '默认', preset_description: '',
    timezone_display_name: '北京时间', override: saved,
    compatible_presets: [{ key: 'default', display_name: '默认', description: '' }],
    preset_policy: policy, effective_policy: policy,
    operations: Object.keys(operations).map(key => ({ key, display_name: ({ place_order: '报单', cancel_order: '撤单', query_order: '查单' })[key], category: '常用', description: '', groups: [] })), groups: [],
  } }))
  await page.goto('/e2e/control.html?warning-editor')
  await expect(page.getByRole('heading', { name: /执行流控/ })).toBeVisible()
  await page.getByRole('button', { name: /^报单 排队优先/ }).click()
  return writes
}

test('warning editor saves only the chosen account daily percentage and reloads it', async ({ page }) => {
  const writes = await warningEditor(page)
  await page.getByRole('button', { name: '调整报单额度预警' }).click()
  const group = page.getByRole('group', { name: '报单额度预警开关' })
  await group.getByRole('button', { name: '开启', exact: true }).click()
  const input = page.getByRole('textbox', { name: '报单预警比例' })
  await expect(input).toHaveValue('80')
  await expect(page.getByText('达到 400 次时发飞书，剩余 100 次。')).toBeVisible()
  await input.fill('90')
  await expect(page.getByText('达到 450 次时发飞书，剩余 50 次。')).toBeVisible()
  await page.screenshot({ path: 'test-results/quota-warning-editor.png', fullPage: true })
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => writes.length).toBe(1)
  expect(writes[0].account_control_override).toEqual({ operations: { place_order: { daily_warning_percent: 90 } }, groups: {} })
  await expect(page.getByRole('button', { name: '保存', exact: true, includeHidden: true })).toBeDisabled()
  await page.reload()
  await page.getByRole('button', { name: /^报单 排队优先/ }).click()
  await expect(page.getByRole('button', { name: '调整报单额度预警' })).toContainText('90% · 达到 450 次时提醒')
  await page.getByRole('button', { name: '调整报单额度预警' }).click()
  await page.getByRole('group', { name: '报单额度预警开关' }).getByRole('button', { name: '关闭', exact: true }).click()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => writes.length).toBe(2)
  expect(writes[1].account_control_override).toBeNull()
})

test('warning editor blocks invalid values, Esc restores the draft, and opening preserves the input', async ({ page }) => {
  await warningEditor(page)
  await page.getByRole('button', { name: '调整报单额度预警' }).click()
  const group = page.getByRole('group', { name: '报单额度预警开关' })
  await group.getByRole('button', { name: '开启', exact: true }).click()
  const input = page.getByRole('textbox', { name: '报单预警比例' })
  await input.fill('100')
  await expect(page.getByText('请输入 1–99 的整数')).toBeVisible()
  await expect(page.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  await input.fill('85')
  const original = await input.elementHandle()
  await group.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(input).toBeHidden()
  await group.getByRole('button', { name: '开启', exact: true }).click()
  await expect(input).toHaveValue('85')
  expect(await input.evaluate((node, previous) => node === previous, original)).toBe(true)
  await input.press('Escape')
  await expect(page.getByRole('button', { name: '调整报单额度预警' })).toBeFocused()
  await expect(page.getByRole('button', { name: '调整报单额度预警' })).toContainText('未启用')
  await expect(page.getByRole('button', { name: '保存', exact: true, includeHidden: true })).toBeDisabled()
})

test('unlimited daily quota suspends the warning and restoring a limit retains its ratio', async ({ page }) => {
  const writes = await warningEditor(page, { operations: { place_order: { daily_warning_percent: 90, account: { per_day: { unlimited: true } } } }, groups: {} })
  const warning = page.getByRole('button', { name: '调整报单额度预警' })
  await expect(warning).toBeDisabled()
  await expect(warning).toContainText('每日额度不限，预警暂不生效')
  await page.getByRole('button', { name: '调整每日额度', exact: true }).first().click()
  await page.getByRole('button', { name: '设置限额', exact: true }).click()
  const daily = page.getByRole('textbox', { name: '每日额度', exact: true })
  await daily.fill('1000')
  await daily.press('Enter')
  await expect(warning).toBeEnabled()
  await expect(warning).toContainText('90% · 达到 900 次时提醒')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => writes.length).toBe(1)
  expect(writes[0].account_control_override?.operations.place_order.daily_warning_percent).toBe(90)
  expect(writes[0].account_control_override?.operations.place_order.account?.per_day?.limit).toBe(1000)
})

test('page cancel also discards an invalid warning input', async ({ page }) => {
  const writes = await warningEditor(page)
  await page.getByRole('button', { name: '调整报单额度预警' }).click()
  await page.getByRole('group', { name: '报单额度预警开关' }).getByRole('button', { name: '开启', exact: true }).click()
  await page.getByRole('textbox', { name: '报单预警比例' }).fill('100')
  await expect(page.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByRole('button', { name: '调整报单额度预警' })).toContainText('未启用')
  await expect(page.getByText('请输入 1–99 的整数')).toBeHidden()
  expect(writes).toHaveLength(0)
})

test('原生报撤阈值独立保存并显示触发当笔数字', async ({ page }) => {
  const update = snapshot()
  Object.assign(update.data[0], { order_submissions: { scope: 'instrumented_ctp_calls', recorded: 2, sync_accepted: 1, sync_rejected: 1, unknown: 0, broker_acknowledged: 1, broker_rejected: 1, cancel_recorded: 1, cancel_sync_accepted: 1, cancel_sync_rejected: 0, cancel_unknown: 0 } })
  let saved = { settings: { order_limit: null as number | null, combined_limit: null as number | null, timezone: 'Asia/Shanghai', revision: 0, updated_at_ms: null as number | null }, control_date: '2026-10-19', scope: 'instrumented_ctp_calls', metrics: [{ metric: 'order_calls', value: 2, unknown: 0, limit: null as number | null, state: 'disabled' }, { metric: 'combined_calls', value: 3, unknown: 0, limit: null as number | null, state: 'disabled' }], warnings: [] as { event_uid: string; metric: string; value: number; limit: number; occurred_at_ms: number; control_date: string; timezone: string }[] }
  let writes = 0
  await page.route('**/api/v1/account/1/orders/monitor', route => {
    if (route.request().method() === 'PUT') {
      writes++
      const payload = route.request().postDataJSON()
      saved = { ...saved, settings: { ...saved.settings, ...payload, revision: writes, updated_at_ms: Date.now() }, metrics: saved.metrics.map(metric => { const limit = metric.metric === 'order_calls' ? payload.order_limit : payload.combined_limit; return { ...metric, limit, state: limit == null ? 'disabled' : 'reached' } }), warnings: [{ event_uid: String(writes), metric: 'combined_calls', value: 3, limit: payload.combined_limit, occurred_at_ms: Date.now(), control_date: '2026-10-19', timezone: 'Asia/Shanghai' }] }
    }
    return route.fulfill({ json: saved })
  })
  await page.route('**/api/v1/events/stream', route => route.fulfill({ status: 200, contentType: 'text/event-stream', body: `event: control.snapshot\ndata: ${JSON.stringify(update)}\n\n` }))
  await page.reload()
  await page.getByRole('button', { name: /^流控/ }).click()
  const section = page.getByRole('region', { name: '原生报撤监测' })
  await expect(section.getByLabel('报撤合计阈值')).toBeEnabled()
  await section.getByLabel('报撤合计阈值').fill('0')
  await section.getByRole('button', { name: '保存监测阈值' }).click()
  await expect(section.getByRole('alert')).toContainText('阈值须为')
  expect(writes).toBe(0)
  await section.getByLabel('报撤合计阈值').fill('3')
  await section.getByRole('button', { name: '保存监测阈值' }).click()
  await expect(section.getByText(/报撤合计预警：触发时 3\/3/)).toBeVisible()
  await expect(section.getByRole('button', { name: '保存监测阈值' })).toBeDisabled()
  await expect(page.getByRole('button', { name: /^流控/ })).toContainText('报单额度 123/500')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: /^流控/ }).click()
  await expect(page.getByLabel('报撤合计阈值')).toHaveValue('3')
  expect(writes).toBe(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
