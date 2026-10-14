import { expect, test, type Page } from '@playwright/test'
import type { SchedulePreview } from '../src/lib/api/accounts'

type PreviewRequest = {
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
