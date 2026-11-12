import { expect, test, type Page } from '@playwright/test'
import { DEFAULT_FUTURES_CRON } from '../src/features/setup/cron'

async function mockCopy(page: Page, failure?: 'copy' | 'archive') {
  const writes: string[] = []
  let reject = failure
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() !== 'GET') writes.push(path)
    if (path.endsWith('/copy') || path.endsWith('/archive')) {
      if (reject && path.endsWith(`/${reject}`)) {
        reject = undefined
        await route.fulfill({ status: 409, json: { detail: '当前任务未结束，请稍后重试' } })
      } else {
        await route.fulfill({ json: { id: path.endsWith('/copy') ? 2 : 1, is_started: false,
          runtime_sync: { status: 'synchronized' } } })
      }
    } else {
      await route.fulfill({ json: { data: [] } })
    }
  })
  return writes
}

test('创建并归档原账户按顺序执行，窄屏按钮可见', async ({ page }, info) => {
  const writes = await mockCopy(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/e2e/archival.html?copy')
  const dialog = page.getByRole('dialog', { name: '复制账户' })
  const create = dialog.getByRole('button', { name: '创建并归档原账户', exact: true })
  await expect(create).toBeInViewport()
  await expect(dialog.getByRole('button', { name: '创建暂停账户', exact: true })).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('copy-and-archive-narrow.png'), fullPage: true })
  await create.click()
  await expect(page.getByRole('heading', { name: '已创建的新账户' })).toBeVisible()
  expect(writes).toEqual(['/api/v1/account/1/copy', '/api/v1/account/1/archive'])
})

test('归档失败后重试不重复创建账户', async ({ page }) => {
  const writes = await mockCopy(page, 'archive')
  await page.goto('/e2e/archival.html?copy')
  await page.getByRole('button', { name: '创建并归档原账户', exact: true }).click()
  await expect(page.getByText('新账户已创建，原账户归档尚未确认。重试只归档原账户，不会重复创建。')).toBeVisible()
  await expect(page.getByRole('button', { name: '创建暂停账户', exact: true })).toBeDisabled()
  await expect(page.getByLabel('新账户名称')).toBeDisabled()
  await page.getByRole('button', { name: '重试：归档原账户失败', exact: true }).click()
  await expect(page.getByRole('heading', { name: '已创建的新账户' })).toBeVisible()
  expect(writes).toEqual(['/api/v1/account/1/copy', '/api/v1/account/1/archive', '/api/v1/account/1/archive'])
})

