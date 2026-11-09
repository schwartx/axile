import { expect, test, type Page } from '@playwright/test'
import type { SchedulePreview } from '../src/lib/api/accounts'

type PreviewRequest = {
  cron_expr?: string
  after: string | null
  limit: number
  scheduled_ats?: string[]
  supplement: { count: number; interval_minutes: number } | null
}

async function mockPreview(page: Page) {
  const requests: PreviewRequest[] = []
  let failSummary = false
  let delayInterval: number | null = null
  let release: () => void = () => {}
  const times = Array.from({ length: 120 }, (_, index) => new Date(Date.UTC(2026, 9, 12 + index, 6, 55)).toISOString())
  await page.route('**/account/schedule-preview', async (route) => {
    const payload = route.request().postDataJSON() as PreviewRequest
    requests.push(payload)
    if (payload.scheduled_ats && failSummary) {
      failSummary = false
      await route.fulfill({ status: 503, json: { detail: '模拟补发预览失败' } })
      return
    }
    if (payload.scheduled_ats && payload.supplement?.interval_minutes === delayInterval) {
      await new Promise<void>((resolve) => { release = resolve })
    }
    const remaining = payload.scheduled_ats ?? times.filter((time) => !payload.after || Date.parse(time) > Date.parse(payload.after))
    const selected = payload.scheduled_ats ? remaining : remaining.slice(0, payload.limit)
    const items: SchedulePreview['items'] = selected.map((time) => {
      const closed = [0, 6].includes(new Date(time).getUTCDay())
      const count = closed || !payload.supplement ? 0 : Math.min(payload.supplement.count, Math.ceil(5 / payload.supplement.interval_minutes) - 1)
      return {
        scheduled_at: time, base_scheduled_at: payload.scheduled_ats ? time : null,
        index: 0, effective_count: count, is_last: count === 0,
        calendar_day: time.slice(0, 10), calendar_status: closed ? 'available_closed' : 'available_open',
        action: closed ? 'skip' : 'execute', reason_code: closed ? 'CALENDAR.CLOSED' : null,
        unavailable_reason: null, calendar_id: 'china', label: '中国交易日历',
      }
    })
    await route.fulfill({ json: {
      timezone: 'Asia/Shanghai', evaluated_at: '2026-10-09T15:00:00+08:00',
      calendar: { requirement: 'required', availability: 'available', unavailable_reason: null, calendar_id: 'china', label: '中国交易日历', coverage_start: '2003-01-01', coverage_end: '2026-12-31' },
      items, next_cursor: payload.scheduled_ats ? null : items.at(-1)?.scheduled_at,
      has_more: !payload.scheduled_ats && remaining.length > selected.length,
    } })
  })
  return {
    requests,
    fail: () => { failSummary = true },
    delay: (interval: number) => { delayInterval = interval },
    release: () => release(),
  }
}

test('正常轮次留白，补发裁剪才提示，编辑保留时间节点和滚动位置', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 })
  const mock = await mockPreview(page)
  await page.goto('/e2e/timer.html')
  const list = page.getByRole('list', { name: '未来排程预览' })
  await expect.poll(() => mock.requests.some((request) => Boolean(request.scheduled_ats))).toBe(true)
  await expect(list.locator('time').first()).toBeVisible()
  await expect(list.locator('[aria-label^="仅补"], [aria-label="无补发"]')).toHaveCount(0)
  await expect(list.getByText('休市', { exact: true }).first()).toBeVisible()
  const scroll = page.getByRole('complementary').locator('.quiet-scrollbar')
  const initialRows = await list.locator('time').count()
  await scroll.evaluate((node) => { node.scrollTop = node.scrollHeight })
  await expect.poll(() => list.locator('time').count()).toBeGreaterThan(initialRows)
  await scroll.evaluate((node) => { node.scrollTop = 100 })
  const rowHeights = await list.getByRole('listitem').evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height))
  const dates = await list.locator('time').allTextContents()
  const timeNode = await list.locator('time').first().elementHandle()
  const listNode = await list.elementHandle()
  const baseRequests = mock.requests.filter((request) => !request.scheduled_ats).length
  await page.getByRole('button', { name: '下一档补发次数', exact: true }).click()
  await expect.poll(() => mock.requests.some((request) => request.supplement?.count === 3)).toBe(true)
  await page.getByRole('button', { name: '下一档补发间隔分钟', exact: true }).click()
  await expect(list.locator('[aria-label="仅补 2 次"]').first()).toBeAttached()
  expect(mock.requests.filter((request) => !request.scheduled_ats)).toHaveLength(baseRequests)
  expect(await list.locator('time').allTextContents()).toEqual(dates)
  expect(await timeNode!.evaluate((node) => node.isConnected)).toBe(true)
  expect(await listNode!.evaluate((node) => node.isConnected)).toBe(true)
  expect(await list.getByRole('listitem').evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height))).toEqual(rowHeights)
  expect(await scroll.evaluate((node) => node.scrollTop)).toBe(100)
  await expect(page.getByLabel('正在加载排程预览')).toHaveCount(0)
  await page.getByRole('spinbutton', { name: '补发间隔分钟', exact: true }).focus()
  await page.keyboard.press('End')
  await expect(list.locator('[aria-label="无补发"]').first()).toBeAttached()
  await page.getByRole('complementary').screenshot({ path: 'test-results/timer-preview-clipped.png', animations: 'disabled' })
  await page.getByRole('spinbutton', { name: '补发次数', exact: true }).focus()
  await page.keyboard.press('Home')
  await expect(list.locator('[aria-label^="仅补"], [aria-label="无补发"]')).toHaveCount(0)
  expect(await scroll.evaluate((node) => node.scrollTop)).toBe(100)
  await page.getByRole('complementary').screenshot({ path: 'test-results/timer-preview-normal.png', animations: 'disabled' })
})

