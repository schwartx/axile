import { expect, test } from '@playwright/test'

test('range survives save and reload; latest clears the selected bounds', async ({ page }) => {
  let plan: Record<string, unknown> = {}
  await page.route('**/api/v1/transfer/settings', async route => {
    if (route.request().method() === 'PUT') {
      plan = route.request().postDataJSON()
      return route.fulfill({ json: { saved: true } })
    }
    return route.fulfill({ json: { plan, last_run: null } })
  })
  await page.goto('/e2e/data-transfer.html')
  const range = page.getByRole('checkbox')
  await expect(range).toBeEnabled()
  await range.check()
  const dates = page.locator('input[type="datetime-local"]')
  await dates.nth(0).fill('2020-01-01T08:00')
  await dates.nth(1).fill('2020-02-01T08:00')
  await page.getByRole('button', { name: '保存规则' }).click()
  await expect(page.getByRole('status')).toHaveText('传送规则已保存')
  expect(plan.start).toBeTruthy(); expect(plan.end).toBeTruthy()
  await page.reload()
  await expect(range).toBeEnabled()
  await expect(range).toBeChecked()
  await expect(dates.nth(0)).toHaveValue('2020-01-01T08:00')
  await expect(dates.nth(1)).toHaveValue('2020-02-01T08:00')
  await range.uncheck()
  await page.getByRole('button', { name: '保存规则' }).click()
  await expect(page.getByRole('status')).toHaveText('传送规则已保存')
  expect(plan.start).toBeNull(); expect(plan.end).toBeNull()
  let request: Record<string, unknown> = {}
  await page.route('**/api/v1/transfer/pull', route => {
    request = route.request().postDataJSON()
    return route.fulfill({ json: { batch: 'pulled-batch' } })
  })
  await page.getByLabel('来源端地址').fill('https://source.test')
  await page.getByRole('button', { name: '立即拉取' }).click()
  await expect(page.getByRole('status')).toHaveText('拉取完成 · pulled-batch')
  expect(request.source_url).toBe('https://source.test')
})