test('复制失败不归档，重试保留归档选择', async ({ page }) => {
  const writes = await mockCopy(page, 'copy')
  await page.goto('/e2e/archival.html?copy')
  await page.getByRole('button', { name: '创建并归档原账户', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('复制账户失败')
  expect(writes).toEqual(['/api/v1/account/1/copy'])
  await page.getByRole('button', { name: '重试：复制账户失败', exact: true }).click()
  await expect(page.getByRole('heading', { name: '已创建的新账户' })).toBeVisible()
  expect(writes).toEqual(['/api/v1/account/1/copy', '/api/v1/account/1/copy', '/api/v1/account/1/archive'])
})

test('原有创建暂停账户不归档原账户', async ({ page }) => {
  const writes = await mockCopy(page)
  await page.goto('/e2e/archival.html?copy')
  await page.getByRole('button', { name: '创建暂停账户', exact: true }).click()
  await expect(page.getByRole('heading', { name: '已创建的新账户' })).toBeVisible()
  expect(writes).toEqual(['/api/v1/account/1/copy'])
})

for (const blocked of ['started', 'busy']) test(`来源账户${blocked}时禁用两种创建操作`, async ({ page }) => {
  const writes = await mockCopy(page)
  await page.goto(`/e2e/archival.html?copy&${blocked}`)
  await expect(page.getByRole('button', { name: '创建暂停账户', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '创建并归档原账户', exact: true })).toBeDisabled()
  expect(writes).toEqual([])
})

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

test('卡片内归档确认相对视口居中且遮罩覆盖全页', async ({ page }, info) => {
  const writes = await mockArchive(page)
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/e2e/archival.html')
  const archive = page.getByRole('button', { name: '归档组合', exact: true })
  const card = page.locator('.rounded-card').filter({ has: archive })
  await card.evaluate(element => { element.style.transform = 'translateY(-1px)' })
  await archive.click()
  const dialog = page.getByRole('dialog').filter({ has: page.getByText('归档组合', { exact: true }) })
  await expect(dialog).toBeVisible()
  await expect.poll(async () => {
    const box = (await dialog.boundingBox())!
    return Math.abs(box.x + box.width / 2 - 640) + Math.abs(box.y + box.height / 2 - 400)
  }).toBeLessThan(2)
  const scrim = page.locator('.bg-scrim.opacity-100')
  await expect(scrim).toHaveCount(1)
  expect(await scrim.boundingBox()).toEqual({ x: 0, y: 0, width: 1280, height: 800 })
  await page.screenshot({ path: info.outputPath('archive-confirm-viewport.png'), fullPage: true })
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(archive).toBeFocused()
  expect(writes).toEqual([])
})

test('删除组合列出归档账户并建议归档，失败后可重试且不发送删除', async ({ page }, info) => {
  const writes = await mockArchive(page)
  let archivedReads = 0
  await page.route('**/api/v1/account/dashboard?archived=true', route => {
    archivedReads++
    return route.fulfill({ json: { data: [
    { account_id: 7, name: '历史账户甲', portfolio_id: 1, archived_at: '2026-11-09T01:00:00Z', trade_channel: 'ctp', market: '期货' },
    { account_id: 8, name: '历史账户乙', portfolio_id: 1, archived_at: '2026-11-09T01:00:00Z', trade_channel: 'ctp', market: '期货' },
    { account_id: 9, name: '无关账户', portfolio_id: 2, archived_at: '2026-11-09T01:00:00Z', trade_channel: 'ctp', market: '期货' },
    ] } })
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/e2e/archival.html')
  await expect(page.getByText('未绑定', { exact: true })).toBeVisible()
  await expect(page.getByText('历史账户甲')).toHaveCount(0)
  await expect(page.getByText('建议归档组合，保留配置与绑定。')).toHaveCount(0)
  expect(archivedReads).toBe(0)
  await page.getByRole('button', { name: '删除组合', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '无法删除组合', exact: true })
  await expect(dialog.getByText('以下已归档账户仍绑定「商品趋势」：')).toBeVisible()
  expect(archivedReads).toBe(1)
  await expect(dialog.getByRole('link', { name: /历史账户甲/ })).toHaveAttribute('href', '/accounts/7')
  await expect(dialog.getByRole('link', { name: /历史账户乙/ })).toHaveAttribute('href', '/accounts/8')
  await expect(dialog.getByText('#7 · 已归档')).toBeVisible()
  await expect(dialog.getByText('#8 · 已归档')).toBeVisible()
  await expect(dialog.getByText('无关账户')).toHaveCount(0)
  await expect(dialog.getByText('建议归档组合，保留配置与绑定。')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '归档组合', exact: true })).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('archived-bindings-narrow.png'), fullPage: true })
  await dialog.getByRole('button', { name: '归档组合', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('归档组合失败')
  await dialog.getByRole('button', { name: '归档组合', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByText('还没有组合', { exact: true })).toBeVisible()
  expect(writes).toEqual(['POST /api/v1/portfolio/1/archive', 'POST /api/v1/portfolio/1/archive'])
})

test('点击删除时绑定查询失败则中止，重新点击可重试', async ({ page }) => {
  const writes = await mockArchive(page)
  let reject = true
  await page.route('**/api/v1/account/dashboard?archived=true', route => {
    if (reject) {
      reject = false
      return route.fulfill({ status: 503, json: { detail: '读取失败' } })
    }
    return route.fulfill({ json: { data: [] } })
  })
  await page.goto('/e2e/archival.html')
  await expect(page.getByText('未绑定', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '删除组合', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('读取失败')
  await expect(page.getByText('删除「商品趋势」。此操作不可撤销。')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '删除组合', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '删除组合', exact: true }).click()
  await expect(page.getByText('删除「商品趋势」。此操作不可撤销。')).toBeVisible()
  expect(writes).toEqual([])
})

test('未归档账户仍绑定时列出所有绑定且不建议直接归档', async ({ page }) => {
  const writes = await mockArchive(page)
  await page.route('**/api/v1/account/dashboard?archived=true', route => route.fulfill({ json: { data: [
    { account_id: 7, name: '历史账户甲', portfolio_id: 1, archived_at: '2026-11-09T01:00:00Z', trade_channel: 'ctp', market: '期货' },
  ] } }))
  await page.goto('/e2e/archival.html')
  await expect(page.getByText('未绑定', { exact: true })).toBeVisible()
  await page.evaluate(async () => {
    const domainPath = '/src/stores/domain.ts'
    const { useDomainStore } = await import(domainPath)
    useDomainStore.setState({ accounts: [{ account_id: 10, name: '当前账户', portfolio_id: 1, trade_channel: 'ctp', market: '期货' }] })
  })
  await page.getByRole('button', { name: '删除组合', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '无法删除组合', exact: true })
  await expect(dialog.getByRole('link', { name: /当前账户/ })).toHaveAttribute('href', '/accounts/10')
  await expect(dialog.getByRole('link', { name: /历史账户甲/ })).toHaveAttribute('href', '/accounts/7')
  await expect(dialog.getByRole('button', { name: '归档组合', exact: true })).toHaveCount(0)
  await expect(dialog.getByText('请先在未归档账户中改绑或解绑。')).toBeVisible()
  expect(writes).toEqual([])
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