test('摘要失败可重试，快速换间隔时旧响应不能覆盖新值', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const mock = await mockPreview(page)
  await page.goto('/e2e/timer.html')
  const list = page.getByRole('list', { name: '未来排程预览' })
  await expect.poll(() => mock.requests.some((request) => Boolean(request.scheduled_ats))).toBe(true)
  await expect(list.locator('time').first()).toBeVisible()
  const dates = await list.locator('time').allTextContents()
  const baseRequests = mock.requests.filter((request) => !request.scheduled_ats).length
  mock.fail()
  await page.getByRole('button', { name: '下一档补发间隔分钟', exact: true }).click()
  await expect(page.getByText('补发预览未更新')).toBeVisible()
  expect(await list.locator('time').allTextContents()).toEqual(dates)
  await page.getByRole('button', { name: '重试：补发预览未更新', exact: true }).click()
  await expect(page.getByRole('button', { name: '重试：补发预览未更新', exact: true })).toHaveCount(0)
  mock.delay(3)
  await page.getByRole('button', { name: '下一档补发间隔分钟', exact: true }).click()
  await expect.poll(() => mock.requests.some((request) => request.supplement?.interval_minutes === 3)).toBe(true)
  await page.getByRole('button', { name: '下一档补发间隔分钟', exact: true }).click()
  await expect(list.locator('[aria-label="仅补 1 次"]').first()).toBeAttached()
  mock.release()
  expect(await list.locator('time').allTextContents()).toEqual(dates)
  expect(mock.requests.filter((request) => !request.scheduled_ats)).toHaveLength(baseRequests)
  await expect(list.locator('[aria-label="仅补 1 次"]').first()).toBeAttached()
})


test('股票阶段开关传入预览，盘后保持禁用', async ({ page }) => {
  const mock = await mockPreview(page)
  await page.goto('/e2e/timer.html?channel=gm')
  const opening = page.getByRole('switch', { name: '早盘集合竞价 · 可撤单阶段', exact: true })
  await expect(opening).toHaveAttribute('aria-checked', 'false')
  await opening.click()
  await expect(opening).toHaveAttribute('aria-checked', 'true')
  await expect.poll(() => mock.requests.some(request => (request as PreviewRequest & { gm_sessions?: { opening_cancelable?: { enabled: boolean } } }).gm_sessions?.opening_cancelable?.enabled === true)).toBe(true)
  await expect(page.getByRole('switch', { name: '盘后定价交易', exact: true })).toBeDisabled()
})

