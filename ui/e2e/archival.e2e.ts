import { expect, test, type Page } from '@playwright/test'
import { DEFAULT_FUTURES_CRON } from '../src/features/setup/cron'

async function mockArchive(page: Page, account: Record<string, unknown> = {}) {
  const writes: string[] = []
  let archived = false
  let reject = true
  const portfolio = () => ({ id: 1, name: '商品趋势', market: '期货', description: '保留历史的组合',
    custom_calc_py_code: 'def calculate_portfolio(context):\n    return {"rb2610": 1.0}\n',
    status: null, tag: null, updated_at: '2026-11-09', created_at: '2026-11-09', archived_at: archived ? '2026-11-09T01:00:00Z' : null })
  const sync = { status: 'synchronized', revision: 1, attempts: 1, last_error: null, last_attempt_at: null, synchronized_at: null }
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() !== 'GET') writes.push(`${request.method()} ${url.pathname}`)
    if (/\/portfolio\/1\/(archive|restore)$/.test(url.pathname)) {
      if (reject) {
        reject = false
        await route.fulfill({ status: 409, json: { detail: '组合仍被未归档账户绑定，请先改绑或解绑' } })
        return
      }
      archived = url.pathname.endsWith('/archive')
      await route.fulfill({ json: portfolio() })
    } else if (url.pathname.endsWith('/portfolio/')) {
      await route.fulfill({ json: { data: archived === (url.searchParams.get('archived') === 'true') ? [portfolio()] : [] } })
    } else if (url.pathname.endsWith('/portfolio/1')) {
      await route.fulfill({ json: portfolio() })
    } else if (url.pathname.endsWith('/target_snapshot')) {
      await route.fulfill({ json: { weights: { rb2610: 1.0 }, calculated_at: '2026-11-09T01:00:00Z', source: 'manual', execution_id: null, context_account_id: null } })
    } else if (url.pathname.endsWith('/account/1')) {
      await route.fulfill({ json: { id: 1, name: '历史账户', archived_at: '2026-11-09T01:00:00Z', is_started: false,
        market: '期货', trade_channel: 'ctp', brokerage: 'ctp', portfolio_id: null, cron_expr: '0 9 * * *', remark: '历史配置', ...account } })
    } else if (url.pathname.endsWith('/runtime-sync')) {
      await route.fulfill({ json: sync })
    } else if (url.pathname.endsWith('/account/dashboard')) {
      await route.fulfill({ json: { data: [] } })
    } else {
      await route.fulfill({ json: {} })
    }
  })
  return writes
}

test('组合归档失败可重试，切换归档列表可查看只读详情并恢复', async ({ page }, info) => {
  const writes = await mockArchive(page)
  await page.goto('/e2e/archival.html')
  await page.getByRole('button', { name: '归档组合', exact: true }).click()
  await page.getByRole('button', { name: '归档', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('组合仍被未归档账户绑定，请先改绑或解绑')
  await page.getByRole('button', { name: '归档组合', exact: true }).click()
  await page.getByRole('button', { name: '归档', exact: true }).click()
  await expect(page.getByText('还没有组合', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '已归档', exact: true }).click()
  await page.getByRole('link', { name: '商品趋势', exact: true }).click()
  await expect(page.getByRole('heading', { name: '保存的组合代码' })).toBeVisible()
  await expect(page.getByRole('button', { name: '试跑', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '保存', exact: true })).toHaveCount(0)
  await page.screenshot({ path: info.outputPath('archived-portfolio.png'), fullPage: true })
  await page.getByRole('button', { name: '恢复组合', exact: true }).click()
  await page.getByRole('button', { name: '恢复', exact: true }).click()
  await expect(page.getByRole('heading', { name: '保存的组合代码' })).toHaveCount(0)
  expect(writes).toEqual(['POST /api/v1/portfolio/1/archive', 'POST /api/v1/portfolio/1/archive', 'POST /api/v1/portfolio/1/restore'])
})

for (const suffix of ['?account', '?account&edit']) test(`归档账户旧链接${suffix}只读且不发送运行请求`, async ({ page }, info) => {
  const writes = await mockArchive(page)
  await page.goto(`/e2e/archival.html${suffix}`)
  await expect(page.getByRole('heading', { name: '历史账户', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '执行记录与回放' })).toHaveAttribute('href', '/accounts/1/executions')
  await expect(page.getByRole('link', { name: '历史绩效' })).toHaveAttribute('href', '/accounts/1/history')
  await expect(page.getByRole('button', { name: '立即执行' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '恢复账户', exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('archived-account.png'), fullPage: true })
  expect(writes).toEqual([])
})

for (const { theme, width } of [{ theme: 'dark', width: 1280 }, { theme: 'dark', width: 390 }, { theme: 'light', width: 390 }]) {
  test(`归档摘要在${theme}主题${width}px下收起长规则且不溢出`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 800 })
    const writes = await mockArchive(page, { name: 'schwartx', portfolio_id: 1, cron_expr: DEFAULT_FUTURES_CRON, remark: null })
    await page.route('**/api/v1/portfolio/1', route => route.fulfill({ json: { id: 1, name: 'PFO_CY292_C7_N17_10', archived_at: null } }))
    await page.goto('/e2e/archival.html?account')
    await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), theme === 'dark')
    await expect(page.getByRole('heading', { name: 'schwartx', exact: true })).toBeVisible()
    const portfolio = page.getByRole('link', { name: 'PFO_CY292_C7_N17_10', exact: true })
    await expect(portfolio).toBeVisible()
    await expect(page.getByText('自定义执行节奏', { exact: true })).toBeVisible()
    const rules = page.locator('details pre')
    await expect(rules).toBeHidden()
    await expect(page.getByText('备注', { exact: true })).toHaveCount(0)
    const labelBox = (await page.locator('dt').filter({ hasText: '绑定组合' }).boundingBox())!
    const valueBox = (await portfolio.boundingBox())!
    expect(valueBox.x - labelBox.x).toBeLessThan(160)
    await page.screenshot({ path: info.outputPath(`archived-account-${theme}-${width}.png`), fullPage: true })
    const disclosure = page.getByText('查看原始规则', { exact: true })
    await disclosure.click()
    await expect(rules).toHaveText(DEFAULT_FUTURES_CRON.replace(/\s*\|\s*/g, '\n'))
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await disclosure.press('Enter')
    await expect(rules).toBeHidden()
    expect(writes).toEqual([])
  })
}
