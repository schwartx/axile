import { expect, test } from '@playwright/test'

test.beforeEach(async ({ request }) => { await request.post('/api/v1/test/reset') })

test('compact rules keep controls and footer visible while only the table scrolls', async ({ page }) => {
  await page.goto('/e2e/futures-close.html')
  await page.getByRole('button', { name: '设置平仓规则' }).click()
  const dialog = page.getByRole('dialog', { name: '平仓规则', exact: true })
  for (const viewport of [{ width: 1280, height: 800 }, { width: 1280, height: 500 }, { width: 390, height: 700 }]) {
    await page.setViewportSize(viewport)
    await expect(dialog.getByRole('button', { name: '完成', exact: true })).toBeInViewport()
    await expect(dialog.getByRole('textbox', { name: '搜索或添加品种' })).toBeInViewport()
    const table = dialog.locator('table').locator('..')
    await expect.poll(() => table.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
    await expect.poll(() => dialog.evaluate((element) => {
      const body = element.children[1] as HTMLElement
      return body.scrollHeight <= body.clientHeight + 1 && element.scrollWidth <= element.clientWidth + 1
    })).toBe(true)
    await table.evaluate((element) => { element.scrollTop = element.scrollHeight })
    await expect(dialog.getByRole('textbox', { name: '搜索或添加品种' })).toBeInViewport()
    await expect(dialog.getByRole('button', { name: '完成', exact: true })).toBeInViewport()
  }
})

test('sticky header covers segmented buttons when rows scroll behind it', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/e2e/futures-close.html')
  await page.getByRole('button', { name: '设置平仓规则' }).click()
  const dialog = page.getByRole('dialog', { name: '平仓规则', exact: true })
  await dialog.getByRole('textbox', { name: '搜索或添加品种' }).fill('SHFE')
  const table = dialog.locator('table')
  const point = await table.evaluate((element) => {
    const header = element.querySelector('thead')!.getBoundingClientRect()
    const button = element.querySelector('tbody button')!.getBoundingClientRect()
    const y = header.y + header.height / 2
    element.parentElement!.scrollTop = button.y + button.height / 2 - y
    return { x: button.x + button.width / 2, y }
  })
  await expect.poll(() => table.locator('..').evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
  await expect.poll(() => page.evaluate(({ x, y }) => Boolean(document.elementFromPoint(x, y)?.closest('thead')), point)).toBe(true)
})

test('CTP edits product rules in a dialog and persists only on page save', async ({ page, request }) => {
  await page.goto('/e2e/futures-close.html')
  await page.getByRole('button', { name: '设置平仓规则' }).click()
  const dialog = page.getByRole('dialog', { name: '平仓规则', exact: true })
  await expect(dialog.getByRole('button', { name: '低手续费优先', exact: true })).toBeVisible()
  await dialog.getByRole('textbox', { name: '搜索或添加品种' }).fill('AG')
  const row = dialog.getByRole('row').filter({ has: page.getByRole('group', { name: 'AG 平仓顺序', exact: true }) })
  await expect(row).toContainText('万分之0.5')
  await expect(row).toContainText('AG2610—2612、2706')
  await expect(row).toContainText('万分之0.1')
  await expect(row).toContainText('AG2701—2705、2707—2709')
  await dialog.getByRole('group', { name: 'AG 平仓顺序', exact: true }).getByRole('button', { name: '先今后昨', exact: true }).click()
  await dialog.getByRole('button', { name: '完成', exact: true }).click()
  expect((await (await request.get('/api/v1/account/1')).json()).futures_close_policy.overrides).toEqual({})
  await expect(page.getByRole('button', { name: '设置平仓规则' })).toContainText('1 个品种已修改')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(async () => (await (await request.get('/api/v1/account/1')).json()).futures_close_policy.overrides).toEqual({ AG: 'today_first' })
  await page.reload()
  await page.getByRole('button', { name: '设置平仓规则' }).click()
  await dialog.getByRole('textbox', { name: '搜索或添加品种' }).fill('AG')
  await expect(dialog.getByRole('group', { name: 'AG 平仓顺序', exact: true }).getByRole('button', { name: '先今后昨', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await dialog.getByRole('button', { name: 'AG 恢复预设', exact: true }).click()
  await expect(dialog.getByRole('group', { name: 'AG 平仓顺序', exact: true }).getByRole('button', { name: '先昨后今', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByRole('button', { name: '设置平仓规则' })).toContainText('1 个品种已修改')
})

test('TQ exposes only product rules and rejects auto through API', async ({ page, request }) => {
  await page.goto('/e2e/futures-close.html')
  await page.getByRole('link', { name: 'TQ 示例账户' }).click()
  await page.getByRole('button', { name: '设置平仓规则' }).click()
  const dialog = page.getByRole('dialog', { name: '平仓规则', exact: true })
  await expect(dialog.getByRole('button', { name: '低手续费优先', exact: true })).toHaveCount(0)
  await dialog.getByRole('textbox', { name: '搜索或添加品种' }).fill('AG2611')
  await dialog.getByRole('button', { name: '添加品种', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('不填写月份')
  const response = await request.patch('/api/v1/account/2', { data: { futures_close_policy: { mode: 'fee' } } })
  expect(response.status()).toBe(422)
})


test('equal close fees share both columns and retain every contract group', async ({ page }) => {
  await page.goto('/e2e/futures-close.html')
  await page.getByRole('button', { name: '设置平仓规则' }).click()
  const dialog = page.getByRole('dialog', { name: '平仓规则', exact: true })
  await dialog.getByRole('textbox', { name: '搜索或添加品种' }).fill('HC')
  const shared = dialog.locator('td[colspan="2"]')
  await expect(shared).toContainText('平今、平昨相同')
  await expect(shared).toContainText('万分之1')
  await expect(shared).toContainText('HC2610—2612、2701、2705')
  await expect(shared).toContainText('万分之0.2')
  await expect(shared).toContainText('HC2702—2704、2706—2709')
})