test('GM 触发时间在编辑页和快捷弹窗复用时间滚轮，仅允许 09:20–09:24', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/v1/**', route => route.abort())
  await mockPreview(page)
  for (const modal of [false, true]) {
    await page.setViewportSize({ width: modal ? 520 : 1440, height: 900 })
    await page.goto(`/e2e/${modal ? 'control' : 'timer'}.html?channel=gm`)
    if (modal) {
      await page.getByRole('button', { name: '调整自动执行' }).click()
      await page.getByRole('switch', { name: '自动调仓', exact: true }).click()
    }
    const trigger = page.getByRole('group', { name: '早盘不可撤单阶段触发时间' })
    await expect(trigger.getByRole('button', { name: '时', exact: true })).toBeDisabled()
    const minute = trigger.getByRole('button', { name: '分', exact: true })
    await expect(minute).toHaveText('20')
    expect((await trigger.boundingBox())!.height).toBeLessThanOrEqual(34)
    await expect(page.locator('select')).toHaveCount(0)
    await minute.click()
    const menu = page.getByRole('dialog', { name: '选择时刻', exact: true })
    const hours = menu.getByRole('listbox', { name: '时', exact: true })
    const minutes = menu.getByRole('listbox', { name: '分', exact: true })
    await expect(hours.getByRole('option')).toHaveText(['09'])
    await expect(minutes.getByRole('option')).toHaveText(['20', '21', '22', '23', '24'])
    await expect(menu).toBeVisible()
    expect((await menu.boundingBox())!.height).toBeLessThan(145)
    if (modal) await menu.screenshot({ path: testInfo.outputPath('gm-time-wheel.png') })
    await minutes.getByRole('option', { name: '24', exact: true }).click()
    await expect(minute).toHaveText('24')
    await minutes.press('ArrowDown')
    await expect(minute).toHaveText('24')
    await minutes.press('ArrowUp')
    await expect(minute).toHaveText('23')
    await minutes.hover()
    await page.mouse.wheel(0, 36)
    await expect(minute).toHaveText('24')
    const dragMinutes = async (distance: number) => {
      const box = (await minutes.boundingBox())!
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      await page.mouse.down()
      await page.mouse.move(box.x + box.width + 20, box.y + box.height / 2 + distance, { steps: 8 })
      await page.mouse.up()
    }
    await dragMinutes(64)
    await expect(minute).toHaveText('22')
    await expect.poll(() => minutes.evaluate(node => node.scrollTop)).toBe(64)
    await dragMinutes(200)
    await expect(minute).toHaveText('20')
    await dragMinutes(-200)
    await expect(minute).toHaveText('24')
    await minutes.press('Escape')
    await expect(menu).toHaveCount(0)
    await expect(trigger).toBeFocused()
    await trigger.press('ArrowUp')
    await expect(minute).toHaveText('20')
    await trigger.press('Enter')
    await trigger.press('Escape')
    await expect(menu).toHaveCount(0)
    await expect(trigger).toBeFocused()
    if (modal) await expect(page.getByRole('dialog', { name: '调整定时任务', exact: true })).toBeVisible()
  }
})

test('高级编辑的时间滚轮保留完整的小时和分钟范围', async ({ page }) => {
  await mockPreview(page)
  await page.goto('/e2e/timer.html?channel=gm')
  const trigger = page.getByRole('group', { name: '时刻', exact: true })
  const hour = trigger.getByRole('button', { name: '时', exact: true })
  await expect(hour).toBeEnabled()
  await hour.click()
  const menu = page.getByRole('dialog', { name: '选择时刻', exact: true })
  const hours = menu.getByRole('listbox', { name: '时', exact: true })
  const minutes = menu.getByRole('listbox', { name: '分', exact: true })
  await expect(hours.getByRole('option')).toHaveCount(24)
  await expect(minutes.getByRole('option')).toHaveCount(60)
  await hours.getByRole('option', { name: '23', exact: true }).click()
  await minutes.getByRole('option', { name: '59', exact: true }).click()
  await expect(hour).toHaveText('23')
  await expect(trigger.getByRole('button', { name: '分', exact: true })).toHaveText('59')
  const box = (await hours.boundingBox())!
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 + 72, { steps: 8 })
  await page.mouse.up()
  await expect(hour).toHaveText('21')
  await hours.getByRole('option', { name: '22', exact: true }).click()
  await expect(hour).toHaveText('22')
  await minutes.press('Escape')
  await expect(menu).toHaveCount(0)
})

test('A股 30 分快捷节奏在编辑页和弹窗可选，预览与保存保持半小时', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/v1/**', route => route.abort())
  const mock = await mockPreview(page)
  const expected = '0 10,11,13,14 * * * | 29 11 * * * | 30 9,10,13,14 * * *'
  let saved: { cron_expr?: string } | null = null
  await page.route('**/api/v1/account/1', async route => {
    saved = route.request().postDataJSON()
    await route.fulfill({ json: { id: 1 } })
  })
  for (const modal of [false, true]) {
    mock.requests.length = 0
    await page.setViewportSize({ width: modal ? 520 : 1440, height: 900 })
    await page.goto(`/e2e/${modal ? 'control' : 'timer'}.html?channel=gm`)
    if (modal) {
      await page.getByRole('button', { name: '调整自动执行' }).click()
      await page.getByRole('switch', { name: '自动调仓', exact: true }).click()
    } else {
      await page.getByText('快捷', { exact: true }).click()
    }
    const preset = page.getByRole('button', { name: '盘中每 30 分', exact: true })
    await preset.click()
    await expect(preset).toHaveAttribute('aria-pressed', 'true')
    await expect.poll(() => mock.requests.some(request => request.cron_expr === expected)).toBe(true)
    if (modal) {
      await page.getByRole('button', { name: '保存', exact: true }).click()
      await expect.poll(() => saved?.cron_expr).toBe(expected)
    }
  }
})
